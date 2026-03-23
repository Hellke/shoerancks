"""
Shorancks — Strava Shoe Dashboard Refresher
Run locally: python refresh.py
GitHub Actions reads credentials from environment variables automatically.

Supabase (optional): add supabase_url + supabase_anon_key to config.json
or set SUPABASE_URL / SUPABASE_ANON_KEY env vars for per-shoe settings.
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
    env_id     = os.environ.get("STRAVA_CLIENT_ID")
    env_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    env_refresh = os.environ.get("STRAVA_REFRESH_TOKEN")
    if env_id and env_secret and env_refresh:
        return {
            "client_id":          env_id,
            "client_secret":      env_secret,
            "refresh_token":      env_refresh,
            "supabase_url":       os.environ.get("SUPABASE_URL", ""),
            "supabase_anon_key":  os.environ.get("SUPABASE_ANON_KEY", ""),
        }
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    raise RuntimeError("No credentials found. Create config.json or set environment variables.")


def save_refresh_token(config, new_token):
    """Persist updated refresh token locally so it stays valid."""
    config["refresh_token"] = new_token
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)


# ── Strava API ─────────────────────────────────────────────────────────────────
BASE = "https://www.strava.com/api/v3"
RETIREMENT_KM = 800  # default; overridden per-shoe via Supabase


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
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
            },
            timeout=5,
        )
        if r.ok:
            rows = r.json()
            print(f"  Loaded {len(rows)} shoe setting(s) from Supabase.")
            return {row["shoe_id"]: row for row in rows}
        else:
            print(f"  Warning: Supabase returned {r.status_code}")
    except Exception as e:
        print(f"  Warning: Could not fetch Supabase settings: {e}")
    return {}


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
        shoe_monthly[gid][month]  += km
        shoe_types[gid][atype]    += 1
        shoe_total_km[gid]        += km
        shoe_run_count[gid]       += 1
        shoe_acts[gid].append(act)

    all_months = sorted({m for sid in shoe_ids for m in shoe_monthly[sid]})
    today = datetime.utcnow().date()
    shoes_out = []

    for gid in shoe_ids:
        g    = gear_map[gid]
        acts = sorted(shoe_acts[gid], key=lambda a: a["start_date_local"])

        total_km = shoe_total_km[gid]
        runs     = shoe_run_count[gid]
        avg_km   = round(total_km / runs, 1) if runs else 0

        # Per-shoe retirement threshold (Supabase override or global default)
        ret_km = shoe_settings.get(gid, {}).get("retirement_km", RETIREMENT_KM)

        # Retirement projection based on recent cadence (last 30 runs)
        recent   = acts[-min(30, len(acts)):]
        avg_days = 7.0
        if len(recent) >= 2:
            span     = (datetime.fromisoformat(recent[-1]["start_date_local"].replace("Z", "+00:00")) -
                        datetime.fromisoformat(recent[0]["start_date_local"].replace("Z", "+00:00"))).days
            avg_days = span / (len(recent) - 1) if len(recent) > 1 else 7.0

        remaining_km = max(0, ret_km - total_km)
        runs_left    = math.ceil(remaining_km / avg_km) if avg_km else 0
        retire_date  = (today + timedelta(days=runs_left * avg_days)).strftime("%b %Y") if runs_left else None

        # Monthly & cumulative series
        monthly_series = [round(shoe_monthly[gid].get(m, 0), 1) for m in all_months]
        cum, cum_series = 0, []
        for v in monthly_series:
            cum += v
            cum_series.append(round(cum, 1))

        # Per-run distances for the detail overlay histogram
        run_distances = [round(a["distance"] / 1000, 2) for a in acts]

        shoes_out.append({
            "id":            gid,
            "name":          g["name"],
            "model":         g.get("model_name", ""),
            "brand":         g.get("brand_name", "ASICS"),
            "retired":       g.get("retired", False),
            "total_km":      round(total_km),
            "strava_km":     round(g.get("converted_distance") or g.get("distance", 0) / 1000),
            "runs":          runs,
            "avg_km":        avg_km,
            "runs_left":     runs_left,
            "retire_date":   retire_date,
            "pct_life":      round(min(100, total_km / ret_km * 100), 1),
            "retirement_km": ret_km,
            "types":         dict(shoe_types[gid]),
            "monthly":       monthly_series,
            "cumulative":    cum_series,
            "run_distances": run_distances,
        })

    return {
        "generated":  datetime.utcnow().strftime("%d %b %Y"),
        "athlete":    None,
        "all_months": all_months,
        "shoes":      shoes_out,
        "totals": {
            "km":         round(sum(shoe_total_km[sid] for sid in shoe_ids)),
            "activities": sum(shoe_run_count[sid] for sid in shoe_ids),
            "shoes":      len(shoe_ids),
        },
    }


# ── Dashboard HTML ─────────────────────────────────────────────────────────────
COLORS = {
    "Kayano":      "#FFD166",
    "Superblast":  "#8B5CF6",
    "Novablast":   "#FF8C42",
    "Trabuco 12":  "#34D399",
    "Terra":       "#10B981",
    "Metaspeed":   "#06D6A0",
    "Megablast":   "#FC4C02",
    "Nimbus":      "#6B7280",
}
FALLBACK_COLORS = ["#3d9af1", "#f59e0b", "#e74c3c", "#9b59b6", "#1abc9c"]


def color_for(shoe):
    for keyword, color in COLORS.items():
        if keyword.lower() in shoe["name"].lower():
            return color
    return FALLBACK_COLORS[hash(shoe["id"]) % len(FALLBACK_COLORS)]


def render_card(shoe, color):
    retired   = shoe["retired"]
    pct       = shoe["pct_life"]
    ret_km    = shoe["retirement_km"]
    warn      = not retired and pct >= 70
    css_cls   = "card" + (" retired" if retired else "") + (" warning" if warn else "")
    remaining = max(0, round(ret_km - shoe["total_km"]))

    badge = ""
    if retired:
        badge = '<div class="retired-badge">✓ Retired</div>'
    elif pct >= 83:
        badge = '<div class="warning-badge">⚠ Approaching retirement</div>'

    retire_line = ""
    if shoe["retire_date"] and not retired:
        retire_line = f'<div class="card-retire">Est. retirement <span>{shoe["retire_date"]}</span></div>'
    elif retired and shoe["retire_date"]:
        retire_line = f'<div class="card-retire">Retired <span>{shoe["retire_date"]}</span></div>'

    runs_left_val = f'~{shoe["runs_left"]}' if shoe["runs_left"] and not retired else "—"

    # Display name: strip Strava prefix patterns like "Jacob - Name" or "Jacob · Name"
    display_name = shoe["name"]
    if " - " in display_name:
        display_name = display_name.split(" - ", 1)[-1]
    elif " · " in display_name:
        display_name = display_name.split(" · ", 1)[-1]

    return f"""
    <div class="{css_cls}" onclick="openShoe('{shoe['id']}')" style="cursor:pointer">
      <div class="card-accent" style="background:{color}"></div>
      <div class="card-tag">{shoe["brand"]} · {shoe["model"]}</div>
      <div class="card-model">{display_name}</div>
      <div class="card-km" style="color:{color}">{shoe["total_km"]} <span style="font-size:14px;font-weight:400;color:var(--muted)">km</span></div>
      <div class="card-runs">{shoe["runs"]} activities</div>
      <div class="card-stats">
        <div class="mini-stat">
          <div class="mini-stat-val" style="color:{color}">{shoe["avg_km"]}</div>
          <div class="mini-stat-label">avg km/run</div>
        </div>
        <div class="mini-stat">
          <div class="mini-stat-val" style="color:{color}">{runs_left_val}</div>
          <div class="mini-stat-label">runs left</div>
        </div>
      </div>
      {retire_line}
      <div class="life-track"><div class="life-fill" style="width:{min(pct,100)}%;background:{color}"></div></div>
      <div class="life-label"><span>{pct}% of {ret_km}km</span><span>{remaining} km left</span></div>
      {badge}
    </div>"""


def build_html(data, supabase_url="", supabase_anon_key=""):
    athlete    = data["athlete"]
    name       = f'{athlete["firstname"]} {athlete["lastname"]}'
    shoes      = data["shoes"]
    shoe_colors = {s["id"]: color_for(s) for s in shoes}
    cards_html  = "\n".join(render_card(s, shoe_colors[s["id"]]) for s in shoes)

    # Extended JS shoe data (includes per-run distances and all overlay fields)
    js_shoes = json.dumps([{
        "id":            s["id"],
        "name":          (s["name"].split(" - ", 1)[-1] if " - " in s["name"]
                          else s["name"].split(" · ", 1)[-1] if " · " in s["name"]
                          else s["name"]),
        "brand":         s["brand"],
        "model":         s["model"],
        "color":         shoe_colors[s["id"]],
        "retired":       s["retired"],
        "total_km":      s["total_km"],
        "runs":          s["runs"],
        "avg_km":        s["avg_km"],
        "retirement_km": s["retirement_km"],
        "pct_life":      s["pct_life"],
        "types":         s["types"],
        "monthly":       s["monthly"],
        "cumulative":    s["cumulative"],
        "run_distances": s["run_distances"],
    } for s in shoes])

    js_months       = json.dumps(data["all_months"])
    js_supabase_url = json.dumps(supabase_url or None)
    js_supabase_key = json.dumps(supabase_anon_key or None)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{name} · Shoe Dashboard</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.js"></script>
  <style>
    :root {{
      --bg:#0c0c0e; --surface:#141418; --surface2:#1c1c22;
      --border:#2a2a32; --text:#f0f0f0; --muted:#666; --orange:#FC4C02;
    }}
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ background:var(--bg); color:var(--text); font-family:'Helvetica Neue',Arial,sans-serif; padding:40px 32px; min-height:100vh; }}
    .header {{ display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:48px; flex-wrap:wrap; gap:16px; }}
    .header-left h1 {{ font-size:13px; letter-spacing:3px; text-transform:uppercase; color:var(--muted); margin-bottom:8px; }}
    .header-left h2 {{ font-size:36px; font-weight:300; letter-spacing:-0.5px; }}
    .header-left h2 span {{ color:var(--orange); font-weight:700; }}
    .header-right {{ display:flex; gap:32px; }}
    .stat {{ text-align:right; }}
    .stat-val {{ font-size:28px; font-weight:700; line-height:1; }}
    .stat-label {{ font-size:11px; color:var(--muted); letter-spacing:1px; text-transform:uppercase; margin-top:4px; }}
    .generated {{ font-size:11px; color:var(--muted); margin-bottom:36px; }}
    .section-title {{ font-size:11px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); margin-bottom:20px; }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(210px, 1fr)); gap:14px; margin-bottom:48px; }}
    .card {{ background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:20px; position:relative; overflow:hidden; transition:border-color 0.2s, transform 0.15s; }}
    .card:hover {{ border-color:#3a3a44; transform:translateY(-2px); }}
    .card.retired {{ opacity:0.55; }}
    .card.warning {{ border-color:#f59e0b55; }}
    .card-accent {{ position:absolute; top:0; left:0; right:0; height:3px; border-radius:14px 14px 0 0; }}
    .card-tag {{ font-size:10px; letter-spacing:1px; text-transform:uppercase; color:var(--muted); margin-bottom:10px; }}
    .card-model {{ font-size:13px; font-weight:600; margin-bottom:3px; line-height:1.3; }}
    .card-km {{ font-size:30px; font-weight:700; line-height:1; margin-bottom:3px; }}
    .card-runs {{ font-size:11px; color:var(--muted); margin-bottom:14px; }}
    .card-stats {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }}
    .mini-stat {{ background:var(--surface2); border-radius:8px; padding:8px 10px; }}
    .mini-stat-val {{ font-size:15px; font-weight:700; line-height:1; margin-bottom:2px; }}
    .mini-stat-label {{ font-size:9px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; }}
    .card-retire {{ font-size:11px; color:var(--muted); margin-bottom:12px; }}
    .card-retire span {{ font-weight:600; color:var(--text); }}
    .life-track {{ background:var(--surface2); border-radius:99px; height:5px; overflow:hidden; }}
    .life-fill {{ height:5px; border-radius:99px; }}
    .life-label {{ font-size:10px; color:var(--muted); margin-top:6px; display:flex; justify-content:space-between; }}
    .warning-badge {{ margin-top:10px; font-size:10px; color:#f59e0b; letter-spacing:0.5px; }}
    .retired-badge {{ margin-top:10px; font-size:10px; color:var(--muted); letter-spacing:0.5px; }}
    .charts {{ display:grid; gap:20px; }}
    .chart-block {{ background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:28px; }}
    .chart-block h3 {{ font-size:11px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); margin-bottom:24px; }}
    .two-col {{ grid-template-columns:3fr 2fr; }}
    @media(max-width:900px) {{ .two-col {{ grid-template-columns:1fr; }} }}
    canvas {{ width:100% !important; }}

    /* ── Overlay ──────────────────────────────────────────────────────────── */
    #overlay {{
      display:none; position:fixed; inset:0; background:var(--bg);
      z-index:100; overflow-y:auto; padding:40px 32px;
      animation: fadeIn 0.18s ease;
    }}
    @keyframes fadeIn {{ from {{ opacity:0; transform:translateY(12px); }} to {{ opacity:1; transform:translateY(0); }} }}
    .back-btn {{
      background:none; border:1px solid var(--border); color:var(--muted);
      font-size:12px; letter-spacing:1px; padding:8px 18px; border-radius:8px;
      cursor:pointer; margin-bottom:40px; display:inline-flex; align-items:center;
      gap:8px; transition:color 0.2s, border-color 0.2s; font-family:inherit;
    }}
    .back-btn:hover {{ color:var(--text); border-color:#3a3a44; }}
    .overlay-accent {{ height:4px; border-radius:99px; margin-bottom:20px; max-width:120px; }}
    .overlay-tag {{ font-size:10px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); margin-bottom:8px; }}
    .overlay-name {{ font-size:42px; font-weight:700; letter-spacing:-1px; margin-bottom:32px; line-height:1; }}
    .overlay-stats {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(150px,1fr)); gap:14px; margin-bottom:24px; }}
    .overlay-stat {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px 20px; }}
    .overlay-stat-val {{ font-size:28px; font-weight:700; line-height:1; margin-bottom:4px; }}
    .overlay-stat-label {{ font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; }}
    .overlay-life-section {{
      background:var(--surface); border:1px solid var(--border); border-radius:14px;
      padding:20px 24px; margin-bottom:32px;
    }}
    .overlay-life-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; gap:16px; flex-wrap:wrap; }}
    .overlay-life-pct {{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; }}
    .ret-km-editor {{ display:flex; align-items:center; gap:8px; }}
    .ret-km-editor label {{ font-size:11px; color:var(--muted); }}
    .ret-km-input {{
      background:var(--surface2); border:1px solid var(--border); color:var(--text);
      font-size:14px; font-weight:700; padding:6px 10px; border-radius:8px;
      width:80px; text-align:center; font-family:inherit;
    }}
    .ret-km-input:focus {{ outline:none; border-color:#3a3a44; }}
    .ret-km-save {{
      background:none; border:1px solid var(--border); color:var(--muted);
      font-size:11px; padding:6px 14px; border-radius:8px; cursor:pointer;
      transition:all 0.2s; font-family:inherit; letter-spacing:0.5px;
    }}
    .ret-km-save:hover {{ color:var(--text); border-color:#3a3a44; }}
    .ret-km-save.saved {{ color:#34D399; border-color:#34D399; }}
    .overlay-charts {{ display:grid; grid-template-columns:2fr 1fr; gap:20px; margin-top:0; }}
    @media(max-width:900px) {{ .overlay-charts {{ grid-template-columns:1fr; }} }}
    .no-supabase-note {{ font-size:11px; color:var(--muted); margin-left:8px; }}
  </style>
</head>
<body>

  <div class="header">
    <div class="header-left">
      <h1>Shoe Dashboard</h1>
      <h2>{athlete["firstname"]} <span>{athlete["lastname"]}</span></h2>
    </div>
    <div class="header-right">
      <div class="stat"><div class="stat-val" style="color:var(--orange)">{data["totals"]["km"]}</div><div class="stat-label">Total km</div></div>
      <div class="stat"><div class="stat-val">{data["totals"]["activities"]}</div><div class="stat-label">Activities</div></div>
      <div class="stat"><div class="stat-val">{data["totals"]["shoes"]}</div><div class="stat-label">Shoes</div></div>
    </div>
  </div>

  <p class="generated">Last updated {data["generated"]}</p>
  <p class="section-title">Your rotation</p>
  <div class="cards">
    {cards_html}
  </div>

  <p class="section-title">Usage over time</p>
  <div class="charts">
    <div class="chart-block">
      <h3>Cumulative km per shoe</h3>
      <canvas id="cumChart" height="300"></canvas>
    </div>
    <div class="charts two-col">
      <div class="chart-block">
        <h3>Monthly km per shoe</h3>
        <canvas id="monthlyChart" height="280"></canvas>
      </div>
      <div class="chart-block">
        <h3>Activity type per shoe</h3>
        <canvas id="typeChart" height="280"></canvas>
      </div>
    </div>
  </div>

  <!-- ── Full-screen shoe detail overlay ──────────────────────────────────── -->
  <div id="overlay">
    <button class="back-btn" onclick="closeShoe()">← Back to shoes</button>
    <div id="overlay-content"></div>
  </div>

  <script>
    // ── Data ──────────────────────────────────────────────────────────────────
    const months = {js_months};
    const shoes  = {js_shoes};

    // ── Supabase ──────────────────────────────────────────────────────────────
    const SUPABASE_URL = {js_supabase_url};
    const SUPABASE_KEY = {js_supabase_key};
    let supabaseClient = null;
    let shoeSettings   = {{}};

    if (SUPABASE_URL && SUPABASE_KEY) {{
      supabaseClient = window.supabase.createClient(SUPABASE_URL, SUPABASE_KEY);
      supabaseClient.from('shoe_settings').select('*').then(({{ data, error }}) => {{
        if (data) data.forEach(row => {{ shoeSettings[row.shoe_id] = row; }});
      }});
    }}

    // ── Chart shared config ───────────────────────────────────────────────────
    const grid = '#1f1f28', tick = '#555';
    const tip  = {{
      backgroundColor:'#1c1c22', borderColor:'#2a2a32', borderWidth:1,
      titleColor:'#aaa', bodyColor:'#eee',
    }};

    // ── Main page charts ──────────────────────────────────────────────────────
    new Chart(document.getElementById('cumChart'), {{
      type: 'line',
      data: {{
        labels: months,
        datasets: shoes.map(s => ({{
          label: s.name, data: s.cumulative, borderColor: s.color,
          backgroundColor: s.color + '12', borderWidth: s.retired ? 1.5 : 2.5,
          borderDash: s.retired ? [4,3] : [], pointRadius: 0, pointHoverRadius: 5,
          fill: false, tension: 0.4,
        }})),
      }},
      options: {{
        responsive: true,
        interaction: {{ mode:'index', intersect:false }},
        plugins: {{
          legend: {{ labels: {{ color:'#888', font:{{ size:11 }}, boxWidth:24, padding:16 }} }},
          tooltip: {{ ...tip, callbacks: {{ label: c => ` ${{c.dataset.label}}: ${{c.parsed.y}} km` }} }},
        }},
        scales: {{
          x: {{ ticks:{{ color:tick, font:{{ size:10 }}, maxRotation:45 }}, grid:{{ color:grid }} }},
          y: {{ ticks:{{ color:tick, font:{{ size:10 }}, callback: v => v+'km' }}, grid:{{ color:grid }} }},
        }},
      }},
    }});

    new Chart(document.getElementById('monthlyChart'), {{
      type: 'bar',
      data: {{
        labels: months,
        datasets: shoes.map(s => ({{
          label: s.name, data: s.monthly,
          backgroundColor: s.color + 'cc', borderRadius: 2, borderSkipped: false,
        }})),
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{ ...tip, callbacks: {{ label: c => ` ${{c.dataset.label}}: ${{c.parsed.y}} km` }} }},
        }},
        scales: {{
          x: {{ stacked:true, ticks:{{ color:tick, font:{{ size:9 }}, maxRotation:60 }}, grid:{{ color:grid }} }},
          y: {{ stacked:true, ticks:{{ color:tick, font:{{ size:10 }}, callback: v => v+'km' }}, grid:{{ color:grid }} }},
        }},
      }},
    }});

    const allTypes   = ['Run','TrailRun','Walk','Hike'];
    const typeColors = {{ Run:'#FC4C02cc', TrailRun:'#10B981cc', Walk:'#3d9af1cc', Hike:'#f59e0bcc' }};
    new Chart(document.getElementById('typeChart'), {{
      type: 'bar',
      data: {{
        labels: shoes.map(s => s.name.split(' · ')[0].split(' - ')[0]),
        datasets: allTypes.map(t => ({{
          label: t, data: shoes.map(s => s.types[t] || 0),
          backgroundColor: typeColors[t], borderRadius: 2, borderSkipped: false,
        }})),
      }},
      options: {{
        responsive: true,
        indexAxis: 'y',
        plugins: {{
          legend: {{ labels: {{ color:'#888', font:{{ size:11 }}, boxWidth:20, padding:12 }} }},
          tooltip: {{ ...tip }},
        }},
        scales: {{
          x: {{ stacked:true, ticks:{{ color:tick, font:{{ size:10 }} }}, grid:{{ color:grid }} }},
          y: {{ stacked:true, ticks:{{ color:tick, font:{{ size:10 }} }}, grid:{{ color:grid }} }},
        }},
      }},
    }});

    // ── Shoe detail overlay ───────────────────────────────────────────────────
    let histChart = null;
    let pieChart  = null;

    function openShoe(shoeId) {{
      const shoe  = shoes.find(s => s.id === shoeId);
      if (!shoe) return;

      const retKm    = (shoeSettings[shoeId] && shoeSettings[shoeId].retirement_km)
                       || shoe.retirement_km;
      const pct      = Math.min(100, shoe.total_km / retKm * 100).toFixed(1);
      const remaining = Math.max(0, retKm - shoe.total_km);

      // Build histogram (1 km buckets, index 0 = 0–1 km, index 50 = 50+ km)
      const allBuckets = new Array(51).fill(0);
      shoe.run_distances.forEach(d => {{
        allBuckets[Math.min(Math.floor(d), 50)]++;
      }});
      const maxRun    = shoe.run_distances.length
                        ? Math.ceil(Math.max(...shoe.run_distances)) : 15;
      const dispMax   = Math.min(50, Math.max(15, maxRun));
      const histData  = allBuckets.slice(0, dispMax + 1);
      histData.push(allBuckets[50]);
      const histLabels = Array.from({{length: dispMax + 1}}, (_, i) => `${{i}}–${{i+1}}`);
      histLabels.push('50+');

      // Pie chart data
      const pieLabels = Object.keys(shoe.types);
      const pieData   = Object.values(shoe.types);

      const supabaseNote = supabaseClient
        ? '' : '<span class="no-supabase-note">(Supabase not configured — changes saved locally only)</span>';

      document.getElementById('overlay-content').innerHTML = `
        <div class="overlay-accent" style="background:${{shoe.color}}"></div>
        <div class="overlay-tag">${{shoe.brand}} · ${{shoe.model}}</div>
        <div class="overlay-name" style="color:${{shoe.color}}">${{shoe.name}}</div>

        <div class="overlay-stats">
          <div class="overlay-stat">
            <div class="overlay-stat-val" style="color:${{shoe.color}}">${{shoe.total_km}}</div>
            <div class="overlay-stat-label">Total km</div>
          </div>
          <div class="overlay-stat">
            <div class="overlay-stat-val">${{shoe.runs}}</div>
            <div class="overlay-stat-label">Activities</div>
          </div>
          <div class="overlay-stat">
            <div class="overlay-stat-val">${{shoe.avg_km}}</div>
            <div class="overlay-stat-label">Avg km / run</div>
          </div>
        </div>

        <div class="overlay-life-section">
          <div class="overlay-life-header">
            <span class="overlay-life-pct" id="overlay-pct-label">${{pct}}% of retirement distance</span>
            <div class="ret-km-editor">
              <label>Retirement at</label>
              <input class="ret-km-input" id="ret-km-val" type="number" value="${{retKm}}" min="1" max="9999"/>
              <label>km</label>
              <button class="ret-km-save" id="ret-km-save-btn" onclick="saveRetirementKm('${{shoe.id}}', '${{shoe.color}}')">Save</button>
              ${{supabaseNote}}
            </div>
          </div>
          <div class="life-track">
            <div class="life-fill" id="overlay-life-fill" style="width:${{pct}}%;background:${{shoe.color}}"></div>
          </div>
          <div class="life-label" style="margin-top:8px">
            <span id="overlay-pct-right">${{pct}}%</span>
            <span id="overlay-remaining">${{remaining}} km left</span>
          </div>
        </div>

        <div class="overlay-charts">
          <div class="chart-block">
            <h3>Run distance distribution</h3>
            <canvas id="overlayHist"></canvas>
          </div>
          <div class="chart-block">
            <h3>Activity types</h3>
            <canvas id="overlayPie"></canvas>
          </div>
        </div>
      `;

      // Destroy any previous chart instances
      if (histChart) {{ histChart.destroy(); histChart = null; }}
      if (pieChart)  {{ pieChart.destroy();  pieChart  = null; }}

      histChart = new Chart(document.getElementById('overlayHist'), {{
        type: 'bar',
        data: {{
          labels: histLabels,
          datasets: [{{
            label: 'Runs',
            data: histData,
            backgroundColor: shoe.color + 'bb',
            borderRadius: 3,
            borderSkipped: false,
          }}],
        }},
        options: {{
          responsive: true,
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{ ...tip, callbacks: {{
              label: c => ` ${{c.parsed.y}} run${{c.parsed.y !== 1 ? 's' : ''}}`,
            }} }},
          }},
          scales: {{
            x: {{ ticks:{{ color:tick, font:{{ size:10 }}, maxRotation:60 }}, grid:{{ color:grid }} }},
            y: {{ ticks:{{ color:tick, font:{{ size:10 }}, stepSize:1 }}, grid:{{ color:grid }} }},
          }},
        }},
      }});

      const pieColorMap = {{
        Run:'#FC4C02', TrailRun:'#10B981', Walk:'#3d9af1',
        Hike:'#f59e0b', VirtualRun:'#8B5CF6', NordicSki:'#34D399',
      }};

      pieChart = new Chart(document.getElementById('overlayPie'), {{
        type: 'doughnut',
        data: {{
          labels: pieLabels,
          datasets: [{{
            data: pieData,
            backgroundColor: pieLabels.map(l => (pieColorMap[l] || '#888') + 'dd'),
            borderColor: '#1c1c22',
            borderWidth: 2,
            hoverOffset: 8,
          }}],
        }},
        options: {{
          responsive: true,
          plugins: {{
            legend: {{ labels: {{ color:'#888', font:{{ size:11 }}, padding:16 }} }},
            tooltip: {{ ...tip }},
          }},
        }},
      }});

      document.getElementById('overlay').style.display = 'block';
      document.body.style.overflow = 'hidden';
      window.scrollTo(0, 0);
    }}

    function closeShoe() {{
      document.getElementById('overlay').style.display = 'none';
      document.body.style.overflow = '';
    }}

    async function saveRetirementKm(shoeId, color) {{
      const input = document.getElementById('ret-km-val');
      const km    = parseInt(input.value, 10);
      if (!km || km < 1) return;

      // Update shoeSettings cache
      shoeSettings[shoeId] = shoeSettings[shoeId] || {{}};
      shoeSettings[shoeId].retirement_km = km;

      // Update progress bar live
      const shoe = shoes.find(s => s.id === shoeId);
      if (shoe) {{
        const pct       = Math.min(100, shoe.total_km / km * 100).toFixed(1);
        const remaining = Math.max(0, km - shoe.total_km);
        const fill      = document.getElementById('overlay-life-fill');
        if (fill) fill.style.width = pct + '%';
        const pctLabel  = document.getElementById('overlay-pct-label');
        if (pctLabel)   pctLabel.textContent = pct + '% of retirement distance';
        const pctRight  = document.getElementById('overlay-pct-right');
        if (pctRight)   pctRight.textContent = pct + '%';
        const remEl     = document.getElementById('overlay-remaining');
        if (remEl)      remEl.textContent = remaining + ' km left';
      }}

      const btn = document.getElementById('ret-km-save-btn');
      if (!supabaseClient) {{
        if (btn) {{ btn.textContent = 'Saved locally'; btn.classList.add('saved'); }}
        return;
      }}

      try {{
        const {{ error }} = await supabaseClient
          .from('shoe_settings')
          .upsert({{ shoe_id: shoeId, retirement_km: km }},
                  {{ onConflict: 'shoe_id' }});
        if (error) throw error;
        if (btn) {{
          btn.textContent = 'Saved ✓';
          btn.classList.add('saved');
          setTimeout(() => {{ btn.textContent = 'Save'; btn.classList.remove('saved'); }}, 2000);
        }}
      }} catch(e) {{
        console.error('Supabase save failed:', e);
        if (btn) btn.textContent = 'Error — check console';
      }}
    }}

    // Close overlay with Escape key
    document.addEventListener('keydown', e => {{
      if (e.key === 'Escape' && document.getElementById('overlay').style.display !== 'none') {{
        closeShoe();
      }}
    }});
  </script>
</body>
</html>"""


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

    print("Fetching Supabase settings...")
    shoe_settings = fetch_shoe_settings(config)

    data = process(activities, gear_map, shoe_settings)
    data["athlete"] = athlete

    out_dir = Path(__file__).parent
    html    = build_html(data,
                         supabase_url      = config.get("supabase_url", ""),
                         supabase_anon_key = config.get("supabase_anon_key", ""))
    (out_dir / "dashboard.html").write_text(html)
    print(f"\nDashboard saved to {out_dir / 'dashboard.html'}")


if __name__ == "__main__":
    main()
