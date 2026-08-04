"""
Cross-agency comparison report for Careers@Gov hiring pipelines.

Reads the per-agency history CSVs + skills caches produced by daily_report.py
runs sharing one workdir, and builds a side-by-side comparison: hiring volume
trend, skill-category demand (share of postings, so small and large agencies
are comparable), top skills, and job-level mix.

Outputs (in workdir):
  comparison_dashboard.html  - standalone dashboard (trend chart embedded as
                               base64 data URI; no JavaScript)
  comparison_trend.png       - weekly open-postings trend, one line per agency
Email: same dashboard rendered as the email body (email-safe HTML, trend via
CID image), standalone HTML attached. Uses SMTP_USER / SMTP_APP_PASSWORD /
REPORT_EMAIL_TO from <workdir>/.env, like daily_report.py.

Usage:
    python weekly_comparison_report.py --agencies "htx:HTX,govtech:GovTech,dsta:DSTA" \
        --workdir C:/path/to/project [--no-email]
"""

import argparse
import base64
import html
import json
import os
import smtplib
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from skill_normalize import resolve_displays, skill_key

LEVEL_ORDER = ["Engineer/Officer", "Senior/Lead", "Principal", "Manager",
               "Head", "Deputy Director", "Director & Above", "Unspecified"]
PALETTE = ["#5eead4", "#818cf8", "#fbbf24", "#fb7185", "#38bdf8", "#a3e635"]

BG = "#0b1020"
PANEL = "#131a30"
TEXT = "#e8ecf6"
MUTED = "#8b96b3"
ACCENT = "#5eead4"
ROSE = "#fb7185"
CELL_BG = "#0e1428"
LINE = "#232e4d"
FONT = "'Segoe UI',system-ui,Arial,sans-serif"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def _blend(c1: str, c2: str, t: float) -> str:
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def parse_date(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load_agency(workdir: Path, prefix: str, name: str, now: datetime) -> dict:
    history = pd.read_csv(workdir / f"{prefix}_job_history.csv", dtype=str).fillna("")
    cache_path = workdir / f"{prefix}_skills_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    latest_seen = history["Last Seen"].max() if "Last Seen" in history.columns else ""
    active = (history["Last Seen"] == latest_seen).sum() if latest_seen else len(history)

    cat_counts = Counter()
    skill_counts = Counter()
    raw_counter = Counter()
    band_counts = Counter()
    analyzed = 0
    for jid in history["Job ID"].astype(str):
        entry = cache.get(jid)
        if not entry or not entry.get("skills"):
            continue
        analyzed += 1
        for c in {s["category"] for s in entry["skills"]}:
            cat_counts[c] += 1
        for s in entry["skills"]:
            raw_counter[s["skill"].strip()] += 1
        for key in {skill_key(s["skill"]) for s in entry["skills"]}:
            skill_counts[key] += 1
        band_counts[entry.get("job_level_band", "Unspecified")] += 1
    skill_display = resolve_displays(raw_counter)

    # Weekly open-postings series: a posting counts toward each week its
    # posted -> (closing | last-seen | now) window overlaps. Each window also
    # carries the posting's skill keys so skill demand can be sliced by week.
    windows = []
    for _, row in history.iterrows():
        start = parse_date(row.get("Date Posted"))
        if not start:
            continue
        end = parse_date(row.get("Closing Date"))
        if not end:
            last_seen = str(row.get("Last Seen", ""))
            end = now if last_seen == latest_seen else (parse_date(last_seen) or now)
        entry = cache.get(str(row["Job ID"]), {})
        keys = frozenset(skill_key(s["skill"]) for s in entry.get("skills", []))
        windows.append((start, end, keys))

    return {
        "name": name,
        "prefix": prefix,
        "tracked": len(history),
        "active": int(active),
        "analyzed": analyzed,
        "distinct_skills": len(skill_counts),
        "cat_counts": cat_counts,
        "top_skills": [(skill_display.get(k, k), v) for k, v in skill_counts.most_common(10)],
        "skill_display": skill_display,
        "band_counts": band_counts,
        "windows": windows,
    }


def weekly_series(agencies: list[dict], now: datetime, num_weeks: int = 12):
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weeks = [(this_monday - timedelta(weeks=i)) for i in range(num_weeks - 1, -1, -1)]
    labels = [w.strftime("%d %b") for w in weeks]
    series = []
    for a in agencies:
        vals = []
        for wk in weeks:
            wk_end = wk + timedelta(days=7)
            vals.append(sum(1 for start, end, _ in a["windows"] if start < wk_end and end >= wk))
        series.append({"name": a["name"], "values": vals})
    # Trim leading all-zero weeks
    while labels and all(s["values"][0] == 0 for s in series):
        labels.pop(0)
        for s in series:
            s["values"].pop(0)
    return labels, series


def render_trend_png(labels, series, output: Path):
    fig = plt.figure(figsize=(10.5, 3.4), dpi=130)
    fig.patch.set_facecolor(BG)
    ax = fig.add_subplot(111)
    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_color(LINE)
    ax.tick_params(colors=MUTED, labelsize=8)
    x = np.arange(len(labels))
    for i, s in enumerate(series):
        ax.plot(x, s["values"], marker="o", markersize=4, linewidth=2.2,
                color=PALETTE[i % len(PALETTE)], label=s["name"], zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, color=MUTED)
    ax.grid(axis="y", color=LINE, linewidth=0.6, zorder=0)
    ax.legend(loc="upper left", fontsize=8, facecolor=PANEL, edgecolor=LINE, labelcolor=TEXT)
    fig.subplots_adjust(left=0.05, right=0.99, top=0.96, bottom=0.13)
    fig.savefig(output, facecolor=BG)
    plt.close(fig)


def skills_shift(agency: dict, now: datetime, weeks_back: int = 2, top_n: int = 4,
                 min_postings: int = 5) -> dict:
    """Week-over-week movement in skill demand for one agency.

    Compares the share of currently-open postings requiring each skill against
    the share `weeks_back` weeks ago. Share (not raw count) is used so a change
    reflects a genuine shift in what is being asked for, rather than the book
    simply growing or shrinking.
    """
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    past_monday = this_monday - timedelta(weeks=weeks_back)

    def slice_week(wk_start):
        wk_end = wk_start + timedelta(days=7)
        open_now = [keys for start, end, keys in agency["windows"]
                    if start < wk_end and end >= wk_start]
        counts = Counter()
        for keys in open_now:
            counts.update(keys)
        return len(open_now), counts

    n_now, c_now = slice_week(this_monday)
    n_past, c_past = slice_week(past_monday)

    if n_now < min_postings or n_past < min_postings:
        return {"ok": False, "n_now": n_now, "n_past": n_past,
                "weeks_back": weeks_back, "risers": [], "fallers": []}

    deltas = []
    for key in set(c_now) | set(c_past):
        cn, cp = c_now.get(key, 0), c_past.get(key, 0)
        # Ignore skills that are rare in both periods - their swings are noise.
        if cn < 3 and cp < 3:
            continue
        deltas.append({
            "key": key,
            "name": agency["skill_display"].get(key, key),
            "now": cn / n_now, "past": cp / n_past,
            "delta": cn / n_now - cp / n_past,
            "n_now": cn, "n_past": cp,
        })
    deltas.sort(key=lambda d: d["delta"], reverse=True)

    # A move must clear 3pp AND be backed by at least 2 postings. In a small
    # book a single posting is worth several pp, so a share threshold alone
    # would dress up one req as a hiring trend.
    def material(d):
        return abs(d["delta"]) >= 0.03 and abs(d["n_now"] - d["n_past"]) >= 2

    risers = [d for d in deltas if d["delta"] > 0 and material(d)][:top_n]
    fallers = [d for d in deltas if d["delta"] < 0 and material(d)][-top_n:][::-1]
    return {"ok": True, "n_now": n_now, "n_past": n_past,
            "weeks_back": weeks_back, "risers": risers, "fallers": fallers}


def render_skills_shift(agencies: list[dict], now: datetime) -> str:
    cols = []
    width = 100 // max(len(agencies), 1)
    for i, a in enumerate(agencies):
        sh = skills_shift(a, now)
        if not sh["ok"]:
            body = (f'<div style="color:{MUTED};font:11px {FONT}">Not enough comparable '
                    f'history yet ({sh["n_now"]} open now vs {sh["n_past"]} then).</div>')
        else:
            rows = []
            for d in sh["risers"]:
                rows.append(
                    f'<tr><td style="color:{ACCENT};font:11px {FONT};padding:2px 4px 2px 0">&#9650;</td>'
                    f'<td style="color:{TEXT};font:11px {FONT};padding:2px 6px 2px 0">{esc(d["name"])}</td>'
                    f'<td align="right" style="color:{ACCENT};font:600 11px {FONT}">'
                    f'+{d["delta"]*100:.0f}pp</td>'
                    f'<td align="right" style="color:{MUTED};font:10px {FONT};padding-left:6px">'
                    f'{d["past"]*100:.0f}&#8594;{d["now"]*100:.0f}%</td></tr>')
            for d in sh["fallers"]:
                rows.append(
                    f'<tr><td style="color:{ROSE};font:11px {FONT};padding:2px 4px 2px 0">&#9660;</td>'
                    f'<td style="color:{TEXT};font:11px {FONT};padding:2px 6px 2px 0">{esc(d["name"])}</td>'
                    f'<td align="right" style="color:{ROSE};font:600 11px {FONT}">'
                    f'{d["delta"]*100:.0f}pp</td>'
                    f'<td align="right" style="color:{MUTED};font:10px {FONT};padding-left:6px">'
                    f'{d["past"]*100:.0f}&#8594;{d["now"]*100:.0f}%</td></tr>')
            if not rows:
                rows.append(f'<tr><td colspan="4" style="color:{MUTED};font:11px {FONT}">'
                            f'Skills mix stable &#8212; no move cleared 3pp.</td></tr>')
            body = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                    f'{"".join(rows)}</table>'
                    f'<div style="color:{MUTED};font:10px {FONT};padding-top:6px">'
                    f'{sh["n_past"]} &#8594; {sh["n_now"]} open postings</div>')
        cols.append(
            f'<td valign="top" width="{width}%" style="padding:0 6px">'
            f'<div style="color:{PALETTE[i]};font:700 13px {FONT};padding-bottom:6px">{esc(a["name"])}</div>'
            f'{body}</td>')
    note = (f'<div style="color:{MUTED};font:10px {FONT};padding-top:10px">'
            f'Change in the share of an agency&#39;s open postings requiring each skill, '
            f'this week vs 2 weeks ago (pp = percentage points). Share is used so growth in '
            f'the overall book does not read as a skills shift. A move must clear 3pp and be '
            f'backed by at least 2 postings, so single-req noise is filtered out.</div>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr>{"".join(cols)}</tr></table>{note}')


def section(title: str, body: str) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{PANEL};border-radius:12px;margin:0 0 14px 0">'
            f'<tr><td style="padding:14px 16px">'
            f'<div style="color:{MUTED};font:600 11px {FONT};letter-spacing:1px;'
            f'text-transform:uppercase;padding-bottom:10px">{title}</div>{body}</td></tr></table>')


def render_kpi_table(agencies: list[dict]) -> str:
    head = ('<tr><td></td>' + "".join(
        f'<td align="center" style="color:{PALETTE[i]};font:700 14px {FONT};padding:4px">{esc(a["name"])}</td>'
        for i, a in enumerate(agencies)) + "</tr>")
    rows = []
    for label, key in [("Active postings", "active"), ("Postings tracked", "tracked"),
                       ("Analyzed", "analyzed"), ("Distinct skills", "distinct_skills")]:
        tds = "".join(f'<td align="center" style="color:{TEXT};font:600 16px {FONT};padding:6px">{a[key]}</td>'
                      for a in agencies)
        rows.append(f'<tr><td style="color:{MUTED};font:12px {FONT};padding:6px 8px 6px 0">{label}</td>{tds}</tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{"".join(rows)}</table>'


def render_category_matrix(agencies: list[dict]) -> str:
    totals = Counter()
    for a in agencies:
        totals.update(a["cat_counts"])
    cats = [c for c, _ in totals.most_common(10)]
    head = ('<tr><td></td>' + "".join(
        f'<td align="center" style="color:{MUTED};font:11px {FONT};padding:4px">{esc(a["name"])}</td>'
        for a in agencies) + "</tr>")
    rows = []
    for c in cats:
        tds = []
        for a in agencies:
            n = a["cat_counts"].get(c, 0)
            share = n / a["analyzed"] if a["analyzed"] else 0
            t = 0 if n == 0 else 0.12 + 0.55 * share
            bg = CELL_BG if n == 0 else _blend(CELL_BG, ACCENT, min(t, 0.67))
            fg = "#06281f" if t > 0.45 else TEXT
            label = f"{share:.0%} ({n})" if n else ""
            tds.append(f'<td align="center" style="background:{bg};color:{fg};font:11px {FONT};'
                       f'padding:7px 4px;border-radius:4px">{label}</td>')
        rows.append(f'<tr><td style="color:{TEXT};font:11px {FONT};padding:2px 8px 2px 0;'
                    f'white-space:nowrap">{esc(c)}</td>{"".join(tds)}</tr>')
    note = (f'<div style="color:{MUTED};font:10px {FONT};padding-top:8px">Cell = share of the agency\'s '
            f'analyzed postings needing the category (count in brackets). Shares make agencies of '
            f'different sizes comparable.</div>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="2">'
            f'{head}{"".join(rows)}</table>{note}')


def render_top_skills_columns(agencies: list[dict]) -> str:
    cols = []
    for i, a in enumerate(agencies):
        items = "".join(
            f'<tr><td style="color:{TEXT};font:12px {FONT};padding:2px 6px 2px 0">{r}. {esc(s)}</td>'
            f'<td align="right" style="color:{MUTED};font:12px {FONT}">{n}</td></tr>'
            for r, (s, n) in enumerate(a["top_skills"], 1))
        cols.append(
            f'<td valign="top" width="{100 // len(agencies)}%" style="padding:0 6px">'
            f'<div style="color:{PALETTE[i]};font:700 13px {FONT};padding-bottom:6px">{esc(a["name"])}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{items}</table></td>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{"".join(cols)}</tr></table>'


def render_level_matrix(agencies: list[dict]) -> str:
    bands = [b for b in LEVEL_ORDER if any(a["band_counts"].get(b) for a in agencies)]
    head = ('<tr><td></td>' + "".join(
        f'<td align="center" style="color:{MUTED};font:11px {FONT};padding:4px">{esc(a["name"])}</td>'
        for a in agencies) + "</tr>")
    rows = []
    for b in bands:
        tds = []
        for a in agencies:
            n = a["band_counts"].get(b, 0)
            share = n / a["analyzed"] if a["analyzed"] else 0
            t = 0 if n == 0 else 0.12 + 0.55 * share
            bg = CELL_BG if n == 0 else _blend(CELL_BG, "#818cf8", min(t, 0.67))
            label = f"{share:.0%} ({n})" if n else ""
            tds.append(f'<td align="center" style="background:{bg};color:{TEXT};font:11px {FONT};'
                       f'padding:7px 4px;border-radius:4px">{label}</td>')
        rows.append(f'<tr><td style="color:{TEXT};font:11px {FONT};padding:2px 8px 2px 0;'
                    f'white-space:nowrap">{esc(b)}</td>{"".join(tds)}</tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="2">{head}{"".join(rows)}</table>')


def render_comparison_html(agencies: list[dict], trend_src: str, generated: str,
                           now: datetime | None = None) -> str:
    now = now or datetime.now(tz=timezone.utc)
    trend_block = section(
        "Weekly Open Postings Trend",
        f'<img src="{trend_src}" alt="Weekly open postings per agency" width="100%" '
        f'style="max-width:830px;height:auto;border-radius:8px;display:block">') if trend_src else ""
    return (
        f'<html><body style="margin:0;padding:0;background:{BG}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG}">'
        f'<tr><td align="center" style="padding:18px 10px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:880px">'
        f'<tr><td style="padding-bottom:12px">'
        f'<span style="color:{TEXT};font:700 20px {FONT}">Cross-Agency Hiring Comparison</span><br>'
        f'<span style="color:{MUTED};font:12px {FONT}">'
        f'{" &middot; ".join(esc(a["name"]) for a in agencies)} &middot; generated {esc(generated)}</span>'
        f'</td></tr>'
        f'<tr><td>{section("Headline Numbers", render_kpi_table(agencies))}</td></tr>'
        f'<tr><td>{trend_block}</td></tr>'
        f'<tr><td>{section("Key Skills Shift &nbsp;&middot;&nbsp; this week vs 2 weeks ago", render_skills_shift(agencies, now))}</td></tr>'
        f'<tr><td>{section("Skill Category Demand &nbsp;&middot;&nbsp; share of each agency&#39;s postings", render_category_matrix(agencies))}</td></tr>'
        f'<tr><td>{section("Top 10 Skills per Agency &nbsp;&middot;&nbsp; distinct postings", render_top_skills_columns(agencies))}</td></tr>'
        f'<tr><td>{section("Job Level Mix &nbsp;&middot;&nbsp; share of analyzed postings", render_level_matrix(agencies))}</td></tr>'
        f'<tr><td style="color:{MUTED};font:11px {FONT};padding-top:6px">Data: Careers@Gov via the OGP '
        f'public mirror &middot; skills extracted &amp; categorised by Claude &middot; '
        f'per-agency detail in the individual weekly reports.</td></tr>'
        f'</table></td></tr></table></body></html>')


def send_email(html_body: str, trend_png: Path, dashboard_file: Path, agency_names: list[str]) -> None:
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_APP_PASSWORD", "")
    recipient = os.getenv("REPORT_EMAIL_TO", "")
    if not (smtp_user and smtp_password and recipient):
        print("Email skipped: SMTP_USER / SMTP_APP_PASSWORD / REPORT_EMAIL_TO not all set in .env")
        return

    today = datetime.now().strftime("%d %b %Y")
    msg = EmailMessage()
    msg["Subject"] = f"Cross-Agency Hiring Comparison ({' vs '.join(agency_names)}) - {today}"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.set_content(
        f"Weekly cross-agency hiring comparison ({', '.join(agency_names)}) generated {today}.\n"
        "The dashboard is in the HTML version of this email; the standalone file is attached "
        "(download and open in a browser).")
    msg.add_alternative(html_body, subtype="html")
    if trend_png.exists():
        msg.get_payload()[-1].add_related(trend_png.read_bytes(), maintype="image",
                                          subtype="png", cid="<comparisontrend>")
    if dashboard_file.exists():
        msg.add_attachment(dashboard_file.read_bytes(), maintype="text", subtype="html",
                           filename=dashboard_file.name)
    print(f"Sending comparison to {recipient}...")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
    print("Email sent.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agencies", required=True,
                        help='Comma-separated prefix:DisplayName pairs, e.g. "htx:HTX,govtech:GovTech,dsta:DSTA"')
    parser.add_argument("--workdir", default=".", help="Folder holding the per-agency data files and .env")
    parser.add_argument("--no-email", action="store_true")
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    load_dotenv(workdir / ".env", override=True)
    now = datetime.now(tz=timezone.utc)

    agencies = []
    for pair in args.agencies.split(","):
        prefix, _, name = pair.strip().partition(":")
        name = name or prefix.upper()
        hist = workdir / f"{prefix}_job_history.csv"
        if not hist.exists():
            print(f"Skipping {name}: {hist} not found (run its pipeline first)")
            continue
        agencies.append(load_agency(workdir, prefix, name, now))
    if len(agencies) < 2:
        print("Need at least two agencies with history files to compare.")
        return 1

    labels, series = weekly_series(agencies, now)
    trend_png = workdir / "comparison_trend.png"
    render_trend_png(labels, series, trend_png)

    generated = now.strftime("%d %b %Y, %H:%M UTC")
    # Standalone file: trend embedded as data URI so the file is self-contained
    b64 = base64.b64encode(trend_png.read_bytes()).decode()
    file_html = render_comparison_html(agencies, f"data:image/png;base64,{b64}", generated, now)
    dashboard_file = workdir / "comparison_dashboard.html"
    dashboard_file.write_text(file_html, encoding="utf-8")
    print(f"Wrote {dashboard_file} ({len(agencies)} agencies)")

    if not args.no_email:
        email_html = render_comparison_html(agencies, "cid:comparisontrend", generated, now)
        send_email(email_html, trend_png, dashboard_file, [a["name"] for a in agencies])
    return 0


if __name__ == "__main__":
    sys.exit(main())
