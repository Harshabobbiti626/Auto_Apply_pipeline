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

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(ROOT, "companies.yaml"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = {"User-Agent": "blr-job-alerts/1.0 (github-actions)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("job-alerts")

STATE_VERSION = 1
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
    """Returns (state_dict, existed_before_run)."""
    p = state_path(cfg)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("seen", {}), True
        except (json.JSONDecodeError, OSError) as e:
            log.warning("State file unreadable (%s) — reseeding: %s", e, p)
    return {}, False


def save_state(cfg, seen):
    if DRY_RUN:
        log.info("DRY_RUN — state not persisted")
        return
    p = state_path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_MAX_AGE_DAYS)
    pruned = {}
    for k, iso in seen.items():
        try:
            if datetime.fromisoformat(iso) >= cutoff:
                pruned[k] = iso
        except ValueError:
            pruned[k] = iso  # keep malformed entries rather than re-alerting
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"version": STATE_VERSION, "seen": pruned}, f, indent=1)
    log.info("State saved: %d jobs seen (%s)", len(pruned), p)


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
            "posted": j.get("updated_at") or j.get("first_published"),
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


# ---------------------------------------------------------------- filtering
def strip_html(raw, limit=220):
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def title_allowed(title, s):
    t = (title or "").lower()
    if any(x in t for x in s["exclude_titles"]):
        return False
    return any(x in t for x in s["include_titles"])


def location_allowed(job, s):
    text = f"{job['location']} {job['title']}".lower()
    if any(x in text for x in s["location_include"]):
        return True
    return bool(s["remote_india_ok"] and "remote" in text and "india" in text)


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
            <br><span style='color:#777;font-size:12px'>{html_mod.escape(r['snippet'])}</span></td>
        </tr>""")

    tail = f"<p style='color:#888;font-size:12px'>+ {extra} more (see previous emails / report)</p>" if extra else ""
    foot = ("<p style='color:#aaa;font-size:11px;margin-top:20px'>Hourly Bangalore job pipeline · "
            "Greenhouse/Lever boards · reply STOP-equivalent by deleting the Actions workflow to stop.</p>")

    html = (f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:760px;margin:auto'>{head}"
            f"<table style='border-collapse:collapse;width:100%'>{''.join(body_rows)}</table>{tail}{foot}</div>")

    lines = [f"{r['company']} ({r['size']}) — {r['title']} [{r['location']}] ({rel_time(r['posted'])})\n  {r['url']}\n"
             for r in shown]
    text = f"NEW BANGALORE JOBS ({len(rows)}):\n\n" + "\n".join(lines)
    return html, text


def send_email(subject, html_body, text_body):
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO") or user

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"BLR Job Alerts <{user}>"
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    log.info("Email sent to %s", to)


# ---------------------------------------------------------------- main
def main():
    global max_email_jobs_global
    cfg = load_config()
    s = cfg["settings"]
    max_email_jobs_global = s["max_email_jobs"]

    seen, existed = load_state(cfg)
    now_iso = datetime.now(timezone.utc).isoformat()

    all_matches, new_jobs, failed = [], [], []
    for company in cfg["companies"]:
        name, ats, slug = company["name"], company["ats"], company["slug"]
        try:
            jobs = FETCHERS[ats](slug)
        except Exception as e:  # dead board / network hiccup — skip gracefully
            failed.append(f"{name} ({slug}): {e}")
            log.warning("Board failed, skipping: %s (%s) — %s", name, slug, e)
            continue

        for job in jobs:
            if not title_allowed(job["title"], s) or not location_allowed(job, s):
                continue
            match = {**job, "company": name, "size": company.get("size", ""), "ats": ats,
                     "snippet": strip_html(job["desc"])}
            all_matches.append(match)
            key = f"{name}:{job['id']}"
            if key not in seen:
                seen[key] = now_iso
                new_jobs.append(match)
        log.info("%-15s %2d jobs on board", name, len(jobs))

    new_jobs.sort(key=lambda r: r["company"])
    log.info("Total open matches: %d | NEW: %d | boards failed: %d", len(all_matches), len(new_jobs), len(failed))

    outbox = os.path.join(ROOT, s.get("outbox_path", "outbox"))
    os.makedirs(outbox, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M")
    with open(os.path.join(outbox, f"report_{stamp}.md"), "w", encoding="utf-8") as f:
        f.write(f"# Run {stamp} IST — {len(all_matches)} open matches, {len(new_jobs)} new\n\n")
        for r in all_matches:
            f.write(f"- **{r['company']}** ({r['size']}) — [{r['title']}]({r['url']}) — {r['location']}\n")
        if failed:
            f.write("\n## Failed boards\n" + "\n".join(f"- {x}" for x in failed) + "\n")

    if DRY_RUN:
        print(f"[DRY RUN] would process {len(new_jobs)} new job(s); state NOT saved; no email sent")
        return 0

    save_state(cfg, seen)

    if not existed:  # first run: seed silently + intro email (avoid blasting hundreds)
        sample = sorted(all_matches, key=lambda r: r["company"])[:15]
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
