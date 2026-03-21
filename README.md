# Shorancks 👟

A personal Strava shoe dashboard that tracks mileage, rotation habits, and retirement projections across your shoe collection. Auto-refreshes weekly via GitHub Actions and publishes to GitHub Pages.

## Live dashboard

Once deployed: `https://hellke.github.io/shoerancks/dashboard.html`

## Local usage

```bash
pip install requests
python refresh.py
open dashboard.html
```

Credentials are read from `config.json` (never commit this file — it's in `.gitignore`).

## GitHub Actions setup

The workflow in `.github/workflows/refresh.yml` runs every Monday at 09:00 Stockholm time and redeploys GitHub Pages automatically.

You need to add three **Repository Secrets** (Settings → Secrets and variables → Actions):

| Secret name            | Value                        |
|------------------------|------------------------------|
| `STRAVA_CLIENT_ID`     | Your Strava app Client ID    |
| `STRAVA_CLIENT_SECRET` | Your Strava app Client Secret|
| `STRAVA_REFRESH_TOKEN` | Your Strava refresh token    |

Then enable GitHub Pages (Settings → Pages → Source: **GitHub Actions**).

## Adding features

Open an issue or just add ideas to the list below:

### Feature backlog
- [ ] Shoe purchase cost tracking + cost-per-km
- [ ] Week-by-week rotation heatmap
- [ ] Pace correlation per shoe
- [ ] Email/push alert when a shoe hits 700km
