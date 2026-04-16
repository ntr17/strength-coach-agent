"""
build_dashboard.py — Generate a static HTML dashboard from coach.db.

Reads lift history, health metrics, and strength estimates from coach.db,
then produces a self-contained HTML file with Chart.js visualizations.

Usage:
    python scripts/build_dashboard.py
    python scripts/build_dashboard.py --db-path data/coach.db --output output/dashboard.html
"""

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

DEFAULT_DB   = Path(__file__).parent.parent / "data" / "coach.db"
DEFAULT_OUT  = Path(__file__).parent.parent / "output" / "dashboard.html"
STATE_PATH   = Path(__file__).parent.parent / "system" / "state.json"
PROFILE_PATH = Path(__file__).parent.parent / "system" / "profile.json"

MAIN_LIFTS = ["Squat", "Bench Press", "Deadlift", "OHP"]

LIFT_COLORS = {
    "Squat":       "#4ade80",
    "Bench Press": "#60a5fa",
    "Deadlift":    "#fb923c",
    "OHP":         "#a78bfa",
}


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_data(conn):
    """Fetch all dashboard data from coach.db."""

    strength_rows = conn.execute("""
        SELECT week_number, exercise,
               ROUND(MAX(weight_kg * (1.0 + CAST(reps AS REAL) / 30.0)), 1) AS e1rm
        FROM lift_sets
        WHERE should_count = 1
          AND weight_kg IS NOT NULL AND weight_kg > 0
          AND reps IS NOT NULL AND reps > 0 AND reps <= 12
        GROUP BY week_number, exercise
        ORDER BY week_number
    """).fetchall()

    volume_rows = conn.execute("""
        SELECT week_number,
               ROUND(SUM(weight_kg * reps) / 1000.0, 2) AS tonnes
        FROM lift_sets
        WHERE should_count = 1
          AND weight_kg IS NOT NULL AND reps IS NOT NULL
        GROUP BY week_number
        ORDER BY week_number
    """).fetchall()

    days_rows = conn.execute("""
        SELECT week_number, COUNT(DISTINCT session_date) AS days
        FROM lift_sets
        WHERE session_date IS NOT NULL
        GROUP BY week_number
        ORDER BY week_number
    """).fetchall()

    health_rows = conn.execute("""
        SELECT log_date, body_weight_kg, body_fat_pct, resting_hr, hrv, sleep_hours, steps
        FROM health_log
        WHERE log_date >= date('now', '-150 days')
        ORDER BY log_date
    """).fetchall()

    est_rows = conn.execute("""
        SELECT exercise, e1rm_kg, e5rm_kg, estimated_at
        FROM strength_estimates
        WHERE (exercise, estimated_at) IN (
            SELECT exercise, MAX(estimated_at)
            FROM strength_estimates
            GROUP BY exercise
        )
        ORDER BY exercise
    """).fetchall()

    last_session = conn.execute(
        "SELECT MAX(session_date) FROM lift_sets WHERE session_date IS NOT NULL"
    ).fetchone()[0]

    latest_weight = conn.execute(
        "SELECT body_weight_kg, log_date FROM health_log "
        "WHERE body_weight_kg IS NOT NULL ORDER BY log_date DESC LIMIT 1"
    ).fetchone()

    return {
        "strength_rows": strength_rows,
        "volume_rows":   volume_rows,
        "days_rows":     days_rows,
        "health_rows":   health_rows,
        "est_rows":      est_rows,
        "last_session":  last_session,
        "latest_weight": latest_weight,
    }


def prepare_chart_data(data, profile):
    """Transform raw DB rows into Chart.js-ready structures."""

    targets = profile.get("goals", {}).get("current_e5rm_targets", {})

    # --- Strength chart (weekly e1RM per lift) ---
    all_weeks_set = set(r[0] for r in data["strength_rows"])
    all_weeks = sorted(all_weeks_set)

    strength_by_lift = {lift: {} for lift in MAIN_LIFTS}
    for week, exercise, e1rm in data["strength_rows"]:
        if exercise in strength_by_lift:
            strength_by_lift[exercise][week] = e1rm

    strength_datasets = []
    for lift in MAIN_LIFTS:
        vals = [strength_by_lift[lift].get(w) for w in all_weeks]
        strength_datasets.append({
            "label":           lift,
            "data":            vals,
            "borderColor":     LIFT_COLORS[lift],
            "backgroundColor": LIFT_COLORS[lift] + "22",
            "tension":         0.35,
            "spanGaps":        True,
            "pointRadius":     3,
        })

    # Dashed target lines (e5RM → e1RM equivalent)
    for lift in MAIN_LIFTS:
        raw = targets.get(lift)
        if raw is None:
            continue
        try:
            e1rm_target = round(float(raw) / 0.8706, 1)
        except (TypeError, ValueError):
            continue
        strength_datasets.append({
            "label":       f"{lift} target",
            "data":        [e1rm_target] * len(all_weeks),
            "borderColor": LIFT_COLORS[lift],
            "borderDash":  [6, 3],
            "tension":     0,
            "spanGaps":    True,
            "pointRadius": 0,
            "borderWidth": 1.5,
            "backgroundColor": "transparent",
        })

    # --- Volume & days ---
    vol_weeks  = [r[0] for r in data["volume_rows"]]
    vol_vals   = [r[1] for r in data["volume_rows"]]
    days_weeks = [r[0] for r in data["days_rows"]]
    days_vals  = [r[1] for r in data["days_rows"]]

    # --- Health time series ---
    health_dates = []
    weights      = []
    body_fat     = []
    hrv_vals     = []
    rhr_vals     = []
    sleep_vals   = []
    for row in data["health_rows"]:
        log_date, bw, bf, rhr, hrv, sleep_h, steps = row
        health_dates.append(log_date)
        weights.append(bw)
        body_fat.append(bf)
        rhr_vals.append(rhr)
        hrv_vals.append(hrv)
        sleep_vals.append(sleep_h)

    # --- Latest estimates ---
    estimates = {}
    for exercise, e1rm_kg, e5rm_kg, estimated_at in data["est_rows"]:
        estimates[exercise] = {
            "e1rm_kg":      e1rm_kg,
            "e5rm_kg":      e5rm_kg,
            "estimated_at": estimated_at,
        }

    return {
        "strength":  {"labels": all_weeks,     "datasets": strength_datasets},
        "volume":    {"labels": vol_weeks,      "data": vol_vals},
        "days":      {"labels": days_weeks,     "data": days_vals},
        "body_comp": {"labels": health_dates,   "weight": weights, "body_fat": body_fat},
        "recovery":  {"labels": health_dates,   "hrv": hrv_vals, "rhr": rhr_vals},
        "sleep":     {"labels": health_dates,   "data": sleep_vals},
        "estimates": estimates,
    }


def generate_html(chart_data, state, profile, last_session, latest_weight):
    week_num    = state.get("current_week", "?")
    block_num   = state.get("current_block", 1)
    total_weeks = profile.get("current_program", {}).get("total_weeks", 30)
    today_str   = date.today().isoformat()

    estimates = chart_data["estimates"]

    def _e1rm(lift):
        v = estimates.get(lift, {}).get("e1rm_kg")
        return f"{v}" if v is not None else "—"

    squat_e1rm  = _e1rm("Squat")
    bench_e1rm  = _e1rm("Bench Press")
    dl_e1rm     = _e1rm("Deadlift")
    ohp_e1rm    = _e1rm("OHP")
    weight_str  = f"{latest_weight[0]}kg" if latest_weight else "—"

    days_since = "—"
    if last_session:
        try:
            from datetime import date as date_cls
            days_since = str((date_cls.today() - date_cls.fromisoformat(last_session)).days)
        except Exception:
            pass

    phase_map = {1: "Volume", 2: "Strength", 3: "Intensity", 4: "Intensity", 5: "Peak", 6: "Test"}
    phase = phase_map.get(block_num, "Strength")
    if state.get("deload_week"):
        phase = "Deload"

    pct_done   = round(week_num / total_weeks * 100) if total_weeks and isinstance(week_num, int) else "?"
    weeks_left = (total_weeks - week_num) if isinstance(week_num, int) else "?"

    targets = profile.get("goals", {}).get("current_e5rm_targets", {})
    squat_target = targets.get("Squat", "120")
    bench_target = targets.get("Bench Press", "105")

    j_strength  = json.dumps(chart_data["strength"])
    j_volume    = json.dumps(chart_data["volume"])
    j_days      = json.dumps(chart_data["days"])
    j_body_comp = json.dumps(chart_data["body_comp"])
    j_recovery  = json.dumps(chart_data["recovery"])
    j_sleep     = json.dumps(chart_data["sleep"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Strength Coach — Week {week_num}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d1117; color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif;
      font-size: 14px; line-height: 1.5; padding: 24px 28px;
    }}
    /* ── Header ── */
    .header {{ margin-bottom: 20px; }}
    .header h1 {{ font-size: 1.4rem; font-weight: 700; letter-spacing: -0.02em; }}
    .header .meta {{ color: #64748b; font-size: 0.8rem; margin-top: 4px; }}
    .badges {{ display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }}
    .badge {{
      padding: 3px 10px; border-radius: 9999px;
      font-size: 0.7rem; font-weight: 600; letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .badge-blue   {{ background: #1e3a5f; color: #60a5fa; }}
    .badge-green  {{ background: #052e16; color: #4ade80; }}
    .badge-orange {{ background: #431407; color: #fb923c; }}
    /* ── Summary cards ── */
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px; margin-bottom: 20px;
    }}
    .card {{
      background: #161b22; border: 1px solid #21262d;
      border-radius: 10px; padding: 14px 16px;
    }}
    .card-label {{ font-size: 0.68rem; color: #6e7681; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }}
    .card-value {{ font-size: 1.6rem; font-weight: 700; line-height: 1; }}
    .card-unit  {{ font-size: 0.8rem; font-weight: 400; opacity: 0.7; }}
    .card-sub   {{ font-size: 0.68rem; color: #6e7681; margin-top: 4px; }}
    .c-green  {{ color: #4ade80; }}
    .c-blue   {{ color: #60a5fa; }}
    .c-orange {{ color: #fb923c; }}
    .c-purple {{ color: #a78bfa; }}
    .c-white  {{ color: #f1f5f9; }}
    /* ── Charts grid ── */
    .charts-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    @media (max-width: 860px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
    .chart-panel {{
      background: #161b22; border: 1px solid #21262d;
      border-radius: 10px; padding: 18px 20px;
    }}
    .chart-panel.wide {{ grid-column: 1 / -1; }}
    .chart-panel h2 {{
      font-size: 0.72rem; font-weight: 600; color: #6e7681;
      text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 14px;
    }}
    canvas {{ max-height: 260px; width: 100% !important; }}
    .wide canvas {{ max-height: 300px; }}
    /* ── Footer ── */
    .footer {{
      text-align: center; color: #3d444d; font-size: 0.72rem;
      margin-top: 28px; padding-top: 14px; border-top: 1px solid #21262d;
    }}
  </style>
</head>
<body>

<div class="header">
  <h1>Strength Coach &mdash; Week {week_num} / {total_weeks}</h1>
  <p class="meta">
    Generated: {today_str} &nbsp;|&nbsp;
    Last session: {last_session or '—'} ({days_since}d ago) &nbsp;|&nbsp;
    Bodyweight: {weight_str}
  </p>
  <div class="badges">
    <span class="badge badge-blue">Block {block_num}</span>
    <span class="badge badge-green">{phase}</span>
    <span class="badge badge-orange">{pct_done}% complete</span>
  </div>
</div>

<div class="cards">
  <div class="card">
    <div class="card-label">Squat e1RM</div>
    <div class="card-value c-green">{squat_e1rm}<span class="card-unit">kg</span></div>
    <div class="card-sub">Target: {squat_target}kg &times;5</div>
  </div>
  <div class="card">
    <div class="card-label">Bench e1RM</div>
    <div class="card-value c-blue">{bench_e1rm}<span class="card-unit">kg</span></div>
    <div class="card-sub">Target: {bench_target}kg &times;5</div>
  </div>
  <div class="card">
    <div class="card-label">Deadlift e1RM</div>
    <div class="card-value c-orange">{dl_e1rm}<span class="card-unit">kg</span></div>
    <div class="card-sub">Long-term: 220kg</div>
  </div>
  <div class="card">
    <div class="card-label">OHP e1RM</div>
    <div class="card-value c-purple">{ohp_e1rm}<span class="card-unit">kg</span></div>
    <div class="card-sub">Shoulder health</div>
  </div>
  <div class="card">
    <div class="card-label">Body Weight</div>
    <div class="card-value c-white">{weight_str}</div>
    <div class="card-sub">Latest reading</div>
  </div>
  <div class="card">
    <div class="card-label">Progress</div>
    <div class="card-value c-white">{pct_done}<span class="card-unit">%</span></div>
    <div class="card-sub">{weeks_left} weeks left</div>
  </div>
</div>

<div class="charts-grid">

  <div class="chart-panel wide">
    <h2>Strength Trajectory &mdash; e1RM by Week</h2>
    <canvas id="strengthChart"></canvas>
  </div>

  <div class="chart-panel">
    <h2>Volume Load (tonnes / week)</h2>
    <canvas id="volumeChart"></canvas>
  </div>

  <div class="chart-panel">
    <h2>Training Days per Week</h2>
    <canvas id="daysChart"></canvas>
  </div>

  <div class="chart-panel">
    <h2>Body Composition</h2>
    <canvas id="bodyCompChart"></canvas>
  </div>

  <div class="chart-panel">
    <h2>Recovery &mdash; HRV &amp; RHR</h2>
    <canvas id="recoveryChart"></canvas>
  </div>

  <div class="chart-panel">
    <h2>Sleep (hours / night)</h2>
    <canvas id="sleepChart"></canvas>
  </div>

</div>

<p class="footer">Strength Coach Agent &mdash; pipeline.py &mdash; {today_str}</p>

<script>
const strengthData  = {j_strength};
const volumeData    = {j_volume};
const daysData      = {j_days};
const bodyCompData  = {j_body_comp};
const recoveryData  = {j_recovery};
const sleepData     = {j_sleep};

const GRID  = '#21262d';
const TICK  = '#6e7681';
const scalesBase = {{
  x: {{ ticks: {{ color: TICK, maxTicksLimit: 12 }}, grid: {{ color: GRID }} }},
  y: {{ ticks: {{ color: TICK }}, grid: {{ color: GRID }} }},
}};
const legendBase = {{ labels: {{ color: '#94a3b8', boxWidth: 12, padding: 12 }} }};

// ── Strength ──────────────────────────────────────────────────────────────
new Chart(document.getElementById('strengthChart'), {{
  type: 'line',
  data: strengthData,
  options: {{
    responsive: true, maintainAspectRatio: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: legendBase }},
    scales: {{
      x: {{
        title: {{ display: true, text: 'Week', color: TICK }},
        ticks: {{ color: TICK }}, grid: {{ color: GRID }},
      }},
      y: {{
        title: {{ display: true, text: 'e1RM (kg)', color: TICK }},
        ticks: {{ color: TICK }}, grid: {{ color: GRID }},
      }},
    }},
  }},
}});

// ── Volume ────────────────────────────────────────────────────────────────
new Chart(document.getElementById('volumeChart'), {{
  type: 'bar',
  data: {{
    labels: volumeData.labels,
    datasets: [{{
      label: 'Volume (t)',
      data: volumeData.data,
      backgroundColor: '#3b82f644',
      borderColor: '#3b82f6',
      borderWidth: 1, borderRadius: 4,
    }}],
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{ legend: legendBase }},
    scales: scalesBase,
  }},
}});

// ── Days ──────────────────────────────────────────────────────────────────
new Chart(document.getElementById('daysChart'), {{
  type: 'bar',
  data: {{
    labels: daysData.labels,
    datasets: [{{
      label: 'Days trained',
      data: daysData.data,
      backgroundColor: '#4ade8044',
      borderColor: '#4ade80',
      borderWidth: 1, borderRadius: 4,
    }}],
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{ legend: legendBase }},
    scales: {{
      x: {{ ticks: {{ color: TICK }}, grid: {{ color: GRID }} }},
      y: {{ min: 0, max: 5, ticks: {{ color: TICK, stepSize: 1 }}, grid: {{ color: GRID }} }},
    }},
  }},
}});

// ── Body Comp ─────────────────────────────────────────────────────────────
new Chart(document.getElementById('bodyCompChart'), {{
  type: 'line',
  data: {{
    labels: bodyCompData.labels,
    datasets: [
      {{
        label: 'Weight (kg)', data: bodyCompData.weight,
        borderColor: '#60a5fa', backgroundColor: '#60a5fa22',
        tension: 0.3, spanGaps: true, pointRadius: 2, yAxisID: 'y',
      }},
      {{
        label: 'Body Fat %', data: bodyCompData.body_fat,
        borderColor: '#fb923c', backgroundColor: '#fb923c22',
        tension: 0.3, spanGaps: true, pointRadius: 2, yAxisID: 'y2',
      }},
    ],
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{ legend: legendBase }},
    scales: {{
      x: {{ ticks: {{ color: TICK, maxTicksLimit: 8 }}, grid: {{ color: GRID }} }},
      y: {{
        position: 'left',
        title: {{ display: true, text: 'kg', color: '#60a5fa' }},
        ticks: {{ color: '#60a5fa' }}, grid: {{ color: GRID }},
      }},
      y2: {{
        position: 'right',
        title: {{ display: true, text: '%', color: '#fb923c' }},
        ticks: {{ color: '#fb923c' }}, grid: {{ drawOnChartArea: false }},
      }},
    }},
  }},
}});

// ── Recovery ──────────────────────────────────────────────────────────────
new Chart(document.getElementById('recoveryChart'), {{
  type: 'line',
  data: {{
    labels: recoveryData.labels,
    datasets: [
      {{
        label: 'HRV (ms)', data: recoveryData.hrv,
        borderColor: '#4ade80', backgroundColor: '#4ade8022',
        tension: 0.3, spanGaps: true, pointRadius: 2, yAxisID: 'y',
      }},
      {{
        label: 'RHR (bpm)', data: recoveryData.rhr,
        borderColor: '#f43f5e', backgroundColor: '#f43f5e22',
        tension: 0.3, spanGaps: true, pointRadius: 2, yAxisID: 'y2',
      }},
    ],
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{ legend: legendBase }},
    scales: {{
      x: {{ ticks: {{ color: TICK, maxTicksLimit: 8 }}, grid: {{ color: GRID }} }},
      y: {{
        position: 'left',
        title: {{ display: true, text: 'HRV (ms)', color: '#4ade80' }},
        ticks: {{ color: '#4ade80' }}, grid: {{ color: GRID }},
      }},
      y2: {{
        position: 'right',
        title: {{ display: true, text: 'RHR (bpm)', color: '#f43f5e' }},
        ticks: {{ color: '#f43f5e' }}, grid: {{ drawOnChartArea: false }},
      }},
    }},
  }},
}});

// ── Sleep ─────────────────────────────────────────────────────────────────
new Chart(document.getElementById('sleepChart'), {{
  type: 'line',
  data: {{
    labels: sleepData.labels,
    datasets: [{{
      label: 'Sleep (h)', data: sleepData.data,
      borderColor: '#a78bfa', backgroundColor: '#a78bfa22',
      tension: 0.3, spanGaps: true, pointRadius: 2,
    }}],
  }},
  options: {{
    responsive: true, maintainAspectRatio: true,
    plugins: {{ legend: legendBase }},
    scales: {{
      x: {{ ticks: {{ color: TICK, maxTicksLimit: 8 }}, grid: {{ color: GRID }} }},
      y: {{
        min: 4, max: 10,
        ticks: {{ color: TICK }}, grid: {{ color: GRID }},
        title: {{ display: true, text: 'hours', color: TICK }},
      }},
    }},
  }},
}});
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Build strength coach HTML dashboard")
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--output",  default=str(DEFAULT_OUT))
    args = parser.parse_args()

    db_path  = Path(args.db_path)
    out_path = Path(args.output)

    if not db_path.exists():
        print(f"[dashboard] ERROR: DB not found at {db_path}")
        return

    state   = _load_json(STATE_PATH)
    profile = _load_json(PROFILE_PATH)

    conn = sqlite3.connect(str(db_path))
    try:
        data = fetch_data(conn)
    finally:
        conn.close()

    chart_data = prepare_chart_data(data, profile)
    html       = generate_html(chart_data, state, profile,
                               data["last_session"], data["latest_weight"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[dashboard] Written: {out_path}  ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
