"""
Shorancks — Strava Shoe Dashboard Refresher
Run locally:   python refresh.py
GitHub Actions reads credentials from environment variables automatically.
"""

import json
import os
import math
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

# ── Credentials ──────────────────────────────────────────────────────────────
# Locally: read from config.json
# GitHub Actions: read from environment variables (set as repo Secrets)

def load_config():
    env_id     = os.environ.get("STRAVA_CLIENT_ID")
    env_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    env_refresh = os.environ.get("STRAVA_REFRESH_TOKEN")
    if env_id and env_secret and env_refresh:
        return {"client_id": env_id, "client_secret": env_secret, "refresh_token": env_refresh}
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

# ── Strava API ────────────────────────────────────────────────────────────────

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

# ── Data Processing ───────────────────────────────────────────────────────────

def process(activities, gear_map):
    shoe_ids = [gid for gid, g in gear_map.items() if not gid.startswith("b")]

    shoe_monthly   = {id: defaultdict(float) for id in shoe_ids}
    shoe_types     = {id: defaultdict(int)   for id in shoe_ids}
    shoe_total_km  = {id: 0.0               for id in shoe_ids}
    shoe_run_count = {id: 0                 for id in shoe_ids}
    shoe_acts      = {id: []                for id in shoe_ids}

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
    today      = datetime.utcnow().date()

    shoes_out = []
    for gid in shoe_ids:
        g        = gear_map[gid]
        acts     = sorted(shoe_acts[gid], key=lambda a: a["start_date_local"])
        total_km = shoe_total_km[gid]
        runs     = shoe_run_count[gid]
        avg_km   = round(total_km / runs, 1) if runs else 0

        # Retirement projection based on recent cadence (last 30 runs)
        recent   = acts[-min(30, len(acts)):]
        avg_days = 7.0
        if len(recent) >= 2:
            span     = (datetime.fromisoformat(recent[-1]["start_date_local"]) -
                        datetime.fromisoformat(recent[0]["start_date_local"])).days
            avg_days = span / (len(recent) - 1) if len(recent) > 1 else 7.0

        remaining_km = max(0, RETIREMENT_KM - total_km)
        runs_left    = math.ceil(remaining_km / avg_km) if avg_km else 0
        retire_date  = (today + timedelta(days=runs_left * avg_days)).strftime("%b %Y") if runs_left else None

        # Monthly series
        monthly_series    = [round(shoe_monthly[gid].get(m, 0), 1) for m in all_months]
        cum, cum_series = 0, []
        for v in monthly_series:
            cum += v
            cum_series.append(round(cum, 1))

        shoes_out.append({
            "id":          gid,
            "name":        g["name"],
            "model":       g.get("model_name", ""),
            "brand":       g.get("brand_name", "ASICS"),
            "retired":     g.get("retired", False),
            "total_km":    round(total_km),
            "strava_km":   round(g.get("converted_distance") or g.get("distance", 0) / 1000),
            "runs":        runs,
            "avg_km":      avg_km,
            "runs_left":   runs_left,
            "retire_date": retire_date,
            "pct_life":    round(min(100, total_km / RETIREMENT_KM * 100), 1),
            "types":       dict(shoe_types[gid]),
            "monthly":     monthly_series,
            "cumulative":  cum_series,
        })

    return {
        "generated":  datetime.utcnow().strftime("%d %b %Y"),
        "athlete":    None,  # filled in below
        "all_months": all_months,
        "shoes":      shoes_out,
        "totals": {
            "km":         round(sum(shoe_total_km[sid] for sid in shoe_ids)),
            "activities": sum(shoe_run_count[sid] for sid in shoe_ids),
            "shoes":      len(shoe_ids),
        }
    }

# ── Dashboard HTML ────────────────────────────────────────────────────────────

COLORS = {
    # keyed by model keywords for consistent color assignment
    "Kayano":     "#FFD166",
    "Superblast": "#8B5CF6",
    "Novablast":  "#FF8C42",
    "Trabuco 12": "#34D399",
    "Terra":      "#10B981",
    "Metaspeed":  "#06D6A0",
    "Megablast":  "#FC4C02",
    "Nimbus":     "#6B7280",
}
FALLBACK_COLORS = ["#3d9af1","#f59e0b","#e74c3c","#9b59b6","#1abc9c"]

def color_for(shoe):
    for keyword, color in COLORS.items():
        if keyword.lower() in shoe["name"].lower():
            return color
    return FALLBACK_COLORS[hash(shoe["id"]) % len(FALLBACK_COLORS)]

def render_card(shoe, color):
    retired     = shoe["retired"]
    pct         = shoe["pct_life"]
    warn        = not retired and pct >= 70
    css_classes = "card" + (" retired" if retired else "") + (" warning" if warn else "")
    remaining   = max(0, round(RETIREMENT_KM - shoe["total_km"]))

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

    return f"""
  <div class="{css_classes}">
    <div class="card-accent" style="background:{color}"></div>
    <div class="card-tag">{shoe["brand"]} · {shoe["model"]}</div>
    <div class="card-model">{shoe["name"].split(" - ", 1)[-1] if " - " in shoe["name"] else shoe["name"].split(" · ", 1)[-1] if " · " in shoe["name"] else shoe["name"]}</div>
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
    <div class="life-label"><span>{pct}% of 800km</span><span>{remaining} km left</span></div>
    {badge}
  </div>"""

def build_html(data):
    athlete = data["athlete"]
    name    = f'{athlete["firstname"]} {athlete["lastname"]}'
    shoes   = data["shoes"]
    shoe_colors = {s["id"]: color_for(s) for s in shoes}

    cards_html = "\n".join(render_card(s, shoe_colors[s["id"]]) for s in shoes)

    js_shoes = json.dumps([{
        "name":       s["name"].split(" - ")[-1] if " - " in s["name"] else s["name"],
        "color":      shoe_colors[s["id"]],
        "retired":    s["retired"],
        "cumulative": s["cumulative"],
        "monthly":    s["monthly"],
        "types":      s["types"],
    } for s in shoes])
    js_months = json.dumps(data["all_months"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{name} · Shoe Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{ --bg:#0c0c0e; --surface:#141418; --surface2:#1c1c22; --border:#2a2a32; --text:#f0f0f0; --muted:#666; --orange:#FC4C02; }}
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
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:20px; position:relative; overflow:hidden; transition:border-color 0.2s; }}
  .card:hover {{ border-color:#3a3a44; }}
  .card.retired {{ opacity:0.55; }}
  .card.warning {{ border-color:#f59e0b55; }}
  .card-accent {{ position:absolute; top:0; left:0; right:0; height:3px; border-radius:14px 14px 0 0; }}
  .card-tag {{ font-size:10px; letter-spacing:1px; text-transform:uppercase; color:var(--muted); margin-bottom:10px; }}
  .card-model {{ font-size:13px; font-weight:600; margin-bottom!3px; line-height:1.3; }}
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
  @media(max-width:900px){{ .two-col {{ grid-template-columns:1fr; }} }}
  canvas {{ width:100% !important; }}
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
<script>
const months = {js_months};
const shoes  = {js_shoes};
const grid   = '#1f1f28', tick = '#555';
const tip    = {{ backgroundColor:'#1c1c22', borderColor:'#2a2a32', borderWidth:1, titleColor:'#aaa', bodyColor:'#eee' }};

new Chart(document.getElementById('cumChart'), {{
  type:'line',
  data:{{ labels:months, datasets:shoes.map(s=>( {{
    label:s.name, data:s.cumulative, borderColor:s.color,
    backgroundColor:s.color+'12', borderWidth:s.retired?1.5:2.5,
    borderDash:s.retired?[4,3]:[], pointRadius:0, pointHoverRadius:5, fill:false, tension:0.4
  }})) }},
  options:{{ responsive:true, interaction:{{mode:'index',intersect:false}},
    plugins:{{ legend:{{labels:{{color:'#888',font:{{size:11}},boxWidth:24,padding:16}}}}, tooltip:{{...tip, callbacks:{{label:c=>` ${{c.dataset.label}}: ${{c.parsed.y}} km`}}}} }},
    scales:{{ x:{{ticks:{{color:tick,font:{{size:10}},maxRotation:45}},grid:{{color:grid}}}}, y:{{ticks:{{color:tick,font:{{size:10}},callback:v=>v+'km'}},grid:{{color:grid}}}} }}
  }}
}});

new Chart(document.getElementById('monthlyChart'), {{
  type:'bar',
  data:{{ labels:months, datasets:shoes.map(s=>( {{
    label:s.name, data:s.monthly, backgroundColor:s.color+'cc', borderRadius:2, borderSkipped:false
  }})) }},
  options:{{ responsive:true,
    plugins:{{ legend:{{display:false}}, tooltip:{{...tip, callbacks:{{label:c=>` ${{c.dataset.label}}: ${{c.parsed.y}} km`}}}} }},
    scales:{{ x:{{stacked:true,ticks:{{color:tick,font:{{size:9}},maxRotation:60}},grid:{{color:grid}}}}, y:{{stacked:true,ticks:{{color:tick,font:{{size:10}},callback:v=>v+'km'}},grid:{{color:grid}}}} }}
  }}
}});

const allTypes=['Run','TrailRun','Walk','Hike'];
const typeColors={{Run:'#FC4C02cc',TrailRun:'#10B981cc',Walk:'#3d9af1cc',Hike:'#f59e0bcc'}};
new Chart(document.getElementById('typeChart'), {{
  type:'bar',
  data:{{ labels:shoes.map(s=>s.name.split(' · ')[0].split(' - ')[0]),
    datasets:allTypes.map(t=>( {{
      label:t, data:shoes.map(s=>s.types[t]||0), backgroundColor:typeColors[t], borderRadius:2, borderSkipped:false
    }})) }},
  options:{{ responsive:true, indexAxis:'y',
    plugins:{{ legend:{{labels:{{color:'#888',font:{{size:11}},boxWidth:20,padding:12}}}}, tooltip:{{...tip}} }},
    scales:{{ x:{{stacked:true,ticks:{{color:tick,font:{{size:10}}}},grid:{{color:grid}}}}, y:{{stacked:true,ticks:{{color:tick,font:{{size:10}}}},grid:{{color:grid}}}} }}
  }}
}});
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config      = load_config()
    token       = get_access_token(config)
    headers     = {"Authorization": f"Bearer {token}"}
    athlete     = fetch_athlete(headers)
    print(f"Hello, {athlete['firstname']} {athlete['lastname']}!")
    activities  = fetch_all_activities(headers)
    print(f"Total activities: {len(activities)}")

    gear_ids    = {a["gear_id"] for a in activities if a.get("gear_id")}
    print(f"Fetching {len(gear_ids)} gear items...")
    gear_map    = {}
    for gid in gear_ids:
        gear_map[gid] = fetch_gear(gid, headers)
        print(f"  · {gear_map[gid]['name']}")

    data           = process(activities, gear_map)
    data["athlete"] = athlete

    out_dir = Path(__file__).parent
    html    = build_html(data)
    (out_dir / "dashboard.html").write_text(html)
    print(f"\nDashboard saved to {out_dir / 'dashboard.html'}")

if __name__ == "__main__":
    main()
