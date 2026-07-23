# Shorancks 👟

A personal Strava shoe dashboard that tracks mileage, rotation habits, and retirement projections across your shoe collection. Auto-refreshes weekly via GitHub Actions and publishes to GitHub Pages.

## Live dashboard

Once deployed: `https://hellke.github.io/shoerancks/index.html`

## Local usage

```bash
pip install requests
python refresh.py
open index.html
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

## Adding a new shoe

The dashboard pulls mileage and retirement data straight from Strava, but each
shoe also gets a hand-cropped photo and a colour theme extracted from that photo.
Follow this routine whenever you add a shoe:

1. **Log it on Strava.** Make sure the shoe exists in Strava and has activities
   tagged to it — that's where mileage, first/last run, and the retirement
   projection come from.

2. **Drop the photo in the repo root.** A side-on product shot works best.
   Name it after the model, e.g. `Superblast 3.JPG`.

3. **Remove the background.** Add the filename to the `IMAGES` list in
   `remove_bg.py`, then run it (needs `pip install rembg pillow`):

   ```bash
   python remove_bg.py
   ```

   This produces `<name>_clean.png` — a 600×340 transparent PNG, auto-cropped
   and centred.

4. **Extract the colour theme.** Pull the dominant accent colours from the
   cleaned PNG:

   ```bash
   python extract_colors.py "Superblast 3_clean.png"
   ```

   Pick a vivid `primary` (usually the midsole/accent) and a supporting
   `secondary` (often the upper) from the printed swatches.

5. **Find the Strava gear ID.** After the next `python refresh.py`, look it up
   in the auto-generated `shoe_ids.md` (IDs are prefixed with `g`, e.g.
   `g32434469`).

6. **Wire it up** — three edits, keyed consistently:

   | File | Add | Key |
   |------|-----|-----|
   | `index.html` → `SHOE_IMAGES` | `'superblast 3': 'Superblast 3_clean.png'` | lowercase Strava `model_name` |
   | `index.html` → `SHOE_PALETTE` | `'superblast 3': { primary: '#f7826b', secondary: '#e3ceb2' }` | lowercase Strava `model_name` |
   | `shoe_config.json` | retirement km under `retirement_distances`, colours under `shoe_colors` | Strava gear ID (`g…`) |

   The retirement distance is your call — match a sibling model if unsure (e.g.
   the Superblast 3 uses 1000 km, same as the Superblast 2). Omit it to fall
   back to `default_retirement_km`.

7. **Refresh and check.**

   ```bash
   python refresh.py
   open index.html
   ```

## Adding features

Open an issue or just add ideas to the list below:

### Feature backlog
- [ ] Shoe purchase cost tracking + cost-per-km
- [ ] Week-by-week rotation heatmap
- [ ] Pace correlation per shoe
- [ ] Email/push alert when a shoe hits 700km
