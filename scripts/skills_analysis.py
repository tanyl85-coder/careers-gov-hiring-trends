"""
Analyzes the historical HTX job postings archive (built by careers_gov_htx_crawler.py)
to extract required skills, job level, and division from each posting via the Claude
API, then reports hiring trends by skill category, division, and job level.

Per posting, Claude extracts:
  - skills (each mapped to one of a fixed skill-category taxonomy)
  - job level as written in the title (e.g. "Lead Engineer/Engineer") plus a
    standardized band (dual-level postings take the higher band)
  - division/CoE (inferred from title + JD, normalizing variants like
    "VWS CoE" vs "Vehicle and Weapon COE")

Results are cached per Job ID in htx_skills_cache.json so re-running only spends
API calls on newly-crawled postings.

Requires ANTHROPIC_API_KEY (Streamlit secrets or .env, same lookup as app.py).

Usage:
    python skills_analysis.py
    python skills_analysis.py --history-file htx_job_history.csv --output htx_skills_analysis.xlsx
    python skills_analysis.py --limit 20      # cap API calls this run (cost control)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from collections import Counter

import anthropic
import pandas as pd
from dotenv import load_dotenv

from division_normalize import resolve_divisions
from skill_normalize import resolve_displays, skill_key

load_dotenv()

DEFAULT_HISTORY_FILE = "htx_job_history.csv"
DEFAULT_OUTPUT = "htx_skills_analysis.xlsx"
DEFAULT_CACHE_FILE = "htx_skills_cache.json"
MODEL = "claude-sonnet-4-5"

SKILL_CATEGORIES = [
    "Programming & Software Development",
    "Cloud, DevOps & Infrastructure",
    "Cybersecurity",
    "AI, Machine Learning & Data Science",
    "Data Engineering & Analytics",
    "Systems, Hardware & Robotics Engineering",
    "Networking & Telecommunications",
    "Product & Project Management",
    "Quality Assurance & Testing",
    "Design & UX",
    "Leadership & Soft Skills",
    "Domain, Regulatory & Compliance Knowledge",
    "Other",
]

# Standardized seniority ladder; dual-level postings map to the higher band.
LEVEL_BANDS = [
    "Engineer/Officer",
    "Senior/Lead",
    "Principal",
    "Manager",
    "Head",
    "Deputy Director",
    "Director & Above",
    "Unspecified",
]


def get_api_key() -> str:
    try:
        import streamlit as st
        key = st.secrets.get("ANTHROPIC_API_KEY", "") if hasattr(st, "secrets") else ""
        if key:
            return key
    except Exception:
        pass
    return os.getenv("ANTHROPIC_API_KEY", "")


def analyze_posting(client: anthropic.Anthropic, agency_name: str, job_title: str, requirements: str,
                    responsibilities: str, description: str) -> dict:
    text = (f"Job Title: {job_title}\n\nJob Requirements:\n{requirements}\n\n"
            f"Job Responsibilities:\n{responsibilities}\n\nJob Description:\n{description}").strip()
    text = text[:7000]

    prompt = f"""You are analysing a Singapore government job posting from {agency_name} to extract structured hiring data.

JOB POSTING:
{text}

Extract the following:

1. SKILLS: Every specific skill, technology, tool, methodology, or competency explicitly required or preferred (e.g. "Python", "AWS", "Penetration Testing", "Stakeholder Management", "Robotics"). For each, assign exactly one category from:
{json.dumps(SKILL_CATEGORIES)}
Extract 3-15 skills. Be specific (e.g. "Python" not "programming languages"). Do not invent skills not implied by the text. Use one canonical name per skill: prefer the widely-used short form for well-known technologies (e.g. "AWS" not "Amazon Web Services", "Kubernetes" not "K8s", "CI/CD" not "Continuous Integration/Continuous Delivery"), use consistent Title Case, and never list the same skill twice under different spellings.

2. JOB LEVEL (RAW): The level exactly as written in the job title (e.g. "Lead Engineer/Engineer", "Head", "Deputy Director", "Manager/Sr Manager"). If no level is discernible, use "Unspecified".

3. JOB LEVEL BAND: One standardized band from this ladder (if the title spans two levels, pick the HIGHER one):
{json.dumps(LEVEL_BANDS)}

4. DIVISION: The division, centre of expertise (CoE), or department this role sits in, inferred from the title and description. Singapore agency job titles often carry the division/branch as a comma-separated suffix (for example HTX uses xCyber, xData, xCloud, AI Products). Normalize obvious variants to one canonical name (e.g. "VWS CoE" and "Vehicle and Weapon COE" are the same division: "Vehicle and Weapon Systems CoE"). If no division is discernible, use "Unspecified".

Respond with ONLY valid JSON, no markdown fences, no preamble. Use this exact structure:
{{"skills": [{{"skill": "<name>", "category": "<category>"}}, ...], "job_level_raw": "<raw level>", "job_level_band": "<band>", "division": "<division>"}}"""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)

    skills = []
    for s in parsed.get("skills", []):
        skill_name = str(s.get("skill", "")).strip()
        category = s.get("category", "Other")
        if category not in SKILL_CATEGORIES:
            category = "Other"
        if skill_name:
            skills.append({"skill": skill_name, "category": category})

    band = parsed.get("job_level_band", "Unspecified")
    if band not in LEVEL_BANDS:
        band = "Unspecified"

    return {
        "skills": skills,
        "job_level_raw": str(parsed.get("job_level_raw", "Unspecified")).strip() or "Unspecified",
        "job_level_band": band,
        "division": str(parsed.get("division", "Unspecified")).strip() or "Unspecified",
    }


def load_cache(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(path: str, cache: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def analyze_new_postings(history_df: pd.DataFrame, cache: dict, cache_path: str, api_key: str,
                          agency_name: str, limit: int | None, delay: float) -> dict:
    # Re-analyze entries cached before level/division extraction was added.
    def needs_analysis(jid: str) -> bool:
        entry = cache.get(jid)
        return entry is None or "division" not in entry

    to_analyze = [str(jid) for jid in history_df["Job ID"] if needs_analysis(str(jid))]
    if limit is not None:
        to_analyze = to_analyze[:limit]

    if not to_analyze:
        print("No new postings to analyze; using cached results.")
        return cache

    client = anthropic.Anthropic(api_key=api_key)
    print(f"Analyzing {len(to_analyze)} posting(s) with Claude...")

    rows_by_id = history_df.set_index(history_df["Job ID"].astype(str))
    for i, job_id in enumerate(to_analyze, 1):
        row = rows_by_id.loc[job_id]
        print(f"  [{i}/{len(to_analyze)}] {row['Job Title']}")
        try:
            result = analyze_posting(client, agency_name, row["Job Title"], row.get("Job Requirements", ""),
                                     row.get("Job Responsibilities", ""), row.get("Job Description", ""))
            result["job_title"] = row["Job Title"]
            cache[job_id] = result
        except Exception as e:
            print(f"    Failed: {e}")
        time.sleep(delay)
        if i % 10 == 0:
            save_cache(cache_path, cache)

    save_cache(cache_path, cache)
    return cache


def month_bucket(date_str: str) -> str:
    if not date_str or not isinstance(date_str, str) or len(date_str) < 7:
        return "Unknown"
    return date_str[:7]  # YYYY-MM


def build_skills_detail(history_df: pd.DataFrame, cache: dict) -> pd.DataFrame:
    # Resolve a dataset-wide canonical display for every skill so variant
    # spellings ("AWS"/"Amazon Web Services") collapse to one label everywhere.
    raw_counter = Counter()
    for jid in history_df["Job ID"].astype(str):
        for s in cache.get(jid, {}).get("skills", []):
            raw_counter[s["skill"].strip()] += 1
    display_map = resolve_displays(raw_counter)
    div_map = resolve_divisions(
        Counter(cache.get(str(j), {}).get("division", "Unspecified")
                for j in history_df["Job ID"].astype(str)))

    rows = []
    for _, row in history_df.iterrows():
        job_id = str(row["Job ID"])
        entry = cache.get(job_id)
        if not entry:
            continue
        for s in entry.get("skills", []):
            rows.append({
                "Job ID": job_id,
                "Job Title": row["Job Title"],
                "Date Posted": row.get("Date Posted", ""),
                "Month": month_bucket(row.get("Date Posted", "")),
                "Division": div_map.get(entry.get("division", "Unspecified"), "Unspecified"),
                "Job Level (Raw)": entry.get("job_level_raw", "Unspecified"),
                "Job Level Band": entry.get("job_level_band", "Unspecified"),
                "Skill": display_map.get(skill_key(s["skill"]), s["skill"].strip()),
                "Category": s["category"],
            })
    return pd.DataFrame(rows, columns=["Job ID", "Job Title", "Date Posted", "Month", "Division",
                                       "Job Level (Raw)", "Job Level Band", "Skill", "Category"])


def build_category_trend(skills_detail: pd.DataFrame) -> pd.DataFrame:
    """Monthly counts of distinct postings demanding each skill category."""
    if skills_detail.empty:
        return pd.DataFrame()
    dedup = skills_detail.drop_duplicates(subset=["Job ID", "Category"])
    pivot = dedup.pivot_table(index="Month", columns="Category", values="Job ID", aggfunc="count", fill_value=0)
    pivot["Total Distinct Postings"] = dedup.drop_duplicates(subset=["Job ID"]).groupby("Month")["Job ID"].count()
    pivot = pivot.fillna(0).sort_index()
    return pivot.reset_index()


def build_weekly_category_trend(history_df: pd.DataFrame, cache: dict, num_weeks: int = 12) -> pd.DataFrame:
    """Weekly demand per skill category: a posting counts toward every week its
    open window (Date Posted..Closing Date) overlaps, so the trend is available
    retroactively rather than only from crawl snapshots."""
    def parse(d):
        try:
            return datetime.strptime(str(d), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    now = datetime.now(tz=timezone.utc)
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weeks = [(this_monday - timedelta(weeks=i)) for i in range(num_weeks - 1, -1, -1)]

    dated = []
    for _, row in history_df.iterrows():
        entry = cache.get(str(row["Job ID"]))
        if not entry or not entry.get("skills"):
            continue
        start, end = parse(row.get("Date Posted")), parse(row.get("Closing Date"))
        if not start:
            continue
        dated.append((start, end or now, {s["category"] for s in entry["skills"]}))

    rows = []
    for wk_start in weeks:
        wk_end = wk_start + timedelta(days=7)
        counts = {}
        total = 0
        for start, end, cats in dated:
            if start < wk_end and end >= wk_start:
                total += 1
                for c in cats:
                    counts[c] = counts.get(c, 0) + 1
        if total == 0 and not rows:
            continue  # skip leading empty weeks
        rows.append({"Week Starting": wk_start.strftime("%Y-%m-%d"),
                     "Open Postings": total, **counts})
    df = pd.DataFrame(rows).fillna(0)
    num_cols = [c for c in df.columns if c != "Week Starting"]
    df[num_cols] = df[num_cols].astype(int)
    return df


def build_top_skills(skills_detail: pd.DataFrame) -> pd.DataFrame:
    if skills_detail.empty:
        return pd.DataFrame()
    # One row per skill: count distinct postings, and attach the single most
    # common category (a skill can be tagged to different categories across
    # postings; showing it once avoids a skill appearing on multiple rows).
    dedup = skills_detail.drop_duplicates(subset=["Job ID", "Skill"])
    counts = dedup.groupby("Skill")["Job ID"].nunique().reset_index()
    counts.columns = ["Skill", "Posting Count"]
    top_cat = (skills_detail.groupby(["Skill", "Category"]).size()
               .reset_index(name="n").sort_values("n", ascending=False)
               .drop_duplicates("Skill").set_index("Skill")["Category"])
    counts["Category"] = counts["Skill"].map(top_cat)
    counts = counts[["Skill", "Category", "Posting Count"]]
    counts = counts.sort_values("Posting Count", ascending=False).reset_index(drop=True)
    return counts


def build_skills_by_division(skills_detail: pd.DataFrame) -> pd.DataFrame:
    """Skill-category demand per division: distinct postings in each division needing each category."""
    if skills_detail.empty:
        return pd.DataFrame()
    dedup = skills_detail.drop_duplicates(subset=["Job ID", "Category"])
    pivot = dedup.pivot_table(index="Division", columns="Category", values="Job ID", aggfunc="count", fill_value=0)
    pivot["Total Distinct Postings"] = dedup.drop_duplicates(subset=["Job ID"]).groupby("Division")["Job ID"].count()
    pivot = pivot.fillna(0).sort_values("Total Distinct Postings", ascending=False)
    return pivot.reset_index()


def build_skills_by_level(skills_detail: pd.DataFrame) -> pd.DataFrame:
    """Skill-category demand per standardized level band, ordered by seniority."""
    if skills_detail.empty:
        return pd.DataFrame()
    dedup = skills_detail.drop_duplicates(subset=["Job ID", "Category"])
    pivot = dedup.pivot_table(index="Job Level Band", columns="Category", values="Job ID",
                              aggfunc="count", fill_value=0)
    pivot["Total Distinct Postings"] = dedup.drop_duplicates(subset=["Job ID"]).groupby("Job Level Band")["Job ID"].count()
    pivot = pivot.fillna(0)
    order = [b for b in LEVEL_BANDS if b in pivot.index]
    pivot = pivot.reindex(order)
    return pivot.reset_index()


def build_headcount_trend(history_df: pd.DataFrame, cache: dict) -> pd.DataFrame:
    """Posting volume by month x division x level band, independent of skills."""
    rows = []
    for _, row in history_df.iterrows():
        job_id = str(row["Job ID"])
        entry = cache.get(job_id)
        if not entry:
            continue
        rows.append({
            "Month": month_bucket(row.get("Date Posted", "")),
            "Division": entry.get("division", "Unspecified"),
            "Job Level Band": entry.get("job_level_band", "Unspecified"),
            "Job ID": job_id,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    counts = df.groupby(["Month", "Division", "Job Level Band"])["Job ID"].nunique().reset_index()
    counts.columns = ["Month", "Division", "Job Level Band", "Posting Count"]
    return counts.sort_values(["Month", "Division", "Job Level Band"]).reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-file", default=DEFAULT_HISTORY_FILE, help="Path to the historical postings CSV")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output Excel report path")
    parser.add_argument("--cache-file", default=DEFAULT_CACHE_FILE, help="Path to the per-job analysis cache (JSON)")
    parser.add_argument("--agency-name", default="the hiring agency",
                        help="Agency display name, used in the extraction prompt for context")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of new postings analyzed this run")
    parser.add_argument("--delay", type=float, default=0.3, help="Delay in seconds between Claude API calls")
    args = parser.parse_args()

    if not os.path.exists(args.history_file):
        print(f"History file {args.history_file} not found. Run the crawler first.")
        return 1

    # The workdir's .env should win regardless of where this script is invoked from
    workdir_env = os.path.join(os.path.dirname(os.path.abspath(args.history_file)), ".env")
    if os.path.exists(workdir_env):
        load_dotenv(workdir_env, override=False)

    api_key = get_api_key()
    if not api_key:
        print("ANTHROPIC_API_KEY not set (checked Streamlit secrets and .env). Cannot analyze skills.")
        return 1

    history_df = pd.read_csv(args.history_file, dtype=str).fillna("")
    print(f"Loaded {len(history_df)} posting(s) from {args.history_file}.")

    cache = load_cache(args.cache_file)
    cache = analyze_new_postings(history_df, cache, args.cache_file, api_key, args.agency_name, args.limit, args.delay)

    skills_detail = build_skills_detail(history_df, cache)
    category_trend = build_category_trend(skills_detail)
    weekly_trend = build_weekly_category_trend(history_df, cache)
    top_skills = build_top_skills(skills_detail)
    skills_by_division = build_skills_by_division(skills_detail)
    skills_by_level = build_skills_by_level(skills_detail)
    headcount_trend = build_headcount_trend(history_df, cache)

    postings_view = history_df.copy()
    jid_series = postings_view["Job ID"].astype(str)
    _div_map = resolve_divisions(
        Counter(cache.get(str(j), {}).get("division", "Unspecified")
                for j in history_df["Job ID"].astype(str)))
    postings_view.insert(7, "Division", jid_series.map(
        lambda j: _div_map.get(cache.get(j, {}).get("division", ""), cache.get(j, {}).get("division", ""))))
    postings_view.insert(8, "Job Level (Raw)", jid_series.map(lambda j: cache.get(j, {}).get("job_level_raw", "")))
    postings_view.insert(9, "Job Level Band", jid_series.map(lambda j: cache.get(j, {}).get("job_level_band", "")))
    postings_view["Extracted Skills"] = jid_series.map(
        lambda j: "; ".join(f"{s['skill']} ({s['category']})" for s in cache.get(j, {}).get("skills", []))
    )

    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        postings_view.to_excel(writer, sheet_name="Postings", index=False)
        skills_detail.to_excel(writer, sheet_name="Skills Detail", index=False)
        category_trend.to_excel(writer, sheet_name="Category Trend (Monthly)", index=False)
        weekly_trend.to_excel(writer, sheet_name="Category Trend (Weekly)", index=False)
        top_skills.to_excel(writer, sheet_name="Top Skills", index=False)
        skills_by_division.to_excel(writer, sheet_name="Skills by Division", index=False)
        skills_by_level.to_excel(writer, sheet_name="Skills by Level", index=False)
        headcount_trend.to_excel(writer, sheet_name="Headcount Trend", index=False)

    print(f"Wrote skills analysis report to {args.output}")
    print(f"  Postings: {len(postings_view)}, Skills extracted: {len(skills_detail)}, "
          f"Distinct skills: {top_skills['Skill'].nunique() if not top_skills.empty else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
