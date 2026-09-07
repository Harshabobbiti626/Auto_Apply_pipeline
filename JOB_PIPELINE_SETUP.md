# 🔔 Bangalore Job Alert Pipeline — Setup Guide

Fully automated: **every hour**, checks career pages of 31 curated Bangalore-hiring companies
(500–10,000 employees) for new **Java / Spring Boot / Backend / Full-stack / React** roles at your
level, and emails you only the **new** ones — within ~1 hour of them being posted.

Runs free on GitHub Actions (no PC needed). Source: Greenhouse + Lever boards (fresh listings,
no scraping, no ToS issues).

```
GitHub Actions (hourly cron) → fetch_jobs.py → filter (BLR + role + level)
  → dedupe vs seen-jobs state → email via Gmail SMTP → you apply from the link
```

---

## One-time setup (≈10 minutes)

### 1. Create the GitHub repo & push

This folder's git history points at your *other* project, so start it fresh as its own repo:

```bash
cd "C:\Users\YASWANTH\Downloads\Applications flow"

# Create an EMPTY private repo named blr-job-alerts on github.com first (no README!)

git init -b main
git add .
git commit -m "BLR job alert pipeline"
git remote add origin https://github.com/<YOUR_USERNAME>/blr-job-alerts.git
git push -u origin main
```

> Private repo = your resume PDF stays private. Delete `Harshavardhan_Java+_react.pdf` before
> pushing if you don't want it there at all.

### 2. Get a Gmail App Password (2 min)

1. Go to https://myaccount.google.com/security
2. Make sure **2-Step Verification is ON** (required for app passwords)
3. Visit https://myaccount.google.com/apppasswords → create one (name: "job-alerts")
4. Copy the **16-character password**

### 3. Add GitHub secrets

In your new repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name         | Value                                        |
|---------------------|----------------------------------------------|
| `GMAIL_USER`        | `harshabobbiti626@gmail.com` (sender)        |
| `GMAIL_APP_PASSWORD`| the 16-char app password (no spaces)         |
| `ALERT_TO`          | your inbox (can be same address)             |

### 4. First run + verify

Repo → **Actions** tab → *Bangalore job alerts* → **Run workflow**.

- First run sends a **"✅ Job pipeline live"** email listing current matching jobs (no spam-blast)
- Every run after that emails **only jobs posted since the last hourly check**
- Check the spam folder once and mark the sender as "not spam"

Done. From now on: new matching job posted → email in your inbox within the hour.

---

## Fine-tuning (edit `job_pipeline/companies.yaml`)

- **Add a company**: find its board — Greenhouse: `https://job-boards.greenhouse.io/<slug>`,
  Lever: `https://jobs.lever.co/<slug>` — then add:
  ```yaml
  - {name: Swiggy-like co, ats: lever, slug: that-slug, size: "~2,000"}
  ```
- **Re-include senior roles**: delete `senior`, `sr.`, `"sr "`, `staff`, `principal` from `exclude_titles`
- **Narrow roles**: remove keywords from `include_titles` (e.g. drop `react` for backend-only)
- **Only strict Bangalore** (drop Remote-India matches): set `remote_india_ok: false`
- **Faster than hourly**: in `.github/workflows/job-alerts.yml` change cron to `"*/30 * * * *"`

## How state/dedup works

Seen job IDs travel between runs via the Actions cache, with a **daily snapshot committed to the
repo** (07:07 UTC run) as a durable fallback — so you never get duplicate emails even if the
cache is evicted. Failed/dead boards are skipped and logged, never crash the run.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No email at all | Check Actions run log — red ❌ on "Fetch jobs" usually = wrong app password |
| `SmtpAuthenticationError` | Re-create the app password; paste without spaces |
| Runs delayed | GitHub scheduled runs can lag a few minutes — normal; jobs still arrive well within the hour |
| Want to stop | Delete the workflow file or disable the workflow in the Actions tab |
| Scheduled runs auto-disabled | GitHub does this after 60 days of zero repo activity — the daily snapshot commit keeps it alive |
