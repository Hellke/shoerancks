"""
Shorancks — Strava Shoe Dashboard Refresher

Fetches Strava data, processes it, and injects it inline into dashboard.html.
The dashboard.html is a fully self-contained static file — open it directly.

Run locally:  python refresh.py
GitHub Actions reads credentials from environment variables automatically.

config.json keys:
  client_id, client_secret, refresh_token   — Strava OAuth
  github_pat, github_repo                   — optional, for the Refresh button in the dashboard

shoe_config.json keys:
  retirement_distances   — dict of shoe_id -> retirement_km
  default_retirement_km  — fallback when a shoe has no explicit entry (default: 500)
"""

import json
import os
import re
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
            "client_id":     env_id,
            "client_secret": env_secret,
            "refresh_token": env_refresh,
            "github_pat":    os.environ.get("GITHUB_PAT", ""),
            "github_repo":   os.environ.get("GITHUB_REPO", ""),
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


def load_shoe_config():
    """Load per-shoe retirement distances from shoe_config.json."""
    path = Path(__file__).parent / "shoe_config.json"
    if path.exists():
        with open(path) as f:
            cfg = json.load(f)
        print(f"  Loaded shoe config: {len(cfg.get('retirement_distances', {}))} shoe(s) configured.")
        return cfg
    print("  No shoe_config.json found — using default retirement distance for all shoes.")
    return {"retirement_distances": {}, "default_retirement_km": 500}


# ── Strava API ─────────────────────────────────────────────────────────────────
BASE = "https://www.strava.com/api/v3"


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


# ── Output ─────────────────────────────────────────────────────────────────────
def write_dashboard_json(data):
    """Inject dashboard data inline into dashboard.html."""
    html_path = Path(__file__).parent / "dashboard.html"
    html = html_path.read_text(encoding="utf-8")
    json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    html, n = re.subn(
        r'const DASHBOARD_DATA = .*; // injected by refresh\.py',
        f'const DASHBOARD_DATA = {json_str}; // injected by refresh.py',
        html,
    )
    if n == 0:
        raise RuntimeError("Could not find DASHBOARD_DATA placeholder in dashboard.html")
    html_path.write_text(html, encoding="utf-8")
    print("  Dashboard data injected into dashboard.html ✓")


def write_shoe_lookup(data):
    """Write shoe_ids.md — a human-readable name→ID reference for shoe_config.json."""
    shoes = sorted(data["shoes"], key=lambda s: s["total_km"], reverse=True)
    lines = [
        "# Shoe IDs\n",
        "Use these IDs in `shoe_config.json` under `retirement_distances`.\n",
        f"Last updated: {data['generated']}\n",
        "\n",
        "| Name | Brand | Total km | Strava ID |\n",
        "|------|-------|----------|-----------|\n",
    ]
    for s in shoes:
        lines.append(f"| {s['name']} | {s['brand']} | {s['total_km']} km | `{s['id']}` |\n")
    path = Path(__file__).parent / "shoe_ids.md"
    path.write_text("".join(lines), encoding="utf-8")
    print(f"  Shoe ID lookup written to {path.name} ✓")


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
def process(activities, gear_map, shoe_config=None):
    shoe_config    = shoe_config or {}
    ret_distances  = shoe_config.get("retirement_distances", {})
    default_ret_km = shoe_config.get("default_retirement_km", 500)

    shoe_ids       = [gid for gid, g in gear_map.items() if not gid.startswith("b")]
    shoe_monthly   = {id: defaultdict(float) for id in shoe_ids}
    shoe_weekly    = {id: defaultdict(float) for id in shoe_ids}
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
        iso   = datetime.strptime(act["start_date_local"][:10], "%Y-%m-%d").date().isocalendar()
        week  = f"{iso[0]}-W{iso[1]:02d}"
        sport = act.get("sport_type") or act.get("type") or "Run"
        if sport == "Run":
            wt    = act.get("workout_type") or 0
            atype = {1: "Race", 2: "Long Run", 3: "Workout"}.get(wt, "Run")
        else:
            atype = sport
        shoe_monthly[gid][month] += km
        shoe_weekly[gid][week]   += km
        shoe_types[gid][atype]   += 1
        shoe_total_km[gid]       += km
        shoe_run_count[gid]      += 1
        shoe_acts[gid].append(act)

    all_months = sorted({m for sid in shoe_ids for m in shoe_monthly[sid]})
    all_weeks  = sorted({w for sid in shoe_ids for w in shoe_weekly[sid]})
    today      = datetime.utcnow().date()
    shoes_out  = []

    for gid in shoe_ids:
        g    = gear_map[gid]
        acts = sorted(shoe_acts[gid], key=lambda a: a["start_date_local"])

        total_km = shoe_total_km[gid]
        runs     = shoe_run_count[gid]
        avg_km   = round(total_km / runs, 1) if runs else 0

        ret_km = ret_distances.get(gid, default_ret_km)

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

        # Monthly, weekly & cumulative series
        monthly_series = [round(shoe_monthly[gid].get(m, 0), 1) for m in all_months]
        weekly_series  = [round(shoe_weekly[gid].get(w, 0), 1)  for w in all_weeks]
        cum, cum_series = 0, []
        for v in monthly_series:
            cum += v
            cum_series.append(round(cum, 1))

        first_run = acts[0]["start_date_local"][:10]  if acts else None
        last_run  = acts[-1]["start_date_local"][:10] if acts else None

        # Format as "15 Aug 2024"
        def fmt_date(d):
            return datetime.strptime(d, "%Y-%m-%d").strftime("%-d %b %Y") if d else None

        # Weeks active (span from first to last run)
        weeks_active = 1
        if acts and len(acts) >= 2:
            span_days = (datetime.strptime(last_run, "%Y-%m-%d") -
                         datetime.strptime(first_run, "%Y-%m-%d")).days
            weeks_active = max(1, round(span_days / 7))
        km_per_week = round(total_km / weeks_active, 1) if weeks_active else 0

        cfg_colors = shoe_config.get("shoe_colors", {}).get(gid)
        primary    = cfg_colors["primary"]   if cfg_colors else color_for(g)
        secondary  = cfg_colors["secondary"] if cfg_colors else "#6B7280"

        shoes_out.append({
            "id":            gid,
            "name":          display_name,
            "model":         g.get("model_name", ""),
            "brand":         g.get("brand_name", "ASICS"),
            "color":         primary,
            "secondary":     secondary,
            "retired":       retired,
            "total_km":      round(total_km),
            "runs":          runs,
            "avg_km":        avg_km,
            "km_per_week":   km_per_week,
            "first_run":     fmt_date(first_run),
            "last_run":      fmt_date(last_run),
            "last_run_iso":  last_run,
            "runs_left":     runs_left,
            "retire_date":   retire_date,
            "retirement_km": ret_km,
            "pct_life":      pct_life,
            "remaining":     max(0, round(ret_km - total_km)),
            "types":         dict(shoe_types[gid]),
            "monthly":       monthly_series,
            "weekly":        weekly_series,
            "cumulative":    cum_series,
            "run_distances": [round(a["distance"] / 1000, 2) for a in acts],
        })

    shoes_out.sort(key=lambda s: s.get("last_run_iso") or "", reverse=True)

    return {
        "generated":  datetime.utcnow().strftime("%d %b %Y"),
        "all_months": all_months,
        "all_weeks":  all_weeks,
        "shoes":      shoes_out,
        "totals": {
            "km":         round(sum(shoe_total_km[sid] for sid in shoe_ids)),
            "activities": sum(shoe_run_count[sid] for sid in shoe_ids),
            "shoes":      len(shoe_ids),
        },
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    config      = load_config()
    shoe_config = load_shoe_config()
    token       = get_access_token(config)
    headers     = {"Authorization": f"Bearer {token}"}

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

    data = process(activities, gear_map, shoe_config)
    data["athlete"] = {"firstname": athlete["firstname"], "lastname": athlete["lastname"]}

    print("Injecting data into dashboard.html...")
    write_dashboard_json(data)
    write_shoe_lookup(data)
    print(f"\nDone. {data['totals']['activities']} activities across {data['totals']['shoes']} shoes.")


if __name__ == "__main__":
    main()
