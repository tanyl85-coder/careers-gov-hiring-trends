# Careers@Gov Hiring Trends

Automated hiring-trend tracking for any Singapore public-service agency listed on
[Careers@Gov](https://jobs.careers.gov.sg): crawl job postings daily, extract the skills /
job level / division from each job description with the Claude API, and produce a dashboard
and an emailed report showing what an agency is hiring for and how that demand shifts over time.

Built for talent/HR strategy work — turning a job board (built for job seekers reading one
posting at a time) into labour-market intelligence read in aggregate.

## What it produces

| Output | Contents |
|---|---|
| `<prefix>_job_history.csv` | Accumulating archive keyed by Job ID with First Seen / Last Seen. Postings disappear from the live feed once filled or closed, so this is the source of truth for trends. |
| `<prefix>_skills_analysis.xlsx` | 8 sheets: Postings (with Division, Job Level Raw/Band, extracted skills), Skills Detail, Category Trend (Monthly + Weekly), Top Skills, Skills by Division, Skills by Level, Headcount Trend |
| `<prefix>_dashboard.html` | Self-contained dashboard — KPIs, weekly skills-demand trend, category/skill/division/level breakdowns, division × category heatmap. No JavaScript, no external assets. |
| `comparison_dashboard.html` | Cross-agency comparison — demand normalised to *share of each agency's postings*, so a 25-posting agency and a 290-posting one are comparable |
| Email | The dashboard rendered as the email body (scrollable native HTML), with the workbook attached |

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env      # then add your Anthropic API key
```

Find the agency's name as it appears in the data source, then run the pipeline:

```bash
python scripts/careers_gov_crawler.py --list-agencies
python scripts/daily_report.py --agency "Home Team Science" --agency-name "HTX" --prefix htx --workdir ./data --no-email
```

Cross-agency comparison, once two or more pipelines share a workdir:

```bash
python scripts/weekly_comparison_report.py --agencies "htx:HTX,govtech:GovTech,dsta:DSTA" --workdir ./data --no-email
```

Drop `--no-email` to send the report (see *Email* below).

## Data source

Job data comes from the **Open Government Products public mirror** of the Careers@Gov feed,
refreshed daily:

```
https://raw.githubusercontent.com/opengovsg/careersgovsg-jobs-data/main/data/job-listings.json
```

This is the same data the jobs.careers.gov.sg search UI reads (~2,300 postings across ~90 agencies).
OGP builds Careers@Gov and publishes this mirror; the underlying SAP HRP backend is not public.

Three things that are easy to get wrong, and are handled here:

1. **Agency names in the feed are full names, not codes** — `Government Technology Agency`, not
   "GovTech"; `Monetary Authority of Singapore`, not "MAS". `--agency` is a case-insensitive
   substring match, so also beware false positives (`MAS` matches Te**mas**ek Polytechnic).
   Use `--list-agencies` to discover exact names. If a keyword matches more than one agency the
   crawler aborts rather than silently mixing agencies into one dataset.
2. **Postings with no closing date are open-until-filled and count as active.** GovTech lists
   ~96% of its jobs this way; treating them as expired shrinks it from ~290 postings to ~10.
3. **"Active" means present in the latest crawl**, not "closing date in the future" — agencies
   delist roles early once filled, so closing-date counting overstates the live book.

## How the analysis works

Each posting goes to Claude once (`claude-sonnet-4-5`) and the result is cached per Job ID in
`<prefix>_skills_cache.json`, so re-runs only pay for newly-seen postings. Roughly US$0.005 per
posting; a daily run on a tracked agency is usually cents or free.

- **Skills** — 3–15 specific skills per posting, each mapped to one of 13 fixed categories.
  `skill_normalize.py` merges variant spellings so tallies aren't split: formatting variants
  (`Machine Learning` / `Machine learning`, `Problem-Solving` / `Problem Solving`) collapse
  automatically, and a curated whitelist handles acronym↔expansion pairs (`AWS` / `Amazon Web
  Services`, `SRE`, `NLP`, `RAG`, `IAM`, `CI/CD`…). Acronyms are never auto-merged — that
  produces false positives — and sub-products like `AWS Glue` stay distinct from `AWS`.
- **Job level** — two columns: the raw title wording (`Lead Engineer/Engineer`) and a
  standardised band (Engineer/Officer → Senior/Lead → Principal → Manager → Head → Deputy
  Director → Director & Above). Dual-level titles take the **higher** band, so the band reflects
  how roles are titled and somewhat overstates required seniority.
- **Division** — inferred from title suffixes and JD text (Singapore agencies often encode the
  division, e.g. `…, Red Team, xCyber`), normalising obvious variants.
- **Weekly trend** — a posting counts toward every week its posted→closing window overlaps, so
  trends are available retroactively rather than only after weeks of snapshots. Caveat: weeks
  before the first crawl undercount, because postings that closed earlier were never captured.

## Email

Set in `.env`:

```
SMTP_USER=you@gmail.com          # the account that generated the app password
SMTP_APP_PASSWORD=xxxxxxxxxxxxxxxx
REPORT_EMAIL_TO=recipient@example.com
```

Use a Gmail [App Password](https://myaccount.google.com/apppasswords) (requires 2FA) — a mismatch
between `SMTP_USER` and the account that generated it returns `535 BadCredentials`.

The email body *is* the dashboard, rendered as email-safe HTML (tables + inline styles, kept
under Gmail's ~102 KB clipping limit). Two deliberate constraints behind that design: Gmail never
renders HTML *attachments* (it shows their source as text), and a full-page dashboard image can't
scroll or reflow. Only the trend line chart is an image, embedded via CID.

## Scheduling

Any scheduler works — the scripts are plain CLI. A useful split is a **daily crawl** (so
short-lived postings aren't missed) plus a **weekly emailed report**:

```powershell
# Windows Task Scheduler — daily data collection, no email
$py = (Get-Command python).Source
$args = "scripts\daily_report.py --agency `"Home Team Science`" --agency-name HTX --prefix htx --workdir `"$PWD\data`""
Register-ScheduledTask -TaskName "HTX Daily Crawl" `
  -Action (New-ScheduledTaskAction -Execute $py -Argument "$args --no-email") `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 8:00am) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun)
```

`-StartWhenAvailable` matters: without it a task whose start time passes while the machine is
asleep is simply skipped for that day.

## Use as a Claude Code skill

`careers-gov-hiring-trends.skill` packages this as an installable
[Claude Code](https://claude.com/claude-code) skill — Claude then sets up and maintains pipelines
conversationally ("track what DSTA is hiring for"), with the pitfalls above already encoded.
`SKILL.md` holds the instructions.

## Limitations

- Sees only what agencies publish on Careers@Gov; agencies that also recruit through their own
  channels will be understated.
- Skills, levels and divisions are Claude's interpretation of free-text JDs — directionally
  reliable in aggregate, not audit-grade per posting.
- Strategic sensing, not a recruiting tool: it reads the market, not a candidate pipeline.

## Licence

MIT
