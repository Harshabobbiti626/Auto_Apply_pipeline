# 🔔 BLR Job Alert Pipeline

**Fully automated hourly job radar for Bangalore — built for a ~2-year Java / Spring Boot / React engineer.**

No job boards to refresh, no missed postings. Every hour this pipeline scans 46 free, public
job sources, applies a strict profile filter, and emails only brand-new roles that genuinely
fit — straight to your inbox within ~60 minutes of them being posted.

![python](https://img.shields.io/badge/python-3.11-blue) ![actions](https://img.shields.io/badge/github%20actions-hourly-green) ![cost](https://img.shields.io/badge/cost-%240%2Fmo-success)

---

## What it does

```
GitHub Actions (hourly cron)
        │
        ▼
Fetch 46 public sources (parallel, ~15s)
  Greenhouse · Lever · SmartRecruiters · Workable · Jobicy · Arbeitnow
  + Adzuna India (aggregator) + JSearch (optional)
        │
        ▼
Filter gauntlet
  1. Title level   — blocks senior/staff/lead/sse/manager/ml/trainee…
  2. Title stack   — java · spring · backend · full-stack · react · sde · node
  3. Location      — Bangalore / BLR / Karnataka / Remote-India only
  4. Profile score — resume keywords vs full job description
  5. Experience    — parses "4-7 yrs", "5+ years" → drops anything outside ~2 yrs
  6. Freshness     — only jobs published in the last 26 hours
        │
        ▼
Dedupe against seen-jobs state → ✉️ email NEW matches only
```

Every email shows: **company (size) · role · location · posted-when · profile-match keywords ·
one-click apply link**. Days with no matches = no email. Silence means clean, not broken.

## Coverage

| Source | What it adds | Key needed |
|---|---|---|
| Greenhouse + Lever (44 curated companies) | Bangalore HQ/branch product companies & GCCs in the 500–10,000 employee band — Databricks, Cloudflare, Okta, Coinbase, Postman, Groww, CRED, Meesho, Zeta, Porter… | — |
| SmartRecruiters / Workable | companies on other ATSs (extensible in one line) | — |
| Jobicy + Arbeitnow | global remote boards — India-eligible roles only pass the filter | — |
| [Adzuna India](https://developer.adzuna.com) (free key) | aggregates dozens of smaller boards; Bengaluru, ≤1 day old | `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` |
| [JSearch on RapidAPI](https://rapidapi.com/) (paid ~$10/mo) | the only clean route to LinkedIn / Indeed / Naukri listings | `JSEARCH_API_KEY` |
| Naukri / LinkedIn / Indeed direct | ❌ intentionally excluded — anti-bot walls + ToS violations; account-ban risk | — |

Company size (500–10,000) is enforced via the **curated list** — job APIs don't expose
headcounts, so the list is the filter. Dead boards are skipped automatically, never crash runs.

## Setup (one-time, ~10 min)

Full walkthrough in **[JOB_PIPELINE_SETUP.md](JOB_PIPELINE_SETUP.md)**. Short version:

1. Add repo secrets: `GMAIL_USER`, `GMAIL_APP_PASSWORD` ([Google App Password](https://myaccount.google.com/apppasswords), needs 2FA), `ALERT_TO`
2. Actions tab → *Bangalore job alerts* → **Run workflow** → first run sends a "pipeline live" email with everything currently open
3. Done — the hourly schedule takes over

Optional: add `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` (free) or `JSEARCH_API_KEY` (paid) as secrets to widen coverage — no other change needed.

## Configuration — `job_pipeline/companies.yaml`

| Setting | Default | Meaning |
|---|---|---|
| `include_titles` / `exclude_titles` | tuned for Java/React backend, ~2 yrs | title keyword gates |
| `location_include` + `remote_india_ok` | Bangalore/BLR/Karnataka | location gate |
| `profile_keywords` + `min_profile_score` | resume skills, score ≥ 4 | description-level matching |
| `aggregator_min_profile_score` | 6 | stricter bar for Adzuna/JSearch (agency spam) |
| `experience_target_years` / `experience_max_asks` / `experience_min_asks` | 2 / 3 / 1.5 | explicit-range experience fit |
| `max_job_age_hours` | 26 | "posted today" freshness |
| `companies:` | 44 verified boards | add any Greenhouse/Lever/SmartRecruiters/Workable board as one YAML line |

## Reliability

- ⏱️ Hourly cron at :07 UTC; GitHub delays are typically a few minutes — always inside the hour
- 💾 Seen-jobs state travels via Actions **cache + daily git snapshot** → no duplicate emails even after cache loss
- 📧 **Failure alerts**: a broken run emails you automatically with the log link
- 🔁 **Daily keepalive commit** prevents GitHub's 60-day-inactivity auto-disable
- 🧯 Dead/misconfigured boards are logged and skipped; the rest of the run is unaffected

## Repository structure

```
├── .github/workflows/job-alerts.yml   # hourly schedule + state snapshot + failure alerts
├── job_pipeline/
│   ├── fetch_jobs.py                  # fetch → filter → dedupe → email (single script)
│   ├── companies.yaml                 # curated companies + all filter settings
│   ├── requirements.txt
│   ├── state/seen_jobs.json           # dedupe state (auto-managed)
│   └── outbox/                        # per-run markdown reports (git-ignored)
├── JOB_PIPELINE_SETUP.md              # detailed setup + troubleshooting
└── README.md
```

## Notes & limits

- **Auto-apply is deliberately out of scope** — automating applications on LinkedIn/Naukri
  violates their ToS and risks account suspension; this pipeline instead delivers fresh,
  relevant roles with one-click apply links, which is the 95% win with 0% risk.
- Adzuna/JSearch listings come from agencies and secondary boards — the stricter score +
  experience parsing exists exactly because of that.
- Curated company headcounts are approximate (shown as `~` in emails).
