"""
Daily Careers@Gov hiring-trend pipeline for one agency, meant to be run by a
scheduler (Windows Task Scheduler / cron):

  1. Crawl Careers@Gov for the agency's active postings (careers_gov_crawler.py),
     merging into the historical archive <prefix>_job_history.csv.
  2. Analyze skills/level/division for any new postings (skills_analysis.py,
     cached per Job ID so only new postings cost API calls).
  3. Rebuild the single-file dashboard <prefix>_dashboard.html and the trend
     chart image for the email body.
  4. Email the report: dashboard rendered as native HTML in the email body
     (scrollable), workbook + HTML dashboard attached.

All data files live in --workdir (default: current directory), so one skill
install can serve many agency pipelines in different project folders.

Email settings come from <workdir>/.env:
  ANTHROPIC_API_KEY   - for the skills analysis
  SMTP_USER           - Gmail address the report is sent from (the account
                        that generated the app password)
  SMTP_APP_PASSWORD   - Gmail App Password (myaccount.google.com/apppasswords)
  REPORT_EMAIL_TO     - recipient address

Usage:
    python daily_report.py --agency HTX --workdir C:/path/to/project
    python daily_report.py --agency GVT --agency-name "GovTech" --no-email
"""

import argparse
import errno
import os
import smtplib
import subprocess
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class Pipeline:
    def __init__(self, workdir: Path, agency: str, agency_name: str, prefix: str):
        self.workdir = workdir
        self.agency = agency
        self.agency_name = agency_name
        self.prefix = prefix
        self.log_file = workdir / f"{prefix}_daily_report.log"
        self.history_file = workdir / f"{prefix}_job_history.csv"
        self.snapshot_file = workdir / f"{prefix}_job_postings.xlsx"
        self.report_file = workdir / f"{prefix}_skills_analysis.xlsx"
        self.cache_file = workdir / f"{prefix}_skills_cache.json"
        self.dashboard_file = workdir / f"{prefix}_dashboard.html"
        self.trend_image = workdir / f"{prefix}_trend.png"

    def log(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def run_step(self, name: str, cmd: list[str]) -> bool:
        self.log(f"START {name}: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=self.workdir, capture_output=True, text=True)
        for stream in (result.stdout, result.stderr):
            if stream:
                for line in stream.strip().splitlines():
                    self.log(f"  {line}")
        if result.returncode != 0:
            self.log(f"FAILED {name} (exit {result.returncode})")
            return False
        self.log(f"DONE {name}")
        return True

    def build_email_summary_text(self) -> str:
        """Headline numbers for the plain-text email body. Best-effort."""
        try:
            import json as _json
            from collections import Counter

            import pandas as _pd

            history = _pd.read_csv(self.history_file, dtype=str).fillna("")
            with open(self.cache_file, "r", encoding="utf-8") as f:
                cache = _json.load(f)

            if "Last Seen" in history.columns:
                active = history[history["Last Seen"] == history["Last Seen"].max()]
            else:
                today = datetime.now().strftime("%Y-%m-%d")
                cd = history["Closing Date"]
                active = history[(cd == "") | (cd >= today)]

            from skill_normalize import resolve_displays, skill_key
            cat_counts = Counter()
            skill_counts = Counter()
            raw_counter = Counter()
            for jid in history["Job ID"].astype(str):
                entry = cache.get(jid, {})
                for c in {s["category"] for s in entry.get("skills", [])}:
                    cat_counts[c] += 1
                for s in entry.get("skills", []):
                    raw_counter[s["skill"].strip()] += 1
                for k in {skill_key(s["skill"]) for s in entry.get("skills", [])}:
                    skill_counts[k] += 1
            disp = resolve_displays(raw_counter)

            top_cats = ", ".join(f"{c} ({n})" for c, n in cat_counts.most_common(3))
            top_skills = ", ".join(f"{disp.get(k, k)} ({n})" for k, n in skill_counts.most_common(5))
            return (f"TODAY'S HEADLINES\n"
                    f"- Active postings: {len(active)} (of {len(history)} tracked)\n"
                    f"- Top skill categories: {top_cats}\n"
                    f"- Most-demanded skills: {top_skills}\n")
        except Exception:
            return ""

    def send_report_email(self) -> bool:
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_password = os.getenv("SMTP_APP_PASSWORD", "")
        recipient = os.getenv("REPORT_EMAIL_TO", "")

        if not (smtp_user and smtp_password and recipient):
            self.log("Email skipped: SMTP_USER / SMTP_APP_PASSWORD / REPORT_EMAIL_TO not all set in .env")
            return False
        if not self.report_file.exists():
            self.log(f"Email skipped: report file {self.report_file} not found")
            return False

        today = datetime.now().strftime("%d %b %Y")
        title = f"{self.agency_name} Hiring Skills Dashboard"
        summary_text = self.build_email_summary_text()
        msg = EmailMessage()
        msg["Subject"] = f"{self.agency_name} Hiring Skills Report - {today}"
        msg["From"] = smtp_user
        msg["To"] = recipient

        # Plain-text fallback for clients that don't render HTML.
        msg.set_content(
            f"Daily {self.agency_name} hiring skills analysis generated on {today}.\n\n"
            + summary_text +
            "\nThe dashboard is in the HTML version of this email.\n"
            f"Attachments: {self.report_file.name} (full workbook), {self.dashboard_file.name} "
            "(open in a browser after downloading).\n\n"
            "Data source: Careers@Gov (jobs.careers.gov.sg) via the Open Government Products "
            "public data mirror. Skills extracted and categorized by Claude."
        )

        # HTML body: the dashboard itself rendered as native email HTML. Gmail
        # renders HTML in the body but NEVER in attachments (attachments show as
        # raw source), and a full-page image can't scroll - so the body IS the
        # dashboard, with only the trend line chart as a CID-embedded image.
        trend_cid = "trendchart" if self.trend_image.exists() else None
        intro_html = f"Daily {self.agency_name} hiring skills analysis generated on <b>{today}</b>."
        footer_html = (f"Full detail in the attached workbook ({self.report_file.name}). "
                       f"The attached {self.dashboard_file.name} shows this dashboard in a browser "
                       "(download it first; email providers display HTML attachments as source text). "
                       "Data: Careers@Gov via the OGP public mirror &middot; skills extracted &amp; "
                       "categorised by Claude.")
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            from build_email_body import build_email_html
            html_body = build_email_html(title, intro_html, footer_html, trend_cid,
                                         history_file=str(self.history_file),
                                         cache_file=str(self.cache_file))
        except Exception as e:
            self.log(f"Email-body dashboard build failed ({e}); falling back to plain summary body.")
            html_body = (f"<html><body style='font-family:Segoe UI,Arial,sans-serif'>"
                         f"<p>{intro_html}</p><pre>{summary_text}</pre>"
                         f"<p style='font-size:.8em;color:#666'>{footer_html}</p></body></html>")
            trend_cid = None
        msg.add_alternative(html_body, subtype="html")

        if trend_cid:
            html_part = msg.get_payload()[-1]
            html_part.add_related(self.trend_image.read_bytes(), maintype="image", subtype="png",
                                  cid=f"<{trend_cid}>")

        msg.add_attachment(
            self.report_file.read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=self.report_file.name,
        )
        if self.dashboard_file.exists():
            msg.add_attachment(
                self.dashboard_file.read_bytes(),
                maintype="text",
                subtype="html",
                filename=self.dashboard_file.name,
            )

        self.log(f"Sending report to {recipient} via {SMTP_HOST}...")
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        self.log("Email sent.")
        return True

    def acquire_lock(self):
        """Exclusive per-agency lock. Windows Task Scheduler fires every missed
        run at once after the PC has been off, so an agency's crawl task and its
        report task can otherwise start seconds apart and fight over the same
        history/cache files. Second arrival exits quietly instead."""
        lock_path = self.workdir / f".{self.prefix}_pipeline.lock"
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            # Stale lock (>2h) means a previous run died; take it over.
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = 0
            if age > 7200:
                self.log(f"Removing stale lock ({age/3600:.1f}h old).")
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                return self.acquire_lock()
            return None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lock_path

    def run(self, send_email: bool) -> int:
        lock_path = self.acquire_lock()
        if lock_path is None:
            self.log(f"SKIPPED: another {self.agency_name} pipeline run is already in progress.")
            return 0
        try:
            return self._run(send_email)
        finally:
            try:
                os.unlink(lock_path)
            except OSError:
                pass

    def _run(self, send_email: bool) -> int:
        py = sys.executable
        self.log(f"=== Daily {self.agency_name} report run started ===")

        ok = self.run_step("crawl", [py, str(SCRIPT_DIR / "careers_gov_crawler.py"),
                                     "--agency", self.agency,
                                     "--output", str(self.snapshot_file),
                                     "--history-file", str(self.history_file)])
        if not ok:
            self.log("=== Run aborted at crawl step ===")
            return 1

        ok = self.run_step("analyze", [py, str(SCRIPT_DIR / "skills_analysis.py"),
                                       "--history-file", str(self.history_file),
                                       "--cache-file", str(self.cache_file),
                                       "--agency-name", self.agency_name,
                                       "--output", str(self.report_file)])
        if not ok:
            self.log("=== Run aborted at analyze step ===")
            return 1

        ok = self.run_step("dashboard", [py, str(SCRIPT_DIR / "build_dashboard.py"),
                                         "--history-file", str(self.history_file),
                                         "--cache-file", str(self.cache_file),
                                         "--agency-name", self.agency_name,
                                         "--output", str(self.dashboard_file)])
        if not ok:
            self.log("Dashboard build failed; continuing (email will attach workbook only).")

        ok = self.run_step("trend-image", [py, str(SCRIPT_DIR / "build_dashboard_image.py"),
                                           "--trend-only",
                                           "--history-file", str(self.history_file),
                                           "--cache-file", str(self.cache_file),
                                           "--output", str(self.trend_image)])
        if not ok:
            self.log("Trend image build failed; continuing (email body will omit the trend chart).")

        if not send_email:
            self.log("Email step skipped (--no-email).")
        else:
            try:
                self.send_report_email()
            except Exception as e:
                self.log(f"Email failed: {e}")
                self.log("=== Run finished with email failure ===")
                return 1

        self.log(f"=== Daily {self.agency_name} report run finished OK ===")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agency", default="HTX",
                        help="Agency keyword to filter Careers@Gov postings on (matched against the agency field)")
    parser.add_argument("--agency-name", default=None,
                        help="Agency display name for report titles/prompts (default: same as --agency)")
    parser.add_argument("--prefix", default=None,
                        help="Filename prefix for all data files (default: agency keyword lowercased)")
    parser.add_argument("--workdir", default=".",
                        help="Directory holding the data files and .env (default: current directory)")
    parser.add_argument("--no-email", action="store_true", help="Skip the email step")
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    load_dotenv(workdir / ".env", override=True)

    agency_name = args.agency_name or args.agency
    prefix = args.prefix or args.agency.strip().lower().replace(" ", "_")

    pipeline = Pipeline(workdir, args.agency, agency_name, prefix)
    return pipeline.run(send_email=not args.no_email)


if __name__ == "__main__":
    sys.exit(main())
