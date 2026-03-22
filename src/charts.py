"""
Chart generation for weekly summary emails and analytics reports.
Uses matplotlib to produce inline PNG images (no files written to disk).

Charts available:
  generate_1rm_chart()              — e1RM trend lines per lift
  generate_volume_chart()           — completed sets bar chart per week
  generate_bodyweight_chart()       — bodyweight scatter + rolling avg
  generate_rep_bucket_chart()       — stacked bar: strength/hypertrophy/endurance volume
  generate_push_pull_balance_chart()— push:pull ratio trend
  generate_strength_trajectory_chart() — e1RM per lift with goal line
  generate_sleep_strength_scatter() — sleep hours vs next-day e1RM scatter
  generate_cardio_zones_chart()     — zone distribution pie + weekly bar
"""

from io import BytesIO
from typing import Optional

from config import KEY_LIFTS  # fallback when tracked_lifts not passed


def _get_plt():
    """Import matplotlib lazily (not needed on every run)."""
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend, safe for servers
    import matplotlib.pyplot as plt
    return plt


def generate_1rm_chart(lift_history: list[dict],
                        tracked_lifts: list[dict] = None) -> Optional[BytesIO]:
    """
    Generate a line chart of estimated 1RM over time for key lifts.
    Shows MAIN + AUXILIARY lifts. Falls back to KEY_LIFTS if tracked_lifts not provided.
    Returns a BytesIO PNG, or None if there's not enough data.
    """
    if tracked_lifts:
        lifts_to_chart = [(tl["domain"], tl["match_pattern"]) for tl in tracked_lifts
                          if tl.get("lift_type", "MAIN") in ("MAIN", "AUXILIARY")]
    else:
        lifts_to_chart = KEY_LIFTS

    lift_data: dict[str, list[tuple[str, float]]] = {}

    for row in lift_history:
        ex_name = row.get("Exercise", "")
        est = row.get("Est 1RM", "")
        date_str = row.get("Date", "")
        if not est or not date_str:
            continue
        for _domain, lift in lifts_to_chart:
            if lift.lower() in ex_name.lower():
                try:
                    lift_data.setdefault(lift, []).append((date_str, float(est)))
                except (ValueError, TypeError):
                    pass

    series = {k: v for k, v in lift_data.items() if len(v) >= 2}
    if not series:
        return None

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(7, 3.5))

    colors = ["#2563eb", "#dc2626", "#16a34a", "#d97706"]
    for (lift, points), color in zip(series.items(), colors):
        dates = [p[0] for p in points]
        values = [p[1] for p in points]
        ax.plot(dates, values, marker="o", markersize=4, label=lift,
                color=color, linewidth=1.8)

        # Only show a few x-axis labels to avoid clutter
    step = max(1, len(next(iter(series.values()))) // 6)
    sample_dates = next(iter(series.values()))[::step]
    ax.set_xticks([p[0] for p in sample_dates])
    ax.set_xticklabels([p[0][:10] for p in sample_dates], rotation=30, ha="right", fontsize=8)

    ax.set_ylabel("Est. 1RM (kg)", fontsize=9)
    ax.set_title("Estimated 1RM Trajectory", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_volume_chart(recent_weeks: list[dict], current_week: Optional[dict] = None) -> Optional[BytesIO]:
    """
    Generate a bar chart of completed sessions (exercise count) per week.
    Returns a BytesIO PNG, or None if there's not enough data.
    """
    weeks = list(recent_weeks)
    if current_week:
        weeks = weeks + [current_week]

    if len(weeks) < 2:
        return None

    labels = []
    counts = []
    for w in weeks:
        wn = w.get("week_num", "?")
        labels.append(f"Wk {wn}")
        done = sum(
            1 for day in w.get("days", [])
            for ex in day.get("exercises", [])
            if ex.get("done") is True
        )
        counts.append(done)

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(7, 3))

    bars = ax.bar(labels, counts, color="#2563eb", alpha=0.75, width=0.55)
    for bar, count in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1,
                str(count), ha="center", va="bottom", fontsize=8)

    ax.set_ylabel("Exercises completed", fontsize=9)
    ax.set_title("Weekly Training Volume (completed sets)", fontsize=11, fontweight="bold")
    ax.set_ylim(0, max(counts) * 1.25 + 1)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_bodyweight_chart(health_log: list[dict]) -> Optional[BytesIO]:
    """
    Generate a scatter + rolling average chart of bodyweight over time.
    Returns a BytesIO PNG, or None if there's not enough data.
    """
    points = []
    for entry in health_log:
        d = entry.get("Date", "")
        bw = entry.get("Bodyweight (kg)", "")
        if d and bw:
            try:
                points.append((d, float(str(bw).replace(",", "."))))
            except (ValueError, TypeError):
                pass

    if len(points) < 3:
        return None

    points.sort(key=lambda x: x[0])
    dates = [p[0] for p in points]
    weights = [p[1] for p in points]

    # 7-day rolling average (simple)
    window = 7
    rolling = []
    for i in range(len(weights)):
        window_vals = weights[max(0, i - window + 1):i + 1]
        rolling.append(sum(window_vals) / len(window_vals))

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(7, 3))

    ax.scatter(dates, weights, color="#6b7280", s=18, alpha=0.5, label="Daily")
    ax.plot(dates, rolling, color="#2563eb", linewidth=2, label="7-day avg")

    step = max(1, len(dates) // 6)
    ax.set_xticks(dates[::step])
    ax.set_xticklabels([d[:10] for d in dates[::step]], rotation=30, ha="right", fontsize=8)

    ax.set_ylabel("kg", fontsize=9)
    ax.set_title("Bodyweight Trend", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_rep_bucket_chart(
    volume_by_bucket: dict,
    last_n_weeks: int = 8,
) -> Optional[BytesIO]:
    """
    Stacked bar chart of weekly volume partitioned by rep bucket:
      Strength (1-5 reps), Hypertrophy (6-12), Endurance (13+).

    volume_by_bucket: output of strength_tracker.compute_volume_by_rep_bucket()
    Groups all motion groups together for a total weekly view.

    Returns BytesIO PNG or None if insufficient data.
    """
    sorted_weeks = sorted(volume_by_bucket.keys())[-last_n_weeks:]
    if len(sorted_weeks) < 2:
        return None

    # Aggregate tonnage per bucket per week (across all motion groups)
    strength_vals = []
    hypertrophy_vals = []
    endurance_vals = []

    for wk in sorted_weeks:
        s = h = e = 0.0
        for motion, buckets in volume_by_bucket.get(wk, {}).items():
            s += buckets.get("strength", {}).get("tonnage", 0)
            h += buckets.get("hypertrophy", {}).get("tonnage", 0)
            e += buckets.get("endurance", {}).get("tonnage", 0)
        # Convert to thousands for readability
        strength_vals.append(s / 1000)
        hypertrophy_vals.append(h / 1000)
        endurance_vals.append(e / 1000)

    if max(strength_vals + hypertrophy_vals + endurance_vals) == 0:
        return None

    labels = [wk[-3:] for wk in sorted_weeks]  # "W09", "W10", etc.

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(8, 4))

    ax.bar(labels, strength_vals,    color="#1d4ed8", label="Strength (1-5 reps)")
    ax.bar(labels, hypertrophy_vals, bottom=strength_vals,
           color="#16a34a", label="Hypertrophy (6-12 reps)")
    cumulative = [s + h for s, h in zip(strength_vals, hypertrophy_vals)]
    ax.bar(labels, endurance_vals, bottom=cumulative,
           color="#d97706", label="Endurance (13+)")

    ax.set_ylabel("Volume (tonnes)", fontsize=9)
    ax.set_title("Weekly Volume by Rep Bucket", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_push_pull_balance_chart(
    push_pull_balance: list,
    last_n_weeks: int = 8,
) -> Optional[BytesIO]:
    """
    Line chart of push:pull tonnage ratio per week.
    Reference line at 1.0 (equal) and shaded zone for healthy range (0.7-1.4).

    push_pull_balance: output of strength_tracker.compute_push_pull_balance()

    Returns BytesIO PNG or None if insufficient data.
    """
    data = [w for w in push_pull_balance if w.get("ratio") is not None]
    data = data[-last_n_weeks:]
    if len(data) < 2:
        return None

    weeks = [d["week"][-3:] for d in data]
    ratios = [d["ratio"] for d in data]
    colors = [
        "#ef4444" if r > 1.5 or r < 0.6 else "#22c55e"
        for r in ratios
    ]

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(7, 3.5))

    ax.plot(weeks, ratios, color="#2563eb", linewidth=2, marker="o", markersize=5)
    ax.axhspan(0.7, 1.4, alpha=0.08, color="#22c55e", label="Healthy range (0.7-1.4)")
    ax.axhline(1.0, color="#6b7280", linewidth=0.8, linestyle="--")

    # Color dots based on flag
    for i, (wk, r, c) in enumerate(zip(weeks, ratios, colors)):
        ax.plot(wk, r, "o", color=c, markersize=7, zorder=5)

    ax.set_ylabel("Push:Pull ratio", fontsize=9)
    ax.set_title("Weekly Push:Pull Volume Balance", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.set_ylim(0, max(ratios) * 1.3 + 0.3)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_strength_trajectory_chart(
    weekly_e1rm: dict,
    goals: dict = None,
    last_n_weeks: int = 12,
) -> Optional[BytesIO]:
    """
    Line chart of estimated 1RM per lift over time with goal lines.

    weekly_e1rm: output of strength_tracker.compute_weekly_e1rm()
    goals: {exercise_name: target_kg}

    Returns BytesIO PNG or None if insufficient data.
    """
    if not weekly_e1rm:
        return None

    # Filter to main lifts (squat, bench, deadlift, ohp) and take recent weeks
    key_exercises = ["squat", "bench press", "deadlift", "overhead press", "ohp"]
    series: dict = {}

    for exercise, weekly in weekly_e1rm.items():
        if any(k in exercise for k in key_exercises):
            sorted_weeks = sorted(weekly.keys())[-last_n_weeks:]
            if len(sorted_weeks) >= 3:
                series[exercise] = [(wk, weekly[wk]["e1rm"]) for wk in sorted_weeks]

    if not series:
        return None

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(8, 4))

    colors = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed"]
    for (exercise, points), color in zip(series.items(), colors):
        weeks_labels = [p[0][-3:] for p in points]
        values = [p[1] for p in points]
        ax.plot(weeks_labels, values, marker="o", markersize=4,
                label=exercise, color=color, linewidth=2)

        # Goal line
        if goals and exercise in goals:
            ax.axhline(goals[exercise], color=color, linewidth=1.0,
                       linestyle="--", alpha=0.5)
            # Label the goal line
            ax.text(0.98, goals[exercise], f"Goal {goals[exercise]}kg",
                    transform=ax.get_yaxis_transform(), ha="right",
                    va="bottom", fontsize=7, color=color, alpha=0.8)

    ax.set_ylabel("Estimated 1RM (kg)", fontsize=9)
    ax.set_title("Strength Trajectory", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_sleep_strength_scatter(
    health_log: list[dict],
    lift_history: list[dict],
    exercise_filter: str = "squat",
) -> Optional[BytesIO]:
    """
    Scatter plot: sleep hours (x) vs next-day e1RM (y) for a given exercise.
    Shows the sleep→strength relationship with a trend line.

    Returns BytesIO PNG or None if insufficient data (< 8 paired points).
    """
    from datetime import date as _date, timedelta

    # Build sleep dict
    sleep_by_date: dict = {}
    for entry in health_log:
        d = entry.get("Date") or entry.get("date") or ""
        raw = entry.get("Sleep (hrs)") or entry.get("sleep_hrs") or ""
        try:
            sleep_by_date[str(d)[:10]] = float(str(raw).replace(",", "."))
        except (ValueError, TypeError):
            pass

    # Build e1RM dict for exercise
    e1rm_by_date: dict = {}
    for entry in lift_history:
        ex_name = (entry.get("Exercise") or entry.get("exercise") or "").lower()
        if exercise_filter.lower() not in ex_name:
            continue
        d = str(entry.get("Date") or "")[:10]
        est = entry.get("Est 1RM") or ""
        if est:
            try:
                val = float(str(est).replace(",", "."))
                if d and val > 0:
                    e1rm_by_date[d] = max(e1rm_by_date.get(d, 0.0), val)
            except (ValueError, TypeError):
                pass

    # Pair sleep day X → e1RM day X+1
    pairs = []
    for sleep_date, sleep_hrs in sleep_by_date.items():
        try:
            next_d = str(_date.fromisoformat(sleep_date) + timedelta(days=1))
        except ValueError:
            continue
        if next_d in e1rm_by_date:
            pairs.append((sleep_hrs, e1rm_by_date[next_d]))

    if len(pairs) < 8:
        return None

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    # Linear trend line
    import statistics as _stats
    n = len(xs)
    mean_x, mean_y = _stats.mean(xs), _stats.mean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    den = sum((x - mean_x) ** 2 for x in xs)
    slope = num / den if den != 0 else 0
    intercept = mean_y - slope * mean_x

    x_range = [min(xs), max(xs)]
    trend_y = [slope * x + intercept for x in x_range]

    plt = _get_plt()
    fig, ax = plt.subplots(figsize=(6, 4))

    ax.scatter(xs, ys, color="#2563eb", alpha=0.6, s=30, label=f"Sessions (n={n})")
    ax.plot(x_range, trend_y, color="#dc2626", linewidth=1.5, label=f"Trend (slope={slope:.1f})")
    ax.axvline(7.5, color="#6b7280", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.text(7.5, ax.get_ylim()[0] * 1.01 + ax.get_ylim()[1] * 0.01,
            "7.5h target", fontsize=7, color="#6b7280")

    ax.set_xlabel("Sleep previous night (hrs)", fontsize=9)
    ax.set_ylabel(f"Est. 1RM — {exercise_filter} (kg)", fontsize=9)
    ax.set_title(f"Sleep → Next-day {exercise_filter.title()} Strength", fontsize=11, fontweight="bold")
    ax.legend(fontsize=8)
    ax.grid(linestyle="--", alpha=0.3)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_cardio_zones_chart(
    distribution: dict,
) -> Optional[BytesIO]:
    """
    Two-panel chart: zone distribution pie (left) + weekly zone breakdown bar (right).
    distribution: output of cardio_zones.compute_zone_distribution()

    Returns BytesIO PNG or None if no cardio data.
    """
    if not distribution or distribution.get("total_sessions", 0) == 0:
        return None

    zone_mins = distribution.get("total_zone_minutes", {})
    labels = ["Z1\nRecovery", "Z2\nAerobic", "Z3\nTempo", "Z4\nThreshold", "Z5\nVO2max"]
    values = [zone_mins.get(f"zone{i}", 0) for i in range(1, 6)]
    colors_z = ["#93c5fd", "#22c55e", "#facc15", "#f97316", "#ef4444"]

    # Filter zero slices for pie
    nonzero = [(l, v, c) for l, v, c in zip(labels, values, colors_z) if v > 0]
    if not nonzero:
        return None

    pie_labels, pie_values, pie_colors = zip(*nonzero)

    plt = _get_plt()
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))

    # Pie chart
    ax1.pie(
        pie_values,
        labels=pie_labels,
        colors=pie_colors,
        autopct="%1.0f%%",
        pctdistance=0.8,
        startangle=90,
        textprops={"fontsize": 8},
    )
    ax1.set_title("Zone distribution\n(total minutes)", fontsize=10, fontweight="bold")

    # Weekly zone averages bar
    weekly = distribution.get("weekly_zone_averages", {})
    weekly_vals = [weekly.get(f"zone{i}", 0) for i in range(1, 6)]
    ax2.bar(
        [f"Z{i}" for i in range(1, 6)],
        weekly_vals,
        color=colors_z,
        alpha=0.85,
    )
    # Target line for Zone 2
    ax2.axhline(90, color="#16a34a", linewidth=1.2, linestyle="--", alpha=0.7, label="Z2 target (90)")
    ax2.set_ylabel("Minutes per week", fontsize=9)
    ax2.set_title("Weekly zone averages", fontsize=10, fontweight="bold")
    ax2.legend(fontsize=8)
    ax2.grid(axis="y", linestyle="--", alpha=0.4)

    vo2max = distribution.get("vo2max_latest")
    trend = distribution.get("vo2max_trend", "")
    title_extra = f"  |  VO2max: {vo2max} ({trend})" if vo2max else ""
    fig.suptitle(f"Cardio Zones Analysis{title_extra}", fontsize=11, fontweight="bold")

    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf
