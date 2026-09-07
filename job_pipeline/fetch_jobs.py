#!/usr/bin/env python3
"""
Bangalore Job Alert Pipeline
============================
Fetches live openings from Greenhouse/Lever career boards of curated companies
(~500-10,000 employees), filters for Bangalore + Java/Spring Boot/backend/
full-stack/React roles, dedupes against a seen-jobs state file, and emails
every NEW match. Designed to run hourly on GitHub Actions.

Env vars:
    GMAIL_USER          Gmail address used to SEND alerts
    GMAIL_APP_PASSWORD  16-char Google App Password (not your login password)
    ALERT_TO            Recipient (defaults to GMAIL_USER)
    DRY_RUN=1           Don't send email or persist state; write preview to outbox/
    MAX_EMAIL_JOBS      Cap rows per email (overrides settings)
"""

import html as html_mod
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import yaml
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(ROOT, "companies.yaml"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = {"User-Agent": "blr-job-alerts/1.0 (github-actions)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("job-alerts")

STATE_VERSION = 2
STATE_MAX_AGE_DAYS = 120


# ---------------------------------------------------------------- config/state
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("settings", {})
    cfg["settings"].setdefault("include_titles", [])
    cfg["settings"].setdefault("exclude_titles", [])
    cfg["settings"].setdefault("location_include", [])
    cfg["settings"].setdefault("remote_india_ok", True)
    cfg["settings"].setdefault("max_email_jobs", 30)
    return cfg


def state_path(cfg):
    rel = cfg["settings"].get("state_path", "state/seen_jobs.json")
    p = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
    return p


def load_state(cfg):
    """Returns (seen_by_source_id, seen_by_role, existed_before_run)."""
    p = state_path(cfg)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("seen", {}), data.get("roles", {}), True
        except (json.JSONDecodeError, OSError) as e:
            log.warning("State file unreadable (%s) — reseeding: %s", e, p)
    return {}, {}, False


def save_state(cfg, seen, roles):
    if DRY_RUN:
        log.info("DRY_RUN — state not persisted")
        return
    p = state_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_MAX_AGE_DAYS)

    def fresh(entries):
        kept = {}
        for k, iso in entries.items():
            try:
                if datetime.fromisoformat(iso) >= cutoff:
                    kept[k] = iso
            except ValueError:
                kept[k] = iso  # keep malformed entries rather than re-alerting
        return kept

    with open(p, "w", encoding="utf-8") as f:
        json.dump({"version": STATE_VERSION, "seen": fresh(seen), "roles": fresh(roles)},
                  f, indent=1)
    log.info("State saved: %d jobs / %d roles seen (%s)", len(seen), len(roles), p)


# ---------------------------------------------------------------- fetchers
def _get(url):
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(slug):
    """Normalized jobs from a Greenhouse board."""
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": str(j.get("id", "")),
            "title": j.get("title") or "",
            "url": j.get("absolute_url") or "",
            "location": (j.get("location") or {}).get("name") or "",
            # prefer first_published (true posting date) — updated_at bumps on edits
            "posted": j.get("first_published") or j.get("updated_at"),
            "desc": j.get("content") or "",
        })
    return jobs


def fetch_lever(slug):
    """Normalized jobs from a Lever board."""
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        posted_ms = j.get("createdAt")
        jobs.append({
            "id": str(j.get("id", "")),
            "title": j.get("text") or "",
            "url": j.get("hostedUrl") or j.get("applyUrl") or "",
            "location": (j.get("categories") or {}).get("location") or "",
            "posted": datetime.fromtimestamp(posted_ms / 1000, tz=timezone.utc).isoformat() if posted_ms else None,
            "desc": j.get("descriptionPlain") or j.get("description") or "",
        })
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever}


class SkipSource(Exception):
    """Optional source missing its API key — not a failure."""


def fetch_smartrecruiters(slug):
    """SmartRecruiters public postings API (descriptions fetched lazily per matched job)."""
    data = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    jobs = []
    for j in data.get("content") or []:
        loc = j.get("location") or {}
        jobs.append({
            "id": str(j.get("id", "")),
            "title": j.get("name") or "",
            "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            "location": ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
            "posted": j.get("releasedDate"),
            "desc": "",
        })
    return jobs


def smartrecruiters_detail(slug, job_id):
    return _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}")


def fetch_workable(slug):
    """Workable public widget API."""
    r = requests.post(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
                      json={"query": "", "location": [], "department": [], "worktype": []},
                      headers=UA, timeout=25)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("results") or []:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        jobs.append({
            "id": str(j.get("id") or j.get("shortlink") or j.get("title")),
            "title": j.get("title") or "",
            "url": j.get("shortlink") or j.get("url") or "",
            "location": loc,
            "posted": j.get("created_at"),
            "desc": "",
        })
    return jobs


def fetch_jobicy(_slug):
    """Global remote board (free, keyless). Only India-eligible roles pass the location filter."""
    data = _get("https://jobicy.com/api/v2/remote-jobs?count=50")
    jobs = []
    for j in data.get("jobs") or []:
        jobs.append({
            "id": str(j.get("id", "")),
            "title": j.get("jobTitle") or "",
            "url": j.get("url") or "",
            "location": f"{j.get('jobGeo') or ''} (remote)",
            "posted": j.get("pubDate"),
            "desc": j.get("jobExcerpt") or j.get("jobDescription") or "",
            "company_override": j.get("companyName") or "",
        })
    return jobs


def fetch_arbeitnow(_slug):
    """Global job board (free, keyless). Only India-eligible roles pass the location filter."""
    data = _get("https://www.arbeitnow.com/api/job-board-api")
    jobs = []
    for j in data.get("data") or []:
        created = None
        ts = j.get("created_at")
        if ts:
            try:
                created = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        jobs.append({
            "id": str(j.get("slug") or j.get("id") or j.get("title")),
            "title": j.get("title") or "",
            "url": j.get("url") or f"https://www.arbeitnow.com/jobs/{j.get('slug', '')}",
            "location": f"{j.get('location') or ''}{' (remote)' if j.get('remote') else ''}",
            "posted": created,
            "desc": strip_html(j.get("description") or "", limit=1500),
            "company_override": j.get("company_name") or "",
        })
    return jobs


def fetch_adzuna(_slug):
    """Adzuna India aggregator — free key required (developer.adzuna.com)."""
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        raise SkipSource("ADZUNA_APP_ID / ADZUNA_APP_KEY not set — get a free key at developer.adzuna.com")
    data = _get("https://api.adzuna.com/v1/api/jobs/in/search/1"
                f"?app_id={app_id}&app_key={app_key}&results_per_page=50&max_days_old=1"
                f"&what_or={urllib.parse.quote('java spring boot react full stack backend')}"
                f"&where={urllib.parse.quote('bengaluru')}")
    jobs = []
    for j in data.get("results") or []:
        jobs.append({
            "id": str(j.get("id", "")),
            "title": j.get("title") or "",
            "url": j.get("redirect_url") or "",
            "location": (j.get("location") or {}).get("display_name") or "",
            "posted": j.get("created"),
            "desc": strip_html(j.get("description") or "", limit=1500),
            "company_override": (j.get("company") or {}).get("display_name") or "",
        })
    return jobs


def fetch_jsearch(_slug):
    """JSearch (RapidAPI) — aggregates LinkedIn/Indeed/Naukri/Glassdoor. Paid key required."""
    key = os.environ.get("JSEARCH_API_KEY")
    if not key:
        raise SkipSource("JSEARCH_API_KEY not set — optional paid source on RapidAPI")
    r = requests.get("https://jsearch.p.rapidapi.com/search",
                     params={"query": "java spring boot backend developer jobs in Bengaluru India",
                             "num_pages": 1, "date_posted": "today"},
                     headers={**UA, "X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
                     timeout=25)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("data") or []:
        loc = ", ".join(x for x in (j.get("job_city"), j.get("job_country")) if x)
        jobs.append({
            "id": str(j.get("job_id", "")),
            "title": j.get("job_title") or "",
            "url": j.get("job_apply_link") or j.get("job_google_link") or "",
            "location": loc or ("remote (india)" if j.get("job_is_remote") else ""),
            "posted": j.get("job_posted_at_datetime_utc"),
            "desc": strip_html(j.get("job_description") or "", limit=1500),
            "company_override": j.get("employer_name") or "",
        })
    return jobs


FETCHERS.update({
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "jobicy": fetch_jobicy,
    "arbeitnow": fetch_arbeitnow,
    "adzuna": fetch_adzuna,
    "jsearch": fetch_jsearch,
})


# ---------------------------------------------------------------- filtering
def strip_html(raw, limit=220):
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _kw_hit(text, kw):
    """Short tokens (sde, sse, sr, ml, qa) match on word boundaries only."""
    kw = (kw or "").lower()
    if len(kw) <= 4 and kw.isalnum():
        return re.search(r"\b" + re.escape(kw) + r"\b", text) is not None
    return kw in text


def title_allowed(title, s):
    t = (title or "").lower()
    if any(_kw_hit(t, x) for x in s["exclude_titles"]):
        return False
    return any(_kw_hit(t, x) for x in s["include_titles"])


EXP_RE = re.compile(r"\b(\d{1,2})(?:\s*(?:\+|plus)|\s*(?:to|–|—|-)\s*(\d{1,2}))?\s*(?:years?|yrs?)\b", re.I)


def experience_fit(title, desc, s):
    """False only when an explicit experience range clearly doesn't fit (~2 yrs).
    Uses the most permissive reading when several ranges appear in the text."""
    target_max_asks = float(s.get("experience_max_asks", 3))
    target_min_asks = float(s.get("experience_min_asks", 1.5))
    mins, maxs = [], []
    for m in EXP_RE.finditer(f"{title or ''} {desc or ''}"):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        mins.append(lo)
        maxs.append(hi)
    if not mins:
        return True
    if min(mins) > target_max_asks:      # e.g. "5-8 years" — too senior
        return False
    if max(maxs) < target_min_asks:      # e.g. "0-1 years" — fresher level
        return False
    return True


def location_allowed(job, s):
    text = f"{job['location']} {job['title']}".lower()
    if any(x in text for x in s["location_include"]):
        return True
    return bool(s["remote_india_ok"] and "remote" in text and "india" in text)


def score_job(job, s):
    """Profile match score from resume keywords. Title hit = weight x2, desc hit = weight x1."""
    title = (job["title"] or "").lower()
    desc = re.sub(r"\s+", " ", strip_html(job["desc"], limit=6000)).lower()
    score, hits = 0, []
    for kw, weight in (s.get("profile_keywords") or {}).items():
        pts = (weight * 2 if kw in title else 0) + (weight if kw in desc else 0)
        if pts:
            score += pts
            hits.append(kw)
    return score, hits


def is_fresh(iso, max_hours):
    """True if the job was published within the last `max_hours` (or age unknown)."""
    if not iso:
        return True
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) <= timedelta(hours=max_hours)
    except ValueError:
        return True


def rel_time(iso):
    if not iso:
        return "recent"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hours = int((datetime.now(timezone.utc) - dt).total_seconds() // 3600)
        if hours < 1:
            return "just now"
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except ValueError:
        return "recent"


# ---------------------------------------------------------------- email
def build_email(rows, intro=False):
    max_rows = int(os.environ.get("MAX_EMAIL_JOBS", 0)) or max_email_jobs_global
    shown, extra = rows[:max_rows], max(0, len(rows) - max_rows)

    head = (
        "<h2 style='margin:0 0 4px'>🔔 New Bangalore jobs at 500–10,000-person companies</h2>"
        f"<p style='margin:0 0 16px;color:#555'>{len(rows)} new match(es) · generated "
        f"{datetime.now(IST):%a %d %b, %I:%M %p} IST</p>"
    )
    if intro:
        head += ("<p style='background:#e8f5e9;padding:10px;border-radius:8px'>✅ <b>Your pipeline is live.</b> "
                 "You'll get an email within an hour of any matching job being posted. "
                 "These are the matching roles open right now:</p>")

    body_rows = []
    for r in shown:
        kws = " · ".join(r["kws"][:6])
        body_rows.append(f"""
        <tr>
          <td style='padding:10px 8px;border-bottom:1px solid #eee;vertical-align:top;white-space:nowrap'>
            <b>{html_mod.escape(r['company'])}</b><br>
            <span style='color:#888;font-size:12px'>{html_mod.escape(r['size'])}</span></td>
          <td style='padding:10px 8px;border-bottom:1px solid #eee'>
            <a href='{r['url']}' style='font-size:15px;font-weight:600;color:#1a56db;text-decoration:none'>
              {html_mod.escape(r['title'])}</a><br>
            <span style='color:#666;font-size:13px'>{html_mod.escape(r['location'] or '—')}
              · posted {rel_time(r['posted'])}</span>
            <br><span style='color:#777;font-size:12px'>{html_mod.escape(r['snippet'])}</span>
            <br><span style='color:#1a56db;font-size:12px'>Profile match: {html_mod.escape(kws)}</span></td>
        </tr>""")

    tail = f"<p style='color:#888;font-size:12px'>+ {extra} more (see previous emails / report)</p>" if extra else ""
    foot = ("<p style='color:#aaa;font-size:11px;margin-top:20px'>Hourly Bangalore job pipeline · "
            "Greenhouse/Lever boards · reply STOP-equivalent by deleting the Actions workflow to stop.</p>")

    html = (f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:760px;margin:auto'>{head}"
            f"<table style='border-collapse:collapse;width:100%'>{''.join(body_rows)}</table>{tail}{foot}</div>")

    lines = [f"{r['company']} ({r['size']}) - {r['title']} [{r['location']}] ({rel_time(r['posted'])})\n"
             f"  Match: {', '.join(r['kws'][:6])}\n  {r['url']}\n"
             for r in shown]
    text = f"NEW BANGALORE JOBS ({len(rows)}):\n\n" + "\n".join(lines)
    return html, text


def send_email(subject, html_body, text_body):
    send_mime(_multipart(subject, html_body, text_body))
    log.info("Email sent to %s", os.environ.get("ALERT_TO") or os.environ["GMAIL_USER"])


def _multipart(subject, html_body, text_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"BLR Job Alerts <{os.environ['GMAIL_USER']}>"
    msg["To"] = os.environ.get("ALERT_TO") or os.environ["GMAIL_USER"]
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def send_text_email(subject, text_body):
    send_mime(_multipart(subject, "", text_body))


def send_mime(msg):
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def notify_failure(message):
    """Used by the workflow's failure step: email the user that a run failed."""
    subject = "BLR job pipeline - hourly run FAILED"
    body = (f"{message}\n\nThe hourly job-alert run failed. Open the Actions tab to inspect the log.\n"
            "No jobs were lost - the next hourly run continues from the last saved state.")
    try:
        send_text_email(subject, body)
        log.info("Failure alert email sent")
        return 0
    except Exception as e:
        log.error("Could not send failure alert: %s", e)
        return 1


# ---------------------------------------------------------------- main
def main():
    global max_email_jobs_global
    if len(sys.argv) > 1 and sys.argv[1] == "--notify":  # failure-alert mode (see workflow)
        return notify_failure(" ".join(sys.argv[2:]) or "Hourly run failed.")

    cfg = load_config()
    s = cfg["settings"]
    max_email_jobs_global = s["max_email_jobs"]

    seen, roles, existed = load_state(cfg)
    now_iso = datetime.now(timezone.utc).isoformat()

    def fetch_company(company):
        return company, FETCHERS[company["ats"]](company["slug"])

    all_matches, new_jobs, failed = [], [], []
    min_score = s.get("min_profile_score", 4)
    agg_score = s.get("aggregator_min_profile_score", 6)
    max_age = float(s.get("max_job_age_hours", 26))
    skipped_old = skipped_exp = dup_skipped = 0
    seen_roles_run = set()

    companies = list(cfg["companies"])
    # Optional aggregator sources — activated only when their API key secret exists
    if os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"):
        companies.append({"name": "Adzuna", "ats": "adzuna", "slug": "-", "size": "aggregator"})
    if os.environ.get("JSEARCH_API_KEY"):
        companies.append({"name": "JSearch", "ats": "jsearch", "slug": "-", "size": "aggregator"})

    with ThreadPoolExecutor(max_workers=12) as pool:
        future_map = {pool.submit(fetch_company, c): c for c in companies}
        for fut in as_completed(future_map):
            company = future_map[fut]
            name, slug = company["name"], company["slug"]
            try:
                _, jobs = fut.result()
            except SkipSource as e:
                log.info("Optional source disabled: %s", e)
                continue
            except Exception as e:  # dead board / network hiccup — skip gracefully
                failed.append(f"{name} ({slug}): {e}")
                log.warning("Board failed, skipping: %s (%s) — %s", name, slug, e)
                continue

            kept = 0
            for job in jobs:
                if not title_allowed(job["title"], s) or not location_allowed(job, s):
                    continue
                if company["ats"] == "smartrecruiters" and not job["desc"]:
                    try:  # score on the full description, not just the title
                        det = smartrecruiters_detail(slug, job["id"])
                        job["desc"] = det.get("description") or ""
                    except Exception:
                        pass
                score, kws = score_job(job, s)
                eff_min = agg_score if company["ats"] in ("adzuna", "jsearch") else min_score
                if score < eff_min:  # not an exact profile match (aggregators demand more)
                    continue
                if not experience_fit(job["title"], strip_html(job["desc"], limit=6000), s):
                    skipped_exp += 1  # posting states a range outside ~2 years
                    continue
                if not is_fresh(job["posted"], max_age):  # older than "posted today"
                    skipped_old += 1
                    continue
                match = {**job, "company": job.get("company_override") or name,
                         "size": company.get("size", ""), "ats": company["ats"],
                         "snippet": strip_html(job["desc"]), "score": score, "kws": kws}
                key = f"{name}:{job['id']}"
                # role-level dedupe: same opening mirrored across sources must
                # never email twice (company + title + location identify a role)
                role_key = f"{match['company'].lower()}|{match['title'].lower()}|{(match['location'] or '').lower()}"
                if role_key not in seen_roles_run:
                    seen_roles_run.add(role_key)
                    all_matches.append(match)
                    kept += 1
                if key not in seen and role_key not in roles:
                    seen[key] = now_iso
                    roles[role_key] = now_iso
                    new_jobs.append(match)
                elif key not in seen:
                    seen[key] = now_iso  # same role seen via another source — mark, don't email
                    dup_skipped += 1
            log.info("%-15s %2d/%2d jobs match profile", name, kept, len(jobs))

    new_jobs.sort(key=lambda r: (-r["score"], r["company"]))
    log.info("Total open matches: %d | NEW: %d | role-dupes suppressed: %d | "
             "wrong-experience skipped: %d | older-than-%dh skipped: %d | boards failed: %d",
             len(all_matches), len(new_jobs), dup_skipped, skipped_exp, max_age,
             skipped_old, len(failed))

    outbox = os.path.join(ROOT, s.get("outbox_path", "outbox"))
    os.makedirs(outbox, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M")
    with open(os.path.join(outbox, f"report_{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(f"# Run {stamp} IST — {len(all_matches)} open matches, {len(new_jobs)} new\n\n")
        for r in sorted(all_matches, key=lambda x: (-x["score"], x["company"])):
            f.write(f"- **{r['company']}** ({r['size']}) — [{r['title']}]({r['url']}) — {r['location']}"
                    f" — match {r['score']}: {', '.join(r['kws'][:6])}\n")
        if failed:
            f.write("\n## Failed boards\n" + "\n".join(f"- {x}" for x in failed) + "\n")

    if DRY_RUN:
        print(f"[DRY RUN] would process {len(new_jobs)} new job(s); state NOT saved; no email sent")
        return 0

    save_state(cfg, seen, roles)

    if not existed:  # first run: seed silently + intro email (avoid blasting hundreds)
        sample = sorted(all_matches, key=lambda r: (-r["score"], r["company"]))[:15]
        html_body, text_body = build_email(sample, intro=True)
        subject = f"✅ Job pipeline live — {len(all_matches)} matching Bangalore jobs open now"
    elif new_jobs:
        html_body, text_body = build_email(new_jobs)
        comps = ", ".join(dict.fromkeys(r["company"] for r in new_jobs))
        prefix = ", ".join(comps.split(", ")[:4]) + ("…" if len(comps.split(", ")) > 4 else "")
        subject = f"🔥 {len(new_jobs)} new Bangalore job(s) — {prefix}"
    else:
        log.info("No new matching jobs this run — no email sent")
        return 0

    try:
        send_email(subject, html_body, text_body)
    except KeyError as e:
        log.error("Missing secret env var %s — set GMAIL_USER / GMAIL_APP_PASSWORD in GitHub secrets "
                  "(or run with DRY_RUN=1 locally). Email NOT sent, but state was saved.", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
