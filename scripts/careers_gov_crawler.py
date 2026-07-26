"""
Crawls active job postings from Careers@Gov (https://jobs.careers.gov.sg, Singapore
Public Service) for a given agency (default: HTX - Home Team Science and Technology
Agency), accumulates them into a local historical dataset, and exports a snapshot
to Excel.

Careers@Gov's job data is served from an internal SAP HRP OData backend that isn't
publicly documented. Open Government Products (the team that builds Careers@Gov)
publishes a daily-refreshed mirror of the same OData response at:
  https://raw.githubusercontent.com/opengovsg/careersgovsg-jobs-data/main/data/job-listings.json
This script pulls from that official mirror, which is the same data the search UI
at jobs.careers.gov.sg reads from.

Postings fall off the live feed once they close, so this script is meant to run
daily: each run merges newly-seen postings into htx_job_history.csv (keyed by Job
ID) and updates "Last Seen" for postings still active, building an archive that
long-term hiring-trend analysis (see skills_analysis.py) can be run against.

Usage:
    python careers_gov_htx_crawler.py
    python careers_gov_htx_crawler.py --agency HTX --output htx_job_postings.xlsx
    python careers_gov_htx_crawler.py --agency HTX --include-closed
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

DATA_URL = "https://raw.githubusercontent.com/opengovsg/careersgovsg-jobs-data/main/data/job-listings.json"
REQUEST_TIMEOUT = 30
DEFAULT_HISTORY_FILE = "htx_job_history.csv"

COLUMNS = [
    "Job ID", "Posting No", "Date Posted", "Closing Date", "Remaining Days", "Is New",
    "Job Title", "Agency", "Location", "Employment Type", "Work Arrangement",
    "Experience Required", "Field", "Functional Area", "Industry", "Category",
    "Job Requirements", "Job Responsibilities", "Job Description",
]
HISTORY_COLUMNS = COLUMNS + ["First Seen", "Last Seen"]


def fetch_all_listings() -> list[dict]:
    resp = requests.get(DATA_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def epoch_ms_to_date(epoch_ms) -> str:
    if not epoch_ms:
        return ""
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def filter_jobs(jobs: list[dict], agency_keyword: str, active_only: bool) -> list[dict]:
    keyword = agency_keyword.strip().upper()
    now_ms = datetime.now(tz=timezone.utc).timestamp() * 1000

    matched = [j for j in jobs if keyword in j.get("agency", "").upper()]
    if active_only:
        # Postings with no closing date are open-until-filled (e.g. GovTech posts
        # ~96% of its jobs this way) - treat them as active, not expired.
        def is_active(j):
            cd = j.get("closingDate")
            has_date = isinstance(cd, (int, float)) and cd == cd  # NaN != NaN
            return (not has_date) or cd > now_ms
        matched = [j for j in matched if is_active(j)]
    return matched


def to_rows(jobs: list[dict]) -> list[dict]:
    rows = []
    for j in jobs:
        rows.append({
            "Job ID": j.get("jobId", ""),
            "Posting No": j.get("postingNo", ""),
            "Date Posted": epoch_ms_to_date(j.get("startDate")),
            "Closing Date": epoch_ms_to_date(j.get("closingDate")) or j.get("closingDateText", ""),
            "Remaining Days": j.get("remainingDays", ""),
            "Is New": j.get("isNew", False),
            "Job Title": j.get("jobTitle", ""),
            "Agency": j.get("agency", ""),
            "Location": j.get("location", ""),
            "Employment Type": j.get("employmentType", ""),
            "Work Arrangement": j.get("workArrangement", ""),
            "Experience Required": j.get("experienceRequired", ""),
            "Field": j.get("field", ""),
            "Functional Area": j.get("functionalArea", ""),
            "Industry": j.get("industry", ""),
            "Category": j.get("category", ""),
            "Job Requirements": j.get("jobRequirements", ""),
            "Job Responsibilities": j.get("jobResponsibilities", ""),
            "Job Description": j.get("jobDescription", ""),
        })
    return rows


def crawl(agency_keyword: str, active_only: bool, allow_multiple: bool = False) -> list[dict] | None:
    print("Fetching latest Careers@Gov job data mirror...")
    jobs = fetch_all_listings()
    print(f"Loaded {len(jobs)} total job posting(s) across all agencies.")

    matched = filter_jobs(jobs, agency_keyword, active_only)
    label = "active" if active_only else "total"
    print(f"Matched {len(matched)} {label} posting(s) for agency '{agency_keyword}'.")

    # Guard against an over-broad keyword matching several agencies (e.g. a stray
    # "x" matches every agency name containing the letter X). Mixing agencies into
    # one history/cache corrupts it, so abort unless explicitly allowed.
    matched_agencies = sorted(set(j.get("agency", "") for j in matched))
    if len(matched_agencies) > 1 and not allow_multiple:
        print(f"ABORT: keyword '{agency_keyword}' matches {len(matched_agencies)} agencies:")
        for a in matched_agencies[:12]:
            print(f"  - {a}")
        print("Use a more specific --agency fragment (one agency), or pass --allow-multiple-agencies "
              "if you really intend to combine them.")
        return None

    if not matched:
        # The mirror uses full agency names (e.g. "Government Technology Agency",
        # not "GovTech"/"GVT"), so a zero-match usually means the keyword doesn't
        # appear in the name. Suggest candidates instead of failing silently.
        agencies = sorted(set(j.get("agency", "") for j in jobs))
        # Split on spaces AND camel-case so "GovTech" yields ["gov", "tech"],
        # both of which appear in "Government Technology Agency".
        words = [w.lower() for w in re.findall(r"[A-Z]+[a-z]*|[a-z]{3,}", agency_keyword) if len(w) > 2]
        candidates = ([a for a in agencies if all(w in a.lower() for w in words)]
                      or [a for a in agencies if any(w in a.lower() for w in words)]
                      or agencies)
        print("No postings matched. Agency names in the data mirror that may be what you meant:")
        for a in candidates[:15]:
            print(f"  - {a}")
        print("Re-run with --agency set to (part of) one of these names, or use --list-agencies.")

    return to_rows(matched)


def list_agencies() -> None:
    jobs = fetch_all_listings()
    counts = {}
    for j in jobs:
        counts[j.get("agency", "")] = counts.get(j.get("agency", ""), 0) + 1
    print(f"{len(counts)} agencies with postings on record:")
    for a in sorted(counts, key=counts.get, reverse=True):
        print(f"  {counts[a]:4d}  {a}")


def merge_into_history(history_path: str, new_rows: list[dict]) -> pd.DataFrame:
    """Merges freshly-crawled postings into the on-disk history, keyed by Job ID.

    New postings are added with First Seen = Last Seen = today. Postings already
    on file get Last Seen bumped to today (their content is otherwise left as
    originally captured, since Careers@Gov postings don't change once live).
    """
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    new_df["First Seen"] = today
    new_df["Last Seen"] = today

    if os.path.exists(history_path):
        existing_df = pd.read_csv(history_path, dtype=str).fillna("")
        seen_ids = set(existing_df["Job ID"])
        existing_df.loc[existing_df["Job ID"].isin(new_df["Job ID"]), "Last Seen"] = today
        added_df = new_df[~new_df["Job ID"].isin(seen_ids)]
        combined = pd.concat([existing_df, added_df], ignore_index=True)
    else:
        combined = new_df

    combined = combined[HISTORY_COLUMNS]
    combined.to_csv(history_path, index=False)
    return combined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agency", default="HTX", help="Agency code/keyword to filter on (default: HTX)")
    parser.add_argument("--output", default="htx_job_postings.xlsx", help="Output Excel file path for today's snapshot")
    parser.add_argument("--history-file", default=DEFAULT_HISTORY_FILE,
                        help="Path to the persistent historical dataset (CSV) that accumulates across daily runs")
    parser.add_argument("--include-closed", action="store_true",
                        help="Include postings whose closing date has passed (default: active only)")
    parser.add_argument("--no-history", action="store_true",
                        help="Skip updating the historical dataset (snapshot export only)")
    parser.add_argument("--list-agencies", action="store_true",
                        help="List all agency names present in the data mirror and exit")
    parser.add_argument("--allow-multiple-agencies", action="store_true",
                        help="Permit a keyword that matches more than one agency (default: abort)")
    args = parser.parse_args()

    if args.list_agencies:
        list_agencies()
        return 0

    rows = crawl(args.agency, active_only=not args.include_closed,
                 allow_multiple=args.allow_multiple_agencies)
    if rows is None:
        return 1

    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_excel(args.output, index=False, engine="openpyxl")
    print(f"Wrote {len(df)} row(s) to {args.output}")

    if not args.no_history:
        combined = merge_into_history(args.history_file, rows)
        new_count = len(rows)
        print(f"History file {args.history_file} now has {len(combined)} unique posting(s) on record "
              f"({new_count} seen in today's crawl).")


if __name__ == "__main__":
    sys.exit(main())
