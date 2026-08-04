"""
Builds the email-safe HTML body for the daily HTX report: the dashboard rendered
as native email HTML (tables + inline styles, no scripts, no external CSS), so it
scrolls, zooms, and reflows like any normal email in Gmail/Outlook/mobile.

Only the weekly trend line chart is an image (email HTML can't draw lines) - it is
CID-embedded by daily_htx_report.py, which passes the cid used in the <img> tag.

Reuses build_dashboard.build_payload, so email, HTML dashboard, PNG, and workbook
always agree.
"""

import html
import json
import os

import pandas as pd

from build_dashboard import DEFAULT_CACHE_FILE, DEFAULT_HISTORY_FILE, build_payload

BG = "#0b1020"
PANEL = "#131a30"
TEXT = "#e8ecf6"
MUTED = "#8b96b3"
ACCENT = "#5eead4"
ACCENT2 = "#818cf8"
GOLD = "#fbbf24"
CELL_BG = "#0e1428"

FONT = "'Segoe UI',system-ui,Arial,sans-serif"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def _blend(c1: str, c2: str, t: float) -> str:
    """Blend two hex colors; email clients don't all support rgba()."""
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def section(title: str, body: str) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{PANEL};border-radius:12px;margin:0 0 14px 0">'
            f'<tr><td style="padding:14px 16px">'
            f'<div style="color:{MUTED};font:600 11px {FONT};letter-spacing:1px;'
            f'text-transform:uppercase;padding-bottom:10px">{title}</div>'
            f'{body}</td></tr></table>')


def render_kpis(k: dict) -> str:
    items = [(k["active"], "Active Postings", ACCENT), (k["tracked"], "Tracked", ACCENT2),
             (k.get("coreSkills", k["distinctSkills"]), "Core Skills", ACCENT), (k["divisions"], "Divisions", ACCENT2),
             (k["closingSoon"], "Closing In 7 Days", GOLD)]
    tds = "".join(
        f'<td align="center" style="padding:10px 4px">'
        f'<div style="color:{color};font:700 24px {FONT}">{v}</div>'
        f'<div style="color:{MUTED};font:11px {FONT}">{label}</div></td>'
        for v, label, color in items)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{PANEL};border-radius:12px;margin:0 0 14px 0"><tr>{tds}</tr></table>')


def render_bars(rows: list[dict], name_key: str, count_key: str, color: str,
                max_rows: int = 12) -> str:
    rows = rows[:max_rows]
    if not rows:
        return f'<div style="color:{MUTED};font:12px {FONT}">No data yet.</div>'
    max_v = max((r[count_key] for r in rows), default=1) or 1
    trs = []
    for r in rows:
        pct = max(3, round(100 * r[count_key] / max_v))
        trs.append(
            f'<tr>'
            f'<td style="color:{TEXT};font:12px {FONT};padding:3px 8px 3px 0;white-space:nowrap;'
            f'overflow:hidden;max-width:190px">{esc(r[name_key])}</td>'
            f'<td width="100%" style="padding:3px 0">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td width="{pct}%" style="background:{color};height:13px;border-radius:4px;'
            f'font-size:1px;line-height:1px">&nbsp;</td>'
            f'<td style="font-size:1px">&nbsp;</td></tr></table></td>'
            f'<td align="right" style="color:{MUTED};font:12px {FONT};padding:3px 0 3px 8px">'
            f'{r[count_key]}</td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'{"".join(trs)}</table>')


def render_heatmap(matrix: dict) -> str:
    divisions, categories, cells = matrix["divisions"], matrix["categories"], matrix["cells"]
    if not divisions:
        return f'<div style="color:{MUTED};font:12px {FONT}">No data yet.</div>'
    flat = [v for row in cells for v in row]
    max_v = max(flat, default=1) or 1
    head = "<tr><td></td>" + "".join(
        f'<td align="center" style="color:{MUTED};font:10px {FONT};padding:4px 2px">'
        f'{esc(" ".join(c.split(" ")[:2]))}</td>' for c in categories) + "</tr>"
    trs = []
    for i, d in enumerate(divisions):
        tds = []
        for j in range(len(categories)):
            v = cells[i][j]
            t = 0 if v == 0 else 0.12 + 0.55 * v / max_v
            bg = CELL_BG if v == 0 else _blend(CELL_BG, ACCENT, t)
            fg = "#06281f" if t > 0.45 else TEXT
            tds.append(f'<td align="center" style="background:{bg};color:{fg};font:11px {FONT};'
                       f'padding:7px 2px;border-radius:4px">{v if v else "&nbsp;"}</td>')
        trs.append(f'<tr><td align="right" style="color:{TEXT};font:11px {FONT};'
                   f'padding:2px 8px 2px 0;white-space:nowrap">{esc(d)}</td>{"".join(tds)}</tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="2">'
            f'{head}{"".join(trs)}</table>')


def render_core_block(conc: dict) -> str:
    if not conc.get("core"):
        return f'<div style="color:{MUTED};font:12px {FONT}">Not enough analysed postings yet.</div>'
    rows = []
    for c in conc["core"][:12]:
        pct = c["share"] * 100
        rows.append(
            f'<tr><td style="color:{TEXT};font:12px {FONT};padding:3px 8px 3px 0;white-space:nowrap">'
            f'{esc(c["skill"])}</td>'
            f'<td width="100%" style="padding:3px 0">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td width="{max(2, round(pct))}%" style="background:{ACCENT};height:13px;border-radius:4px;'
            f'font-size:1px;line-height:1px">&nbsp;</td><td style="font-size:1px">&nbsp;</td>'
            f'</tr></table></td>'
            f'<td align="right" style="color:{MUTED};font:12px {FONT};padding-left:8px">{pct:.0f}%</td></tr>')
    tail = conc["distinct"] - len(conc["core"])
    note = (f'<div style="color:{MUTED};font:10px {FONT};padding-top:8px">'
            f'{len(conc["core"])} skills appear in 10%+ of postings and carry the recurring demand '
            f'signal. The remaining {tail} of {conc["distinct"]} distinct skills appear rarely - a mix '
            f'of niche tools and one-off phrasings - so &ldquo;distinct skills&rdquo; measures '
            f'vocabulary breadth, not demand.</div>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'{"".join(rows)}</table>{note}')


def render_email_html(payload: dict, title: str, intro_html: str, footer_html: str,
                      trend_cid: str | None) -> str:
    two_col = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td valign="top" width="50%" style="padding-right:7px">'
        + section("Skill Category Demand",
                  render_bars(payload["categories"], "category", "count", ACCENT, 13))
        + '</td><td valign="top" width="50%" style="padding-left:7px">'
        + section("Core Skills &nbsp;&middot;&nbsp; share of postings",
                  render_core_block(payload.get("concentration", {})))
        + "</td></tr></table>")
    two_col2 = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td valign="top" width="50%" style="padding-right:7px">'
        + section("Postings by Division",
                  render_bars(payload["divisions"], "division", "count", ACCENT, 10))
        + '</td><td valign="top" width="50%" style="padding-left:7px">'
        + section("Job Level Mix",
                  render_bars(payload["levels"], "band", "count", ACCENT2, 8))
        + "</td></tr></table>")

    trend_block = ""
    if trend_cid:
        trend_block = section(
            "Weekly Skills Demand Trend &nbsp;&middot;&nbsp; postings open each week, top 6 categories",
            f'<img src="cid:{trend_cid}" alt="Weekly skills demand trend" width="100%" '
            f'style="max-width:830px;height:auto;border-radius:8px;display:block">')

    return (
        f'<html><body style="margin:0;padding:0;background:{BG}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{BG}"><tr><td align="center" style="padding:18px 10px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:880px">'
        f'<tr><td style="padding-bottom:12px">'
        f'<span style="color:{TEXT};font:700 20px {FONT}">{esc(title)}</span><br>'
        f'<span style="color:{MUTED};font:12px {FONT}">Generated {esc(payload["generated"])}</span>'
        f'</td></tr>'
        f'<tr><td style="color:{TEXT};font:13px {FONT};padding-bottom:12px">{intro_html}</td></tr>'
        f'<tr><td>{render_kpis(payload["kpis"])}</td></tr>'
        f'<tr><td>{trend_block}</td></tr>'
        f'<tr><td>{two_col}</td></tr>'
        f'<tr><td>{two_col2}</td></tr>'
        f'<tr><td>{section("Division &times; Skill Category", render_heatmap(payload["matrix"]))}</td></tr>'
        f'<tr><td style="color:{MUTED};font:11px {FONT};padding-top:6px">{footer_html}</td></tr>'
        f'</table></td></tr></table></body></html>')


def build_email_html(title: str, intro_html: str, footer_html: str, trend_cid: str | None,
                     history_file: str = DEFAULT_HISTORY_FILE,
                     cache_file: str = DEFAULT_CACHE_FILE) -> str:
    history_df = pd.read_csv(history_file, dtype=str).fillna("")
    cache = {}
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            cache = json.load(f)
    payload = build_payload(history_df, cache)
    return render_email_html(payload, title, intro_html, footer_html, trend_cid)
