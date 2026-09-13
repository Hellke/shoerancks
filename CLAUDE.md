# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A personal Strava shoe dashboard. `refresh.py` fetches Strava activity data, processes it, and injects it inline into `index.html`. The result is a fully self-contained static file — no server, no build step, no external data dependency at load time.

## Running locally

```bash
pip install requests
python refresh.py
open index.html
```

Credentials are read from `config.json` (gitignored). Required keys:

| Key | Purpose |
|-----|---------|
| `client_id`, `client_secret`, `refresh_token` | Strava OAuth |
| `github_pat`, `github_repo` | Optional — powers the "Refresh" button in the dashboard |

When running in GitHub Actions, the same keys are read from environment variables (see `.github/workflows/refresh.yml`).

## Architecture

```
refresh.py             — Python script: Strava API -> injects data into index.html
index.html             — Static HTML/JS: reads inline DASHBOARD_DATA -> Chart.js visualisations
shoe_config.json       — Per-shoe config (retirement distances, race shoe flags, colors)
best_efforts_cache.json — Generated: per-activity best efforts from Strava
laps_cache.json         — Generated: per-activity lap distance/time/heart rate
speed_points_log.json   — Generated: frozen per-activity speed point scores
```

**Data flow:**
1. `refresh.py` calls Strava OAuth token endpoint, then `/athlete/activities` (paginated, 200/page), and `/gear/{id}` for each unique gear ID.
2. Per-shoe retirement distances are read from `shoe_config.json`.
3. Processed data is injected as `const DASHBOARD_DATA = {...}; // injected by refresh.py` directly into `index.html`.
4. `index.html` reads that inline constant and renders everything client-side with Chart.js.

## Speed points

Speed points rank the shoes against each other at each of the 13 best-effort
distances — 8/5/3/2/1 for 1st→5th. Two things compute them, deliberately:

- `computeSpeedPoints()` in `index.html` derives the **current standings** shown
  on the shoe cards and the sort. It always reflects the latest data.
- `sync_speed_points()` in `refresh.py` scores **individual activities** for the
  logbook: how far a run moved the standings at the moment it happened. Because
  that answer depends on a moment in time, it is computed once and frozen in
  `speed_points_log.json`, and is never recomputed.

Scoring is **forward-only**. The first ever run of `sync_speed_points` records a
`seeded_at` watermark and leaves every pre-existing activity unscored (`pts: null`
in the payload) — there is no record of what the standings looked like back then.
Only activities newer than the watermark get a score.

Both rankers must break ties on gear ID. Without it a tied time is resolved by
list order, which is sorted by most recent use and therefore drifts.

## Pace & heart rate profiles

Each shoe card's detail page shows two histograms: kilometers run per pace band
and per heart rate band. Both are built from **laps**, not the kilometer splits
Strava also exposes. A lap is the unit a run was actually structured in, so an
interval session contributes its reps at rep pace and its recoveries at jog
pace. Kilometer splits average the two together and every interval session comes
out looking like a steady medium run.

Bin edges live in `refresh.py` as `PACE_EDGES` (min/km) and `HR_EDGES` (bpm).
They are *interior* edges: n edges produce n+1 bins, one open at each end. The
labels are generated from them and shipped in the payload as `pace_bins` /
`hr_bins`, so the two ends cannot drift apart — `index.html` only draws a chart
when the bin count it receives matches the data it receives.

**Backfill.** Laps arrive in the same `/activities/{id}` payload as best efforts,
so any activity fetched for the best-efforts cache fills both caches at once.
Everything predating this feature has to be re-fetched, and Strava allows only
100 reads per 15 minutes, so `backfill_laps()` walks history newest-first, one
window per run:

- `LAPS_INITIAL_WINDOW_DAYS` (30) — how far back the very first run reaches
- `LAPS_WINDOW_STEP_DAYS` (60) — how much further each later run reaches, applied
  only once the current window has been fully drained
- `LAPS_FETCH_BUDGET` (40) — hard cap on fetches per run

With a daily workflow that takes roughly two weeks to cover two years of
history. Until then coverage is partial, which is why every shoe ships a
`lap_coverage` block and each chart prints how many of the shoe's activities it
actually speaks for.

## GitHub Actions

`.github/workflows/refresh.yml` runs daily at 06:00 UTC and on `workflow_dispatch`. It runs `refresh.py` then deploys the repo to GitHub Pages.

Required repository secrets: `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`.

⚠️ The commit step lists files explicitly (`git add best_efforts_cache.json laps_cache.json speed_points_log.json index.html shoe_ids.md`). Any new generated file that must survive between runs has to be added there, or it is silently rebuilt from scratch every time.

## Key constants & config

- `shoe_config.json` — per-shoe settings:
  - `retirement_distances` — map of Strava gear ID → retirement km threshold
  - `default_retirement_km` — fallback when a shoe has no explicit entry
  - `race_shoe_ids` — list of gear IDs treated as race shoes
  - `shoe_colors` — map of gear ID → `{ primary, secondary }` hex colors
- `COLORS` dict in `refresh.py` maps shoe name keywords to hex colors (fallback if not in `shoe_config.json`).
- Shoe name display: Strava names like "Jacob - Kayano 30" are stripped to just "Kayano 30" (split on ` - ` or ` · `).
- `shoe_ids.md` is auto-generated by `refresh.py` as a human-readable name→ID reference for filling out `shoe_config.json`.
