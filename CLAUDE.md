# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A personal Strava shoe dashboard. `refresh.py` fetches Strava activity data, processes it, and writes a JSON blob to Supabase. `index.html` is a fully static frontend that reads that blob from Supabase at page load time — no server, no build step.

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
| `supabase_url`, `supabase_anon_key` | Supabase read access (also used by the dashboard) |
| `supabase_service_key` | Supabase write access (service role key) |
| `github_pat`, `github_repo` | Optional — powers the "Refresh" button in the dashboard |

When running in GitHub Actions, the same keys are read from environment variables (see `.github/workflows/refresh.yml`).

## Architecture

```
refresh.py          — Python script: Strava API -> Supabase
index.html      — Static HTML/JS: Supabase -> Chart.js visualisations
```

**Data flow:**
1. `refresh.py` calls Strava OAuth token endpoint, then `/athlete`, `/athlete/activities` (paginated, 200/page), and `/gear/{id}` for each unique gear ID.
2. It also reads a `shoe_settings` table from Supabase (per-shoe custom `retirement_km`).
3. Processed data is upserted as a single JSON blob into `dashboard_data` (row `id=1`).
4. `index.html` fetches that row from Supabase on load and renders everything client-side with Chart.js.

**Supabase schema (one-time setup):**
```sql
create table dashboard_data (
  id int8 primary key,
  data jsonb not null,
  updated_at timestamptz default now()
);
alter table dashboard_data enable row level security;
create policy "Public read" on dashboard_data for select using (true);
```

## GitHub Actions

`.github/workflows/refresh.yml` runs every Monday at 07:00 UTC and on `workflow_dispatch`. It runs `refresh.py` then deploys the repo to GitHub Pages.

Required repository secrets: `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`.

## Key constants

- `RETIREMENT_KM = 800` in `refresh.py` — default retirement threshold (overridable per shoe via `shoe_settings` table).
- `COLORS` dict in `refresh.py` maps shoe name keywords to hex colors used consistently across the dashboard.
- Shoe name display: Strava names like "Jacob - Kayano 30" are stripped to just "Kayano 30" (split on ` - ` or ` · `).
