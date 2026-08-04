"""
Generates htx_dashboard.html - a self-contained, single-file dashboard for the HTX
hiring skills analysis. Reads htx_job_history.csv + htx_skills_cache.json (produced
by careers_gov_htx_crawler.py and skills_analysis.py).

All charts are rendered at build time as static HTML/SVG with ZERO JavaScript, so
the file displays everywhere - including Gmail's sandboxed attachment preview,
which blocks scripts (a JS-drawn dashboard shows up blank there).

Run directly or via daily_htx_report.py (which regenerates it each morning).

Usage:
    python build_dashboard.py
    python build_dashboard.py --history-file htx_job_history.csv --cache-file htx_skills_cache.json --output htx_dashboard.html
"""

import argparse
import html
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from division_normalize import resolve_divisions
from skill_normalize import resolve_displays, skill_key

DEFAULT_HISTORY_FILE = "htx_job_history.csv"
DEFAULT_CACHE_FILE = "htx_skills_cache.json"
DEFAULT_OUTPUT = "htx_dashboard.html"

LEVEL_ORDER = ["Engineer/Officer", "Senior/Lead", "Principal", "Manager",
               "Head", "Deputy Director", "Director & Above", "Unspecified"]

PALETTE = ["#5eead4", "#818cf8", "#fbbf24", "#fb7185", "#38bdf8", "#a3e635"]


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def month_bucket(date_str: str) -> str:
    if not date_str or not isinstance(date_str, str) or len(date_str) < 7:
        return "Unknown"
    return date_str[:7]


def parse_date(date_str: str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def build_weekly_trend(postings: list[dict], now: datetime, num_weeks: int = 12, top_n: int = 6) -> dict:
    """Weekly demand trend per skill category.

    A posting counts toward a week if its open window (posted..closing) overlaps
    that week, so the trend is derivable retroactively from posting dates rather
    than needing weeks of crawl snapshots.
    """
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weeks = [(this_monday - timedelta(weeks=i)) for i in range(num_weeks - 1, -1, -1)]

    dated = []
    for p in postings:
        if not p["skills"]:
            continue
        start = parse_date(p["posted"])
        end = parse_date(p["closing"]) or now  # undated = still open
        if not start:
            continue
        dated.append((start, end, {s["category"] for s in p["skills"]}))

    weekly_counts = []
    for wk_start in weeks:
        wk_end = wk_start + timedelta(days=7)
        counts = Counter()
        for start, end, cats in dated:
            if start < wk_end and end >= wk_start:
                for c in cats:
                    counts[c] += 1
        weekly_counts.append((wk_start, counts))
    while weekly_counts and not weekly_counts[0][1]:
        weekly_counts.pop(0)

    totals = Counter()
    for _, counts in weekly_counts:
        totals.update(counts)
    top_cats = [c for c, _ in totals.most_common(top_n)]

    return {
        "weeks": [wk.strftime("%d %b") for wk, _ in weekly_counts],
        "series": [{"category": c, "values": [counts.get(c, 0) for _, counts in weekly_counts]}
                   for c in top_cats],
    }


def build_concentration(skill_counts: Counter, skill_display: dict, n_analyzed: int) -> dict:
    """Split the skill vocabulary into the recurring core and the long tail.

    Most distinct skills are named in a single posting - a mix of genuinely
    niche tools and one-off phrasings of common ideas. Counting them together
    makes "distinct skills" look like breadth of demand when it mostly measures
    how verbosely JDs are written, so the two are reported separately.
    """
    if not n_analyzed:
        return {"distinct": 0, "core": [], "buckets": [], "threshold": 0}
    thr = max(3, math.ceil(0.10 * n_analyzed))
    core = [{"skill": skill_display.get(k, k), "count": v, "share": v / n_analyzed}
            for k, v in skill_counts.most_common() if v >= thr]
    buckets = [
        {"label": f"Core - in {thr}+ postings (10%+)", "count": len(core)},
        {"label": f"Recurring - 3 to {thr - 1} postings",
         "count": sum(1 for v in skill_counts.values() if 3 <= v < thr)},
        {"label": "Occasional - 2 postings",
         "count": sum(1 for v in skill_counts.values() if v == 2)},
        {"label": "One-off - 1 posting",
         "count": sum(1 for v in skill_counts.values() if v == 1)},
    ]
    return {"distinct": len(skill_counts), "core": core,
            "buckets": [b for b in buckets if b["count"] or b["label"].startswith("Core")],
            "threshold": thr}


def build_payload(history_df: pd.DataFrame, cache: dict) -> dict:
    now = datetime.now(tz=timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # "Active" means present in the most recent crawl: agencies delist postings
    # early (filled/withdrawn) while their closing date is still in the future,
    # so closing-date-based counting overstates. Fall back to closing date only
    # for histories without a Last Seen column.
    last_seen_col = history_df["Last Seen"] if "Last Seen" in history_df.columns else None
    latest_seen = last_seen_col.max() if last_seen_col is not None else ""

    # Job titles name the org unit at inconsistent depth ("xCDI" vs
    # "Mobile Core & Transmission, xCDI"), so fold the variants together before
    # counting - otherwise one division is counted as several.
    div_display = resolve_divisions(
        Counter(cache.get(str(j), {}).get("division", "Unspecified")
                for j in history_df["Job ID"].astype(str)))

    postings = []
    for _, row in history_df.iterrows():
        jid = str(row["Job ID"])
        entry = cache.get(jid, {})
        closing = str(row.get("Closing Date", ""))
        if latest_seen:
            is_active = str(row.get("Last Seen", "")) == latest_seen
        else:
            is_active = (not closing) or not closing[:4].isdigit() or closing >= today_str
        postings.append({
            "id": jid,
            "title": row.get("Job Title", ""),
            "posted": str(row.get("Date Posted", "")),
            "closing": closing,
            "month": month_bucket(str(row.get("Date Posted", ""))),
            "division": div_display.get(entry.get("division", "Unspecified"), "Unspecified"),
            "band": entry.get("job_level_band", "Unspecified"),
            # No closing date = open-until-filled = active
            "active": is_active,
            "skills": entry.get("skills", []),
        })

    analyzed = [p for p in postings if p["skills"]]

    cat_counts = Counter()
    for p in analyzed:
        for c in {s["category"] for s in p["skills"]}:
            cat_counts[c] += 1

    skill_counts = Counter()   # keyed by normalized skill_key, counts distinct postings
    skill_cat = {}
    raw_counter = Counter()    # every mention, for choosing the display spelling
    for p in analyzed:
        seen = set()
        for s in p["skills"]:
            raw_counter[s["skill"].strip()] += 1
            key = skill_key(s["skill"])
            if key in seen:
                continue
            seen.add(key)
            skill_counts[key] += 1
            skill_cat.setdefault(key, s["category"])
    skill_display = resolve_displays(raw_counter)
    top_skills = [{"skill": skill_display.get(k, k), "category": skill_cat[k], "count": v}
                  for k, v in skill_counts.most_common(20)]

    concentration = build_concentration(skill_counts, skill_display, len(analyzed))

    div_counts = Counter(p["division"] for p in analyzed)
    top_divisions = [d for d, _ in div_counts.most_common(10)]
    matrix_cats = [c for c, _ in cat_counts.most_common(8)]
    div_cat = defaultdict(lambda: defaultdict(int))
    for p in analyzed:
        if p["division"] not in top_divisions:
            continue
        for c in {s["category"] for s in p["skills"]}:
            if c in matrix_cats:
                div_cat[p["division"]][c] += 1
    matrix = {"divisions": top_divisions, "categories": matrix_cats,
              "cells": [[div_cat[d][c] for c in matrix_cats] for d in top_divisions]}

    band_counts = Counter(p["band"] for p in analyzed)
    levels = [{"band": b, "count": band_counts.get(b, 0)} for b in LEVEL_ORDER if band_counts.get(b, 0)]

    month_counts = Counter(p["month"] for p in postings if p["month"] != "Unknown")
    monthly = [{"month": m, "count": month_counts[m]} for m in sorted(month_counts)]

    seven_days = [p for p in postings if p["active"] and parse_date(p["closing"]) and
                  0 <= (parse_date(p["closing"]) - now).days < 7]

    return {
        "generated": now.strftime("%d %b %Y, %H:%M UTC"),
        "kpis": {
            "active": sum(1 for p in postings if p["active"]),
            "tracked": len(postings),
            "analyzed": len(analyzed),
            "distinctSkills": len(skill_counts),
            "coreSkills": len(concentration["core"]),
            "divisions": sum(1 for v in div_counts.values() if v >= 2),
            "closingSoon": len(seven_days),
        },
        "concentration": concentration,
        "categories": [{"category": c, "count": v} for c, v in cat_counts.most_common()],
        "topSkills": top_skills,
        "divisions": [{"division": d, "count": div_counts[d]} for d in top_divisions],
        "matrix": matrix,
        "levels": levels,
        "monthly": monthly,
        "weeklyTrend": build_weekly_trend(postings, now),
    }


# ── Static renderers (no JavaScript anywhere) ─────────────────────────────────

def render_kpis(kpis: dict) -> str:
    defs = [("active", "Active Postings", ""), ("tracked", "Postings Tracked", " alt"),
            ("coreSkills", "Core Skills", ""), ("divisions", "Divisions, 2+ Roles", " alt"),
            ("closingSoon", "Closing In 7 Days", " warn")]
    out = []
    for key, label, mod in defs:
        out.append(f'<div class="kpi{mod}"><div class="v">{kpis[key]}</div><div class="l">{label}</div></div>')
    return "".join(out)


def render_bars(rows: list[dict], name_key: str, count_key: str, sub_key: str | None = None) -> str:
    if not rows:
        return '<div class="sub">No data yet.</div>'
    max_v = max((r[count_key] for r in rows), default=1) or 1
    out = []
    for r in rows:
        sub = f'<span class="pill">{esc(r[sub_key])}</span>' if sub_key and r.get(sub_key) else ""
        width = 100 * r[count_key] / max_v
        out.append(
            f'<div class="bar-row"><div class="name">{esc(r[name_key])}{sub}</div>'
            f'<div class="track"><div class="fill" style="width:{width:.1f}%"></div></div>'
            f'<div class="num">{r[count_key]}</div></div>'
        )
    return "".join(out)


def render_trend(trend: dict) -> str:
    weeks, series = trend["weeks"], trend["series"]
    if not weeks or not series:
        return '<div class="sub">Not enough dated postings yet - the trend fills in as the daily crawl accumulates.</div>'

    W, H, padL, padR, padT, padB = 1200, 240, 34, 14, 12, 26
    max_v = max((v for s in series for v in s["values"]), default=1) or 1

    def x(i):
        return padL + (W - padL - padR) * (0.5 if len(weeks) == 1 else i / (len(weeks) - 1))

    def y(v):
        return H - padB - (H - padT - padB) * v / max_v

    parts = []
    grid_n = 4
    for i in range(grid_n + 1):
        v = round(max_v * i / grid_n)
        yy = y(v)
        parts.append(f'<line class="grid" x1="{padL}" y1="{yy:.1f}" x2="{W - padR}" y2="{yy:.1f}"/>')
        parts.append(f'<text x="{padL - 6}" y="{yy + 3:.1f}" text-anchor="end">{v}</text>')
    for i, w in enumerate(weeks):
        if len(weeks) <= 12 or i % 2 == 0:
            parts.append(f'<text x="{x(i):.1f}" y="{H - 8}" text-anchor="middle">{esc(w)}</text>')
    for si, s in enumerate(series):
        color = PALETTE[si % len(PALETTE)]
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(s["values"]))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5" '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
        for i, v in enumerate(s["values"]):
            parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3" fill="{color}"/>')

    legend = "".join(
        f'<div class="li"><div class="sw" style="background:{PALETTE[si % len(PALETTE)]}"></div>'
        f'<span>{esc(s["category"])}</span></div>'
        for si, s in enumerate(series)
    )
    svg = (f'<svg class="trendsvg" viewBox="0 0 {W} {H}" width="100%" height="240" '
           f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')
    return f'<div class="legend">{legend}</div>{svg}'


def render_concentration(conc: dict) -> str:
    if not conc["core"]:
        return '<div class="sub">Not enough analysed postings yet.</div>'
    rows = []
    for b in conc["buckets"]:
        pct = b["count"] / conc["distinct"] if conc["distinct"] else 0
        rows.append(
            f'<div class="bar-row"><div class="name">{esc(b["label"])}</div>'
            f'<div class="track"><div class="fill" style="width:{max(2, pct * 100):.1f}%"></div></div>'
            f'<div class="num">{b["count"]}</div></div>')
    tail = sum(b["count"] for b in conc["buckets"] if b["label"].startswith(("Occasional", "One-off")))
    note = (f'<div class="sub" style="margin-top:10px">'
            f'{len(conc["core"])} core skills carry the recurring demand signal. '
            f'The other {conc["distinct"] - len(conc["core"])} of {conc["distinct"]} distinct skills '
            f'appear rarely &mdash; {tail} in only one or two postings &mdash; a mix of genuinely niche '
            f'tools and one-off phrasings, so treat &ldquo;distinct skills&rdquo; as vocabulary breadth '
            f'rather than demand.</div>')
    return "".join(rows) + note


def render_core_skills(conc: dict) -> str:
    if not conc["core"]:
        return '<div class="sub">Not enough analysed postings yet.</div>'
    rows = []
    for c in conc["core"][:16]:
        rows.append(
            f'<div class="bar-row"><div class="name">{esc(c["skill"])}</div>'
            f'<div class="track"><div class="fill" style="width:{c["share"] * 100:.1f}%"></div></div>'
            f'<div class="num">{c["share"] * 100:.0f}%</div></div>')
    return "".join(rows)


def render_monthly(monthly: list[dict]) -> str:
    if not monthly:
        return '<div class="sub">No data yet.</div>'
    max_v = max((m["count"] for m in monthly), default=1) or 1
    out = []
    for m in monthly:
        h = max(6, 130 * m["count"] / max_v)
        out.append(f'<div class="col"><div class="cv">{m["count"]}</div>'
                   f'<div class="stick" style="height:{h:.0f}px"></div>'
                   f'<div class="cl">{esc(m["month"])}</div></div>')
    return f'<div class="cols">{"".join(out)}</div>'


def render_heatmap(matrix: dict) -> str:
    divisions, categories, cells = matrix["divisions"], matrix["categories"], matrix["cells"]
    if not divisions:
        return '<div class="sub">No data yet.</div>'
    flat = [v for row in cells for v in row]
    max_v = max(flat, default=1) or 1
    head = "<tr><th></th>" + "".join(
        f"<th>{esc(' '.join(c.split(' ')[:3]))}</th>" for c in categories) + "</tr>"
    body = []
    for i, d in enumerate(divisions):
        tds = []
        for j, _ in enumerate(categories):
            v = cells[i][j]
            a = v / max_v
            bg = "#0e1428" if v == 0 else f"rgba(94,234,212,{0.12 + 0.55 * a:.2f})"
            fg = "#06281f" if a > 0.55 else "#e8ecf6"
            tds.append(f'<td><div class="cell" style="background:{bg};color:{fg}">{v if v else ""}</div></td>')
        body.append(f'<tr><th class="rowh">{esc(d)}</th>{"".join(tds)}</tr>')
    return f'<table class="hm">{head}{"".join(body)}</table>'


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  :root {{
    --bg: #0b1020; --panel: #131a30; --panel2: #182138; --line: #232e4d;
    --text: #e8ecf6; --muted: #8b96b3; --accent: #5eead4; --accent2: #818cf8;
    --gold: #fbbf24; --rose: #fb7185;
    --font: "Segoe UI", system-ui, -apple-system, sans-serif;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: radial-gradient(1200px 600px at 20% -10%, #1a2342 0%, var(--bg) 55%); background-color: var(--bg); color: var(--text); font-family: var(--font); min-height: 100vh; padding: 28px 32px 48px; }}
  header {{ display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 22px; }}
  h1 {{ font-size: 1.45rem; font-weight: 650; letter-spacing: .2px; }}
  h1 .x {{ color: var(--accent); }}
  .sub {{ color: var(--muted); font-size: .8rem; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 22px; }}
  .kpi {{ background: linear-gradient(160deg, var(--panel2), var(--panel)); background-color: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 16px 18px; }}
  .kpi .v {{ font-size: 1.75rem; font-weight: 700; color: var(--accent); }}
  .kpi.alt .v {{ color: var(--accent2); }}
  .kpi.warn .v {{ color: var(--gold); }}
  .kpi .l {{ color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .8px; margin-top: 3px; }}
  .grid {{ display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }}
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; }}
  .card h2 {{ font-size: .85rem; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 14px; }}
  .span6 {{ grid-column: span 6; }} .span4 {{ grid-column: span 4; }} .span12 {{ grid-column: span 12; }}
  @media (max-width: 1000px) {{ .span6, .span4 {{ grid-column: span 12; }} }}
  .bar-row {{ display: grid; grid-template-columns: 210px 1fr 34px; align-items: center; gap: 10px; margin-bottom: 8px; font-size: .82rem; }}
  .bar-row .name {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text); }}
  .bar-row .track {{ background: #0e1428; border-radius: 6px; height: 18px; overflow: hidden; }}
  .bar-row .fill {{ height: 100%; border-radius: 6px; background: linear-gradient(90deg, var(--accent2), var(--accent)); background-color: var(--accent); min-width: 2px; }}
  .bar-row .num {{ text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }}
  .pill {{ display: inline-block; font-size: .62rem; padding: 1px 8px; border-radius: 999px; background: #1e2a4a; color: var(--muted); margin-left: 6px; vertical-align: middle; }}
  table.hm {{ border-collapse: collapse; width: 100%; font-size: .74rem; }}
  table.hm th {{ color: var(--muted); font-weight: 500; padding: 6px 8px; text-align: center; }}
  table.hm th.rowh {{ text-align: right; white-space: nowrap; max-width: 190px; overflow: hidden; text-overflow: ellipsis; }}
  table.hm td {{ padding: 0; }}
  table.hm td .cell {{ margin: 2px; border-radius: 6px; height: 30px; display: flex; align-items: center; justify-content: center; font-variant-numeric: tabular-nums; }}
  .cols {{ display: flex; align-items: flex-end; gap: 14px; height: 180px; padding-top: 8px; }}
  .col {{ flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px; height: 100%; justify-content: flex-end; }}
  .col .stick {{ width: 70%; max-width: 64px; border-radius: 8px 8px 3px 3px; background: linear-gradient(180deg, var(--accent), #14b8a6); background-color: var(--accent); }}
  .col .cv {{ font-size: .8rem; color: var(--text); font-variant-numeric: tabular-nums; }}
  .col .cl {{ font-size: .68rem; color: var(--muted); text-align: center; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 10px; font-size: .72rem; color: var(--muted); }}
  .legend .li {{ display: flex; align-items: center; gap: 6px; }}
  .legend .sw {{ width: 18px; height: 3px; border-radius: 2px; }}
  .trendsvg text {{ fill: #8b96b3; font-size: 10px; font-family: var(--font); }}
  .trendsvg .grid {{ stroke: #232e4d; stroke-width: 1; }}
  footer {{ margin-top: 26px; color: var(--muted); opacity: .7; font-size: .72rem; text-align: center; }}
</style>
</head>
<body>
<header>
  <h1>{title} <span class="pill">Careers@Gov</span></h1>
  <div class="sub">Generated {generated} &middot; auto-refreshed daily at 8:00 AM</div>
</header>

<div class="kpis">{kpis}</div>

<div class="grid">
  <div class="card span12"><h2>Weekly Skills Demand Trend <span class="pill">postings open each week, top 6 categories</span></h2>{trend}</div>
  <div class="card span6"><h2>Skill Category Demand <span class="pill">distinct postings</span></h2>{cats}</div>
  <div class="card span6"><h2>Top Skills <span class="pill">top 20</span></h2><div style="max-height:420px;overflow-y:auto">{skills}</div></div>
  <div class="card span6"><h2>Core Skills <span class="pill">share of postings</span></h2>{core}</div>
  <div class="card span6"><h2>Skill Concentration <span class="pill">core vs tail</span></h2>{concentration}</div>
  <div class="card span4"><h2>Postings by Division <span class="pill">top 10</span></h2>{divs}</div>
  <div class="card span4"><h2>Job Level Mix</h2>{levels}</div>
  <div class="card span4"><h2>Monthly Posting Volume</h2>{monthly}</div>
  <div class="card span12"><h2>Division &times; Skill Category Heatmap <span class="pill">postings needing category</span></h2><div style="overflow-x:auto">{heatmap}</div></div>
</div>

<footer>Postings from jobs.careers.gov.sg &middot; skills extracted &amp; categorised by Claude &middot; static single-file dashboard, no scripts</footer>
</body>
</html>
"""


def render_html(payload: dict, title: str) -> str:
    return HTML_TEMPLATE.format(
        title=esc(title),
        generated=esc(payload["generated"]),
        kpis=render_kpis(payload["kpis"]),
        trend=render_trend(payload["weeklyTrend"]),
        cats=render_bars(payload["categories"], "category", "count"),
        skills=render_bars(payload["topSkills"], "skill", "count", "category"),
        divs=render_bars(payload["divisions"], "division", "count"),
        levels=render_bars(payload["levels"], "band", "count"),
        monthly=render_monthly(payload["monthly"]),
        core=render_core_skills(payload["concentration"]),
        concentration=render_concentration(payload["concentration"]),
        heatmap=render_heatmap(payload["matrix"]),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-file", default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--cache-file", default=DEFAULT_CACHE_FILE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--agency-name", default="Agency",
                        help="Agency display name used in the dashboard title")
    args = parser.parse_args()

    if not os.path.exists(args.history_file):
        print(f"History file {args.history_file} not found. Run careers_gov_htx_crawler.py first.")
        return 1

    history_df = pd.read_csv(args.history_file, dtype=str).fillna("")
    cache = {}
    if os.path.exists(args.cache_file):
        with open(args.cache_file, "r", encoding="utf-8") as f:
            cache = json.load(f)

    payload = build_payload(history_df, cache)
    out_html = render_html(payload, f"{args.agency_name} Hiring Skills Dashboard")
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(out_html)
    print(f"Wrote dashboard to {args.output} "
          f"({payload['kpis']['analyzed']} analyzed postings, {payload['kpis']['distinctSkills']} distinct skills, static/no-JS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
