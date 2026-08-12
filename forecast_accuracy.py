#!/usr/bin/env python3
"""Rank weather forecast providers by temperature accuracy for a zip code.

Uses only Open-Meteo APIs (no API key required, stdlib only):
  - Geocoding API          -> zip code to lat/lon
  - Previous Runs API      -> what each model *predicted*, archived per day
  - Historical (ERA5) API  -> what was actually *observed*

For each provider we pull up to the last 3 years of hourly temperature
predictions made 1, 3, 5, and 7 days ahead (Open-Meteo's prediction archive
reaches back to Feb 2024 and stores leads up to 7 days — nothing longer
exists), compare them against ERA5 reanalysis, and rank providers by mean
absolute error at each lead time.

Usage:  python3 forecast_accuracy.py [zipcode] [--celsius]
        (errors are reported in deg F by default)
"""

import json
import math
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

# Forecast models available in Open-Meteo's Previous Runs archive.
# "seamless" variants blend a provider's high-res regional model with its
# global model, i.e. the best product that provider offers for a location.
PROVIDERS = {
    "ecmwf_ifs025":         "ECMWF IFS (EU)",
    "gfs_seamless":         "NOAA GFS/HRRR (US)",
    "icon_seamless":        "DWD ICON (Germany)",
    "gem_seamless":         "ECCC GEM (Canada)",
    "jma_seamless":         "JMA (Japan)",
    "meteofrance_seamless": "Meteo-France AROME/ARPEGE",
    "ukmo_seamless":        "UK Met Office",
    "knmi_seamless":        "KNMI Harmonie (Netherlands)",
    "dmi_seamless":         "DMI Harmonie (Denmark)",
    "metno_seamless":       "MET Norway",
    "bom_access_global":    "BOM ACCESS (Australia)",
    "cma_grapes_global":    "CMA GRAPES (China)",
}

LEAD_DAYS = [1, 3, 5, 7]  # the archive stores nothing beyond 7-day leads
LEAD_VARS = [f"temperature_2m_previous_day{d}" for d in LEAD_DAYS]
MIN_COVERAGE = 0.5  # fraction of hours that must be non-null to count as "covers this location"
MAX_CONCURRENT_REQUESTS = 6  # stay polite to the free API
WINDOW_DAYS = 3 * 365
# Open-Meteo's Previous Runs archive only reaches back to ~mid-Feb 2024; the
# window clamps to this and grows toward a full 3 years as the archive deepens.
ARCHIVE_START = date(2024, 2, 15)

# Consumer apps don't expose a single model: most blend several of the
# government models ranked below with proprietary post-processing.
APP_CONTEXT = """\
How this maps to the weather apps you know (each blends multiple models):
  Apple Weather        Dark Sky-derived blend: HRRR + ECMWF + GFS and others
  Weather Underground  IBM's The Weather Company (GRAF model + multi-model blend)
  The Weather Channel  Same IBM/The Weather Company engine as Weather Underground
  Google (search)      Licenses The Weather Company data + Google's own ML models
  Microsoft/MSN        Microsoft Start weather, multi-model blend (formerly Foreca)
  AccuWeather          Proprietary blend of ~190 models incl. ECMWF and GFS
  WeatherBug           Proprietary forecast on its own station network
  Windy / yr.no        ECMWF directly (yr.no is MET Norway)
  NWS / weather.gov    Human forecasters guided by NBM, GFS, HRRR
The rankings above compare the raw model ingredients these apps are built from."""


def fetch_json(base_url, params, retries=4):
    url = base_url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:
            if attempt == retries - 1:
                raise
            # rate limits (HTTP 429) need a real pause, not a quick retry
            rate_limited = getattr(exc, "code", None) == 429
            time.sleep(8 * (attempt + 1) if rate_limited else 2 * (attempt + 1))


def geocode_zip(zipcode):
    data = fetch_json(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": zipcode, "count": 1},
    )
    results = data.get("results")
    if not results:
        return None
    r = results[0]
    place = ", ".join(p for p in [r.get("name"), r.get("admin1"), r.get("country_code")] if p)
    return r["latitude"], r["longitude"], place


def fetch_predictions(model, lat, lon, start, end):
    """Hourly temps this model predicted 1/3/5/7 days in advance."""
    data = fetch_json(
        "https://previous-runs-api.open-meteo.com/v1/forecast",
        {
            "latitude": lat,
            "longitude": lon,
            "models": model,
            "hourly": ",".join(LEAD_VARS),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
    )
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    return {var: dict(zip(times, hourly.get(var, []))) for var in LEAD_VARS}


def fetch_observed(lat, lon, start, end):
    """Hourly temps that actually occurred, from ERA5 reanalysis."""
    data = fetch_json(
        "https://archive-api.open-meteo.com/v1/archive",
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
    )
    hourly = data["hourly"]
    return dict(zip(hourly["time"], hourly["temperature_2m"]))


def score(predictions, observed):
    """MAE, RMSE, and bias over timestamps where both series have data."""
    errors = [
        pred - observed[t]
        for t, pred in predictions.items()
        if pred is not None and observed.get(t) is not None
    ]
    if not errors:
        return None
    n = len(errors)
    return {
        "n": n,
        "mae": sum(abs(e) for e in errors) / n,
        "rmse": math.sqrt(sum(e * e for e in errors) / n),
        "bias": sum(errors) / n,
    }


def main():
    args = [a for a in sys.argv[1:] if a != "--celsius"]
    celsius = "--celsius" in sys.argv
    scale, unit = (1.0, "C") if celsius else (1.8, "F")
    zipcode = args[0] if args else input("Enter zip code: ").strip()

    loc = geocode_zip(zipcode)
    if loc is None:
        sys.exit(f"Could not find a location for zip code {zipcode!r}.")
    lat, lon, place = loc
    print(f"Zip {zipcode} -> {place} ({lat:.4f}, {lon:.4f})")

    # ERA5 lags realtime by ~5 days; leave a week of headroom.
    end = date.today() - timedelta(days=7)
    start = max(end - timedelta(days=WINDOW_DAYS - 1), ARCHIVE_START)
    span = f"{(end - start).days / 365:.1f} years"
    if start == ARCHIVE_START:
        span += " (full prediction archive; Open-Meteo's goes back to Feb 2024)"
    print(f"Comparing predictions vs observations: {start} to {end} — {span}\n")

    print(f"Fetching ERA5 observations and predictions from {len(PROVIDERS)} providers concurrently...")
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as pool:
        observed_future = pool.submit(fetch_observed, lat, lon, start, end)
        pred_futures = {
            model: pool.submit(fetch_predictions, model, lat, lon, start, end)
            for model in PROVIDERS
        }
        observed = observed_future.result()

    results = []
    skipped = []
    for model, name in PROVIDERS.items():
        try:
            preds = pred_futures[model].result()
        except Exception as exc:
            skipped.append((name, f"request failed: {exc}"))
            continue

        day1 = preds["temperature_2m_previous_day1"]
        n_total = len(day1)
        n_valid = sum(1 for v in day1.values() if v is not None)
        if n_total == 0 or n_valid / n_total < MIN_COVERAGE:
            skipped.append((name, "does not cover this location (or too little archived data)"))
            continue

        scores = {
            d: score(preds[f"temperature_2m_previous_day{d}"], observed)
            for d in LEAD_DAYS
        }
        if scores[1] is None:
            skipped.append((name, "no overlapping data with observations"))
            continue
        # Fingerprint the prediction series: outside a provider's regional
        # domain its "seamless" product may serve another model's data.
        fingerprint = hash(tuple(sorted(day1.items())))
        results.append((name, scores, fingerprint))

    if skipped:
        print("\nProviders without usable data for this location:")
        for name, reason in skipped:
            print(f"  - {name}: {reason}")

    if not results:
        sys.exit("\nNo providers had usable prediction data for this location.")

    print(f"\n{len(results)} providers cover this location.")

    for lead in LEAD_DAYS:
        ranked = sorted(
            (r for r in results if r[1][lead] is not None),
            key=lambda r: r[1][lead]["mae"],
        )
        if not ranked:
            continue
        print(f"\n=== Predicted {lead} day{'s' if lead > 1 else ''} ahead "
              f"(lower MAE = better) ===\n")
        header = f"{'Rank':<5}{'Provider':<28}{'MAE':>8}{'RMSE':>9}{'Bias':>9}{'Hours':>8}"
        print(header)
        print("-" * len(header))
        max_hours = max(r[1][lead]["n"] for r in ranked)
        first_with_fingerprint = {}
        for rank, (name, scores, fingerprint) in enumerate(ranked, 1):
            s = scores[lead]
            notes = []
            if fingerprint in first_with_fingerprint:
                notes.append(f"same data as {first_with_fingerprint[fingerprint]}")
            else:
                first_with_fingerprint[fingerprint] = name
            if s["n"] < 0.8 * max_hours:
                notes.append(f"partial archive: {s['n'] / max_hours:.0%} of period")
            note = f"  ({'; '.join(notes)})" if notes else ""
            print(
                f"{rank:<5}{name:<28}{s['mae'] * scale:>8.2f}{s['rmse'] * scale:>9.2f}"
                f"{s['bias'] * scale:>+9.2f}{s['n']:>8}{note}"
            )

    print(
        f"\nMAE/RMSE/Bias in deg {unit}"
        + ("" if celsius else " (run with --celsius for deg C)")
        + ". Leads beyond 7 days are not archived anywhere."
        "\nPositive bias = provider runs warm; negative = runs cold."
        "\n'same data as X' = this provider serves another model's output here"
        "\n(regional models fall back to a global model outside their domain)."
        "\n'partial archive' = scored over a shorter sub-period than the others,"
        "\nso its numbers aren't directly comparable."
    )
    print("\n" + APP_CONTEXT)


if __name__ == "__main__":
    main()
