"""HTML digest formatter + Gmail SMTP sender.

Env vars expected:
  GMAIL_USER     — full Gmail address that owns the App Password
  GMAIL_APP_PASS — 16-char App Password (requires 2FA on the account)
  ALERT_TO       — optional override; defaults to GMAIL_USER
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape

from ats_feed import Job

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def _card(job: Job) -> str:
    title = escape(job["title"])
    company = escape(job["company"])
    location = escape(job["location"] or "—")
    team = escape(job["team"] or "")
    url = escape(job["url"])
    posted = escape(job.get("posted_at") or "")
    remote_badge = (
        '<span style="background:#e0f2fe;color:#075985;padding:2px 8px;border-radius:10px;font-size:11px;">REMOTE</span>'
        if job["remote"]
        else ""
    )
    posted_line = (
        f'<div style="color:#6b7280;font-size:11px;margin-top:6px;">Posted: {posted}</div>'
        if posted
        else ""
    )
    team_line = (
        f'<div style="color:#374151;font-size:12px;">Team: {team}</div>' if team else ""
    )
    # Border color is a CSS hook for v2 scoring (HIGH/MED/LOW). Neutral for v1.
    return f"""
    <div style="border-left:4px solid #d1d5db;background:#fff;padding:14px 16px;margin:10px 0;border-radius:6px;box-shadow:0 1px 2px rgba(0,0,0,0.04);">
      <div style="font-size:16px;font-weight:600;margin-bottom:4px;">
        <a href="{url}" style="color:#111827;text-decoration:none;">{title}</a>
      </div>
      <div style="color:#374151;font-size:13px;margin-bottom:6px;">
        <strong>{company}</strong> · {location} {remote_badge}
      </div>
      {team_line}
      {posted_line}
    </div>
    """


def build_digest_html(jobs: list[Job]) -> str:
    cards = "".join(_card(j) for j in sorted(jobs, key=lambda j: (j["company"], j["title"])))
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f9fafb;padding:16px;color:#111827;">
  <h2 style="margin:0 0 12px 0;">Job Radar — {len(jobs)} new role(s)</h2>
  <div style="color:#6b7280;font-size:12px;margin-bottom:16px;">
    Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
  </div>
  {cards}
</body></html>"""


def build_digest_text(jobs: list[Job]) -> str:
    lines = [f"Job Radar — {len(jobs)} new role(s)", ""]
    for j in sorted(jobs, key=lambda j: (j["company"], j["title"])):
        flag = " [REMOTE]" if j["remote"] else ""
        lines.append(f"- {j['company']}: {j['title']} ({j['location'] or '—'}){flag}")
        lines.append(f"  {j['url']}")
        lines.append("")
    return "\n".join(lines)


def _smtp_send(subject: str, html_body: str, text_body: str) -> None:
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASS", "").replace(" ", "")
    if not user or not pw:
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASS must be set")
    to = os.environ.get("ALERT_TO", user)

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(user, pw)
        smtp.send_message(msg)
    log.info("sent email '%s' to %s", subject, to)


def send_digest(jobs: list[Job]) -> None:
    if not jobs:
        log.info("no new jobs; skipping digest email")
        return
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"Job Radar — {len(jobs)} new role(s) — {when}"
    _smtp_send(subject, build_digest_html(jobs), build_digest_text(jobs))


def send_failure(error: str, run_url: str | None = None) -> None:
    """Best-effort one-liner so silent breakage gets noticed."""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"Job Radar — pipeline FAILED — {when}"
    where = run_url or "(no run URL)"
    text = f"Job Radar pipeline failed at {when}\n\nError: {error}\nRun: {where}\n"
    html = (
        f"<html><body><h3>Job Radar — pipeline failed</h3>"
        f"<p><b>When:</b> {escape(when)}</p>"
        f"<p><b>Error:</b> <code>{escape(error)}</code></p>"
        f"<p><b>Run:</b> <a href='{escape(where)}'>{escape(where)}</a></p>"
        f"</body></html>"
    )
    try:
        _smtp_send(subject, html, text)
    except Exception as e:
        log.error("failure email also failed: %s", e)
