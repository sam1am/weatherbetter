# WeatherBetter

Which weather provider actually gets *your* zip code right?

Compares up to three years of archived hourly temperature predictions from ~12
forecast models (ECMWF, NOAA GFS, DWD ICON, UK Met Office, JMA, …) against what
actually happened, and ranks them by accuracy for a given zip code — at 1, 3,
5, and 7-day lead times (7 days is the longest lead any archive stores). All data
comes from free, keyless [Open-Meteo](https://open-meteo.com/) APIs:

- **Previous Runs API** — what each model predicted, 1 and 3 days ahead
- **Historical (ERA5) API** — what was actually observed
- **Geocoding API** — zip code → coordinates

## Web version

A single self-contained page (`index.html`) — everything runs in the browser.
Open it locally or host it anywhere static (e.g. GitHub Pages). Supports
shareable URLs like `index.html?zip=84116`, °F/°C toggle, and hover details.

## CLI version

```
python3 forecast_accuracy.py 84116            # errors in °F
python3 forecast_accuracy.py 84116 --celsius  # errors in °C
```

No dependencies beyond the Python 3 standard library.

## Reading the results

- **MAE 1d/3d/5d/7d** — mean absolute error of temperatures predicted that many
  days ahead. Lower is better.
- **Bias** — positive = provider runs warm, negative = runs cold.
- **same data as X** — outside a regional model's home domain, its "seamless"
  product serves another model's output; identical series are flagged so one
  dataset doesn't occupy multiple ranks.
- **partial archive** — the provider's prediction archive covers a shorter
  sub-period, so its numbers aren't directly comparable to the others.

Caveats: observations are ERA5 reanalysis (~9 km grid cells, not stations),
and Open-Meteo's prediction archive reaches back to about February 2024, so
"three years" grows into a full three years as the archive deepens. Consumer
apps (Apple Weather, AccuWeather, …) blend several of these models with their
own post-processing — the ranking compares the raw ingredients they build from.
