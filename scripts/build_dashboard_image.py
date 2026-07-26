"""
Renders the HTX hiring skills dashboard as a single PNG image (htx_dashboard.png),
for embedding directly in the daily report email body. Gmail never renders HTML
attachments (it shows their source as text), but inline images display instantly,
so the PNG is what makes the dashboard visible inside the email itself.

Reuses build_dashboard.build_payload for all aggregation, so the PNG, the HTML
dashboard, and the Excel workbook always agree.

Usage:
    python build_dashboard_image.py
    python build_dashboard_image.py --output htx_dashboard.png
"""

import argparse
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from build_dashboard import DEFAULT_CACHE_FILE, DEFAULT_HISTORY_FILE, PALETTE, build_payload

DEFAULT_OUTPUT = "htx_dashboard.png"

BG = "#0b1020"
PANEL = "#131a30"
TEXT = "#e8ecf6"
MUTED = "#8b96b3"
ACCENT = "#5eead4"
ACCENT2 = "#818cf8"
GOLD = "#fbbf24"
LINE = "#232e4d"


def style_axes(ax, title):
    ax.set_facecolor(PANEL)
    ax.set_title(title, color=MUTED, fontsize=9, loc="left", pad=8,
                 fontweight="bold")
    for spine in ax.spines.values():
        spine.set_color(LINE)
    ax.tick_params(colors=MUTED, labelsize=7)


def hbar(ax, rows, name_key, count_key, title, color=ACCENT, max_rows=12):
    rows = rows[:max_rows][::-1]
    names = [r[name_key] if len(r[name_key]) <= 24 else r[name_key][:22] + "…" for r in rows]
    counts = [r[count_key] for r in rows]
    style_axes(ax, title)
    bars = ax.barh(names, counts, color=color, height=0.62, zorder=3)
    ax.bar_label(bars, color=MUTED, fontsize=7, padding=3)
    ax.set_xlim(0, max(counts, default=1) * 1.18)
    ax.grid(axis="x", color=LINE, linewidth=0.6, zorder=0)
    ax.tick_params(axis="y", labelsize=7.5)
    for lbl in ax.get_yticklabels():
        lbl.set_color(TEXT)


def render(payload: dict, output: str, title: str = "Hiring Skills Dashboard") -> None:
    fig = plt.figure(figsize=(13, 16), dpi=110)
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(5, 6, height_ratios=[0.55, 1.5, 1.9, 1.5, 1.9],
                          hspace=0.52, wspace=0.9,
                          left=0.09, right=0.97, top=0.94, bottom=0.03)

    fig.text(0.09, 0.972, title, color=TEXT, fontsize=16, fontweight="bold")
    fig.text(0.97, 0.972, f"Generated {payload['generated']}", color=MUTED, fontsize=8, ha="right")

    # KPI row
    kpi_ax = fig.add_subplot(gs[0, :])
    kpi_ax.axis("off")
    k = payload["kpis"]
    kpis = [(k["active"], "ACTIVE POSTINGS", ACCENT), (k["tracked"], "POSTINGS TRACKED", ACCENT2),
            (k["distinctSkills"], "DISTINCT SKILLS", ACCENT), (k["divisions"], "DIVISIONS HIRING", ACCENT2),
            (k["closingSoon"], "CLOSING IN 7 DAYS", GOLD)]
    for i, (v, label, color) in enumerate(kpis):
        x = i / len(kpis) + 0.5 / len(kpis)
        kpi_ax.text(x, 0.62, str(v), color=color, fontsize=22, fontweight="bold",
                    ha="center", transform=kpi_ax.transAxes)
        kpi_ax.text(x, 0.13, label, color=MUTED, fontsize=7.5, ha="center",
                    transform=kpi_ax.transAxes)

    # Weekly trend
    trend_ax = fig.add_subplot(gs[1, :])
    trend = payload["weeklyTrend"]
    style_axes(trend_ax, "WEEKLY SKILLS DEMAND TREND  (postings open each week, top 6 categories)")
    if trend["weeks"] and trend["series"]:
        x = np.arange(len(trend["weeks"]))
        for si, s in enumerate(trend["series"]):
            trend_ax.plot(x, s["values"], marker="o", markersize=3.5, linewidth=2,
                          color=PALETTE[si % len(PALETTE)], label=s["category"], zorder=3)
        trend_ax.set_xticks(x)
        trend_ax.set_xticklabels(trend["weeks"], color=MUTED)
        trend_ax.grid(axis="y", color=LINE, linewidth=0.6, zorder=0)
        trend_ax.legend(loc="upper left", fontsize=6.5, ncols=2, facecolor=PANEL,
                        edgecolor=LINE, labelcolor=TEXT)

    # Category demand + top skills
    cat_ax = fig.add_subplot(gs[2, :3])
    hbar(cat_ax, payload["categories"], "category", "count",
         "SKILL CATEGORY DEMAND  (distinct postings)", ACCENT, max_rows=13)
    skills_ax = fig.add_subplot(gs[2, 3:])
    hbar(skills_ax, payload["topSkills"], "skill", "count",
         "TOP SKILLS  (top 12)", ACCENT2, max_rows=12)

    # Divisions, levels, monthly
    div_ax = fig.add_subplot(gs[3, :2])
    hbar(div_ax, payload["divisions"], "division", "count", "POSTINGS BY DIVISION", ACCENT, max_rows=10)
    lvl_ax = fig.add_subplot(gs[3, 2:4])
    hbar(lvl_ax, payload["levels"], "band", "count", "JOB LEVEL MIX", ACCENT2, max_rows=8)
    mon_ax = fig.add_subplot(gs[3, 4:])
    style_axes(mon_ax, "MONTHLY POSTING VOLUME")
    months = payload["monthly"]
    if months:
        bars = mon_ax.bar([m["month"] for m in months], [m["count"] for m in months],
                          color=ACCENT, width=0.55, zorder=3)
        mon_ax.bar_label(bars, color=TEXT, fontsize=8, padding=2)
        mon_ax.grid(axis="y", color=LINE, linewidth=0.6, zorder=0)

    # Heatmap
    hm_ax = fig.add_subplot(gs[4, :])
    m = payload["matrix"]
    style_axes(hm_ax, "DIVISION x SKILL CATEGORY  (postings needing category)")
    if m["divisions"]:
        cells = np.array(m["cells"], dtype=float)
        from matplotlib.colors import LinearSegmentedColormap
        cmap = LinearSegmentedColormap.from_list("htx", ["#0e1428", ACCENT])
        hm_ax.imshow(cells, cmap=cmap, aspect="auto")
        hm_ax.set_xticks(range(len(m["categories"])))
        hm_ax.set_xticklabels([" ".join(c.split(" ")[:3]) for c in m["categories"]],
                              fontsize=7, color=MUTED)
        hm_ax.set_yticks(range(len(m["divisions"])))
        hm_ax.set_yticklabels([d if len(d) <= 30 else d[:28] + "…" for d in m["divisions"]],
                              fontsize=7.5, color=TEXT)
        vmax = cells.max() or 1
        for i in range(cells.shape[0]):
            for j in range(cells.shape[1]):
                v = int(cells[i, j])
                if v:
                    hm_ax.text(j, i, str(v), ha="center", va="center", fontsize=7,
                               color="#06281f" if v / vmax > 0.55 else TEXT)

    fig.savefig(output, facecolor=BG)
    plt.close(fig)


def render_trend_only(payload: dict, output: str) -> None:
    """Just the weekly trend line chart, sized for embedding in the email body."""
    fig = plt.figure(figsize=(10.5, 3.6), dpi=130)
    fig.patch.set_facecolor(BG)
    ax = fig.add_subplot(111)
    style_axes(ax, "")
    trend = payload["weeklyTrend"]
    if trend["weeks"] and trend["series"]:
        x = np.arange(len(trend["weeks"]))
        for si, s in enumerate(trend["series"]):
            ax.plot(x, s["values"], marker="o", markersize=4, linewidth=2.2,
                    color=PALETTE[si % len(PALETTE)], label=s["category"], zorder=3)
        ax.set_xticks(x)
        ax.set_xticklabels(trend["weeks"], color=MUTED, fontsize=8)
        ax.grid(axis="y", color=LINE, linewidth=0.6, zorder=0)
        ax.legend(loc="upper left", fontsize=7, ncols=2, facecolor=PANEL,
                  edgecolor=LINE, labelcolor=TEXT)
    fig.subplots_adjust(left=0.05, right=0.99, top=0.96, bottom=0.12)
    fig.savefig(output, facecolor=BG)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-file", default=DEFAULT_HISTORY_FILE)
    parser.add_argument("--cache-file", default=DEFAULT_CACHE_FILE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--agency-name", default="Agency",
                        help="Agency display name used in the image title")
    parser.add_argument("--trend-only", action="store_true",
                        help="Render only the weekly trend chart (for the email body)")
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
    if args.trend_only:
        render_trend_only(payload, args.output)
    else:
        render(payload, args.output, f"{args.agency_name} Hiring Skills Dashboard")
    size_kb = os.path.getsize(args.output) // 1024
    print(f"Wrote {'trend' if args.trend_only else 'dashboard'} image to {args.output} ({size_kb} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
