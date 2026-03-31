"""
Shorancks — Strava Shoe Dashboard Refresher

Fetches Strava data, processes it, and writes to Supabase.
The dashboard.html is a static file that reads from Supabase on load —
no HTML generation happens here.

Run locally:  python refresh.py
GitHub Actions reads credentials from environment variables automatically.

Supabase setup (one-time, run in Supabase SQL editor):
  create table dashboard_data (
    id int8 primary key,
    data jsonb not null,
    updated_at timestamptz default now()
  );
  alter table dashboard_data enable row level security;
  create policy "Public read" on dashboard_data for select using (true);

config.json keys:
  client_id, client_secret, refresh_token   — Strava OAuth
  supabase_url, supabase_anon_key           — for reading (already used by dashboard)
  supabase_service_key                      — for writing (service role key from Supabase settings)
  github_pat, github_repo                   — optional, for the Refresh button in the dashboard
"""

import json
import os
import math
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path


# ── Credentials ────────────────────────────────────────────────────────────────
def load_config():
    env_id      = os.environ.get("STRAVA_CLIENT_ID")
    env_secret  = os.environ.get("STRAVA_CLIENT_SECRET")
    env_refresh = os.environ.get("STRAVA_REFRESH_TOKEN")
    if env_id and env_secret and env_refresh:
        return {
            "client_id":            env_id,
            "client_secret":        env_secret,
            "refresh_token":        env_refresh,
            "supabase_url":         os.environ.get("SUPABASE_URL", ""),
            "supabase_anon_key":    os.environ.get("SUPABASE_ANON_KEY", ""),
            "supabase_service_key": os.environ.get("SUPABASE_SERVICE_KEY", ""),
            "github_pat":           os.environ.get("GITHUB_PAT", ""),
            "github_repo":          os.environ.get("GITHUB_REPO", ""),
        }
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    raise RuntimeError("No credentials found. Create config.json or set environment variables.")


def save_refresh_token(config, new_token):
    config["refresh_token"] = new_token
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)


# ── Strava API ─────────────────────────────────────────────────────────────────
BASE = "https://www.strava.com/api/v3"
RETIREMENT_KM = 800


def get_access_token(config):
    print("Refreshing access token...")
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": config["refresh_token"],
        "grant_type":    "refresh_token",
    })
    r.raise_for_status()
    data = r.json()
    save_refresh_token(config, data["refresh_token"])
    return data["access_token"]


def fetch_athlete(headers):
    r = requests.get(f"{BASE}/athlete", headers=headers)
    r.raise_for_status()
    return r.json()


def fetch_all_activities(headers):
    print("Fetching activities...")
    activities, page = [], 1
    while True:
        r = requests.get(f"{BASE}/athlete/activities", headers=headers,
                         params={"per_page": 200, "page": page})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        activities.extend(batch)
        print(f"  Page {page}: {len(batch)} activities ({len(activities)} total)")
        page += 1
    return activities


def fetch_gear(gear_id, headers):
    r = requests.get(f"{BASE}/gear/{gear_id}", headers=headers)
    r.raise_for_status()
    return r.json()


# ── Supabase ───────────────────────────────────────────────────────────────────
def fetch_shoe_settings(config):
    """Fetch per-shoe settings (e.g. custom retirement_km) from Supabase."""
    url = config.get("supabase_url", "")
    key = config.get("supabase_anon_key", "")
    if not url or not key:
        return {}
    try:
        r = requests.get(
            f"{url}/rest/v1/shoe_settings",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=5,
        )
        if r.ok:
            rows = r.json()
            print(f"  Loaded {len(rows)} shoe setting(s) from Supabase.")
            return {row["shoe_id"]: row for row in rows}
        print(f"  Warning: Supabase shoe_settings returned {r.status_code}")
    except Exception as e:
        print(f"  Warning: Could not fetch shoe settings: {e}")
    return {}


def push_to_supabase(data, config):
    """Write the full dashboard data blob to Supabase dashboard_data table."""
    url = config.get("supabase_url", "")
    key = config.get("supabase_service_key", "")
    if not url or not key:
        print("  Supabase service key not configured — skipping push.")
        print("  Add 'supabase_service_key' to config.json (Settings > API > service_role key).")
        return
    try:
        r = requests.post(
            f"{url}/rest/v1/dashboard_data",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates",
            },
            json={"id": 1, "data": data, "updated_at": datetime.utcnow().isoformat()},
            timeout=10,
        )
        if r.ok:
            print("  Dashboard data written to Supabase ✓")
        else:
            print(f"  Warning: Supabase write returned {r.status_code}: {r.text}")
    except Exception as e:
        print(f"  Warning: Could not push to Supabase: {e}")


# ── Colors ─────────────────────────────────────────────────────────────────────
COLORS = {
    "Kayano":     "#FFD166",
    "Superblast": "#8B5CF6",
    "Novablast":  "#FF8C42",
    "Trabuco 12": "#34D399",
    "Terra":      "#10B981",
    "Metaspeed":  "#06D6A0",
    "Megablast":  "#FC4C02",
    "Nimbus":     "#6B7280",
}
FALLBACK_COLORS = ["#3d9af1", "#f59e0b", "#e74c3c", "#9b59b6", "#1abc9c"]


def color_for(shoe):
    for keyword, color in COLORS.items():
        if keyword.lower() in shoe["name"].lower():
            return color
    return FALLBACK_COLORS[hash(shoe["id"]) % len(FALLBACK_COLORS)]


# ── Data Processing ────────────────────────────────────────────────────────────
def process(activities, gear_map, shoe_settings=None):
    shoe_settings  = shoe_settings or {}
    shoe_ids       = [gid for gid, g in gear_map.items() if not gid.startswith("b")]
    shoe_monthly   = {id: defaultdict(float) for id in shoe_ids}
    shoe_types     = {id: defaultdict(int)   for id in shoe_ids}
    shoe_total_km  = {id: 0.0               for id in shoe_ids}
    shoe_run_count = {id: 0                 for id in shoe_ids}
    shoe_acts      = {id: []               for id in shoe_ids}

    for act in activities:
        gid = act.get("gear_id")
        if not gid or gid not in shoe_ids or not act.get("distance"):
            continue
        km    = act["distance"] / 1000
        month = act["start_date_local"][:7]
        atype = act.get("sport_type") or act.get("type") or "Run"
        shoe_monthly[gid][month] += km
        shoe_types[gid][atype]   += 1
        shoe_total_km[gid]       += km
        shoe_run_count[gid]      += 1
        shoe_acts[gid].append(act)

    all_months = sorted({m for sid in shoe_ids for m in shoe_monthly[sid]})
    today      = datetime.utcnow().date()
    shoes_out  = []

    for gid in shoe_ids:
        g    = gear_map[gid]
        acts = sorted(shoe_acts[gid], key=lambda a: a["start_date_local"])

        total_km = shoe_total_km[gid]
        runs     = shoe_run_count[gid]
        avg_km   = round(total_km / runs, 1) if runs else 0

        ret_km = shoe_settings.get(gid, {}).get("retirement_km", RETIREMENT_KM)

        # Retirement projection from recent cadence (last 30 activities)
        recent   = acts[-min(30, len(acts)):]
        avg_days = 7.0
        if len(recent) >= 2:
            span     = (datetime.fromisoformat(recent[-1]["start_date_local"].replace("Z", "+00:00")) -
                        datetime.fromisoformat(recent[0]["start_date_local"].replace("Z", "+00:00"))).days
            avg_days = span / (len(recent) - 1)

        remaining_km = max(0, ret_km - total_km)
        runs_left    = math.ceil(remaining_km / avg_km) if avg_km else 0
        retire_date  = (today + timedelta(days=runs_left * avg_days)).strftime("%b %Y") if runs_left else None

        retired  = g.get("retired", False)
        pct_life = round(min(100, total_km / ret_km * 100), 1)

        # Display name: strip Strava prefix like "Jacob - Name" or "Jacob · Name"
        display_name = g["name"]
        if " - " in display_name:
            display_name = display_name.split(" - ", 1)[-1]
        elif " · " in display_name:
            display_name = display_name.split(" · ", 1)[-1]

        # Monthly & cumulative series
        monthly_series = [round(shoe_monthly[gid].get(m, 0), 1) for m in all_months]
        cum, cum_series = 0, []
        for v in monthly_series:
            cum += v
            cum_series.append(round(cum, 1))

        shoes_out.append({
            "id":            gid,
            "name":          display_name,
            "model":         g.get("model_name", ""),
            "brand":         g.get("brand_name", "ASICS"),
            "color":         color_for(g),
            "retired":       retired,
            "total_km":      round(total_km),
            "runs":          runs,
            "avg_km":        avg_km,
            "runs_left":     runs_left,
            "retire_date":   retire_date,
            "retirement_km": ret_km,
            "pct_life":      pct_life,
            "remaining":     max(0, round(ret_km - total_km)),
            "types":         dict(shoe_types[gid]),
            "monthly":       monthly_series,
            "cumulative":    cum_series,
            "run_distances": [round(a["distance"] / 1000, 2) for a in acts],
        })

    return {
        "generated":  datetime.utcnow().strftime("%d %b %Y"),
        "all_months": all_months,
        "shoes":      shoes_out,
        "totals": {
            "km":         round(sum(shoe_total_km[sid] for sid in shoe_ids)),
            "activities": sum(shoe_run_count[sid] for sid in shoe_ids),
            "shoes":      len(shoe_ids),
        },
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    config  = load_config()
    token   = get_access_token(config)
    headers = {"Authorization": f"Bearer {token}"}

    athlete = fetch_athlete(headers)
    print(f"Hello, {athlete['firstname']} {athlete['lastname']}!")

    activities = fetch_all_activities(headers)
    print(f"Total activities: {len(activities)}")

    gear_ids = {a["gear_id"] for a in activities if a.get("gear_id")}
    print(f"Fetching {len(gear_ids)} gear items...")
    gear_map = {}
    for gid in gear_ids:
        gear_map[gid] = fetch_gear(gid, headers)
        print(f"  · {gear_map[gid]['name']}")

    print("Fetching Supabase shoe settings...")
    shoe_settings = fetch_shoe_settings(config)

    data = process(activities, gear_map, shoe_settings)
    data["athlete"] = {"firstname": athlete["firstname"], "lastname": athlete["lastname"]}

    print("Pushing to Supabase...")
    push_to_supabase(data, config)
    print(f"\nDone. {data['totals']['activities']} activities across {data['totals']['shoes']} shoes.")


if __name__ == "__main__":
    main()
