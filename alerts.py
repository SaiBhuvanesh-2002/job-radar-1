"""HTML digest formatter + Gmail SMTP sender.

Env vars expected:
  GMAIL_USER     — full Gmail address that owns the App Password
  GMAIL_APP_PASS — 16-char App Password (requires 2FA on the account)
  ALERT_TO       — optional override; defaults to GMAIL_USER

Layout notes (Gmail-safe HTML):
- Table-based cards. Flexbox is unreliable in Gmail web/iOS; tables aren't.
- All <a> tags get target="_blank" rel="noopener noreferrer" so clicks open a
  new browser tab instead of swallowing the Gmail window.
- No external CSS, no <style> blocks — Gmail strips them. Inline only.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from zoneinfo import ZoneInfo

from ats_feed import Job

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# IANA zone handles EST/EDT switchover automatically. The strftime tag will
# read "EST" or "EDT" depending on the date.
ET_ZONE = ZoneInfo("America/New_York")


def _format_now() -> str:
    """Human-readable timestamp: 'May 23, 2026 · 2:30 PM ET (18:30 UTC)'."""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET_ZONE)
    # %-I strips the leading zero on the hour; portable on macOS + Linux.
    return (
        f"{now_et.strftime('%b %d, %Y')} · "
        f"{now_et.strftime('%-I:%M %p')} ET "
        f"({now_utc.strftime('%H:%M')} UTC)"
    )


def _format_now_subject() -> str:
    """Compact for the email subject line: '2026-05-23 2:30 PM ET'."""
    now_et = datetime.now(timezone.utc).astimezone(ET_ZONE)
    return now_et.strftime("%Y-%m-%d %-I:%M %p ET")

# Right-panel theme per bucket. Dark backgrounds borrowed from the reference
# screenshot — they make the match label pop against the white card body.
_BUCKET_THEME = {
    "HIGH":     {"label": "STRONG MATCH", "panel_bg": "#064e3b", "panel_fg": "#a7f3d0", "accent": "#10b981"},
    "MED":      {"label": "DECENT MATCH", "panel_bg": "#78350f", "panel_fg": "#fde68a", "accent": "#f59e0b"},
    "LOW":      {"label": "WEAK MATCH",   "panel_bg": "#374151", "panel_fg": "#d1d5db", "accent": "#9ca3af"},
    "UNSCORED": {"label": "NO SCORE",     "panel_bg": "#1f2937", "panel_fg": "#9ca3af", "accent": "#6b7280"},
}


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _relative_time(iso: str | None, now: datetime | None = None) -> str:
    """Render an ISO timestamp as 'X hours ago' / 'Yesterday' / 'Mar 15'."""
    dt = _parse_iso(iso)
    if dt is None:
        return ""
    n = now or datetime.now(timezone.utc)
    delta = n - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = secs // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if secs < 172800:
        return "yesterday"
    days = secs // 86400
    if days < 7:
        return f"{days} days ago"
    return dt.astimezone(ET_ZONE).strftime("%b %d")


def _posted_et(iso: str | None) -> str:
    """Absolute posted time in ET — e.g. 'May 13, 4:25 PM ET'."""
    dt = _parse_iso(iso)
    if dt is None:
        return ""
    return dt.astimezone(ET_ZONE).strftime("%b %d, %-I:%M %p ET")


def _chip(text: str, bg: str = "#dcfce7", fg: str = "#166534") -> str:
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:500;'
        f'line-height:1.4;">{escape(text)}</span>'
    )


def _meta_item(label: str, value: str) -> str:
    """One cell of the meta row — label on top, value below. Subtle, readable."""
    return (
        '<td style="padding:0 16px 0 0;vertical-align:top;font-family:-apple-system,Segoe UI,Roboto,sans-serif;">'
        f'<div style="color:#9ca3af;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;margin-bottom:2px;">{escape(label)}</div>'
        f'<div style="color:#111827;font-size:13px;font-weight:500;">{escape(value)}</div>'
        "</td>"
    )


def _score_panel(bucket: str, score: int, rationale: str) -> str:
    theme = _BUCKET_THEME.get(bucket, _BUCKET_THEME["UNSCORED"])
    if bucket == "UNSCORED":
        score_display = "—"
    else:
        score_display = f'{score}<span style="font-size:18px;color:{theme["panel_fg"]};">/10</span>'
    rationale_block = ""
    if rationale:
        rationale_block = (
            f'<div style="margin-top:14px;padding-top:14px;border-top:1px solid rgba(255,255,255,0.08);'
            f'color:{theme["panel_fg"]};font-size:11px;line-height:1.5;text-align:left;">'
            f'<span style="color:{theme["accent"]};font-weight:700;">&#10003;</span> {escape(rationale)}'
            "</div>"
        )
    return (
        '<td width="200" valign="middle" align="center" '
        f'style="width:200px;background:{theme["panel_bg"]};border-radius:0 10px 10px 0;'
        'padding:18px 16px;font-family:-apple-system,Segoe UI,Roboto,sans-serif;">'
        f'<div style="color:#ffffff;font-size:34px;font-weight:700;line-height:1;">{score_display}</div>'
        f'<div style="margin-top:8px;color:{theme["accent"]};font-size:11px;font-weight:700;'
        f'letter-spacing:1px;">{theme["label"]}</div>'
        f'{rationale_block}'
        "</td>"
    )


def _card(job: Job) -> str:
    title = escape(job["title"])
    company = escape(job["company"])
    location = job["location"] or "—"
    team = job["team"] or ""
    url = escape(job["url"])
    posted_rel = _relative_time(job.get("posted_at"))

    bucket = (job.get("bucket") or "UNSCORED").upper()
    score = job.get("score") or 0
    rationale = job.get("rationale") or ""

    # Top chip row: relative posted time + remote/onsite badge.
    top_chips: list[str] = []
    if posted_rel:
        top_chips.append(_chip(posted_rel, bg="#ecfdf5", fg="#047857"))
    if job["remote"]:
        top_chips.append(_chip("REMOTE", bg="#e0f2fe", fg="#075985"))
    top_chips_html = (
        f'<div style="margin-bottom:10px;">{"&nbsp;".join(top_chips)}</div>'
        if top_chips else ""
    )

    # Meta row — label/value pairs in a tight table. Skip cells without data.
    meta_cells: list[str] = [_meta_item("Location", location)]
    if team:
        meta_cells.append(_meta_item("Team", team))
    posted_et = _posted_et(job.get("posted_at"))
    if posted_et:
        meta_cells.append(_meta_item("Posted", posted_et))
    meta_cells.append(_meta_item("Source", (job.get("ats") or "").title() or "—"))
    meta_row = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-top:14px;border-collapse:collapse;">'
        f'<tr>{"".join(meta_cells)}</tr></table>'
    )

    left_cell = (
        '<td valign="top" style="padding:20px 22px;font-family:-apple-system,Segoe UI,Roboto,sans-serif;">'
        f'{top_chips_html}'
        f'<div style="font-size:20px;font-weight:700;line-height:1.25;margin-bottom:4px;">'
        f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
        f'style="color:#111827;text-decoration:none;">{title}</a>'
        f'</div>'
        f'<div style="color:#6b7280;font-size:13px;">'
        f'<span style="color:#374151;font-weight:600;">{company}</span>'
        f'</div>'
        f'{meta_row}'
        '<div style="margin-top:16px;">'
        f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
        'style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;'
        'padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;">'
        'View posting &rarr;</a>'
        '</div>'
        '</td>'
    )

    right_cell = _score_panel(bucket, score, rationale)

    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin:12px 0;background:#ffffff;border-radius:10px;border:1px solid #e5e7eb;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.04);border-collapse:separate;">'
        f'<tr>{left_cell}{right_cell}</tr>'
        '</table>'
    )


def _bucket_summary(jobs: list[Job]) -> str:
    counts: dict[str, int] = {}
    for j in jobs:
        b = (j.get("bucket") or "UNSCORED").upper()
        counts[b] = counts.get(b, 0) + 1
    parts = []
    for b in ("HIGH", "MED", "LOW", "UNSCORED"):
        if counts.get(b):
            parts.append(f"{b} {counts[b]}")
    return " · ".join(parts)


def build_digest_html(jobs: list[Job]) -> str:
    # Caller is expected to have already sorted with scorer.sort_key.
    cards = "".join(_card(j) for j in jobs)
    summary = _bucket_summary(jobs)
    summary_line = (
        f'<div style="color:#6b7280;font-size:13px;margin-bottom:18px;">{escape(summary)}</div>'
        if summary else ""
    )
    return f"""<!doctype html>
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111827;">
  <div style="max-width:720px;margin:0 auto;padding:24px 16px;">
    <h1 style="margin:0 0 4px 0;font-size:22px;font-weight:700;">Job Radar</h1>
    <div style="color:#374151;font-size:14px;margin-bottom:2px;">{len(jobs)} new role(s)</div>
    {summary_line}
    <div style="color:#9ca3af;font-size:11px;margin-bottom:18px;">
      Generated {_format_now()}
    </div>
    {cards}
  </div>
</body>
</html>"""


def build_digest_text(jobs: list[Job]) -> str:
    summary = _bucket_summary(jobs)
    header = f"Job Radar — {len(jobs)} new role(s)"
    if summary:
        header += f" ({summary})"
    lines = [header, ""]
    for j in jobs:
        bucket = (j.get("bucket") or "UNSCORED").upper()
        score = j.get("score") or 0
        tag = f"[{bucket} {score}/10]" if bucket != "UNSCORED" else "[UNSCORED]"
        flag = " [REMOTE]" if j["remote"] else ""
        lines.append(f"- {tag} {j['company']}: {j['title']} ({j['location'] or '—'}){flag}")
        rationale = j.get("rationale") or ""
        if rationale:
            lines.append(f"    why: {rationale}")
        lines.append(f"    {j['url']}")
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
    when = _format_now_subject()
    summary = _bucket_summary(jobs)
    subject_tail = f" — {summary}" if summary else ""
    subject = f"Job Radar — {len(jobs)} new role(s){subject_tail} — {when}"
    _smtp_send(subject, build_digest_html(jobs), build_digest_text(jobs))


def send_failure(error: str, run_url: str | None = None) -> None:
    """Best-effort one-liner so silent breakage gets noticed."""
    when = _format_now()
    subject = f"Job Radar — pipeline FAILED — {_format_now_subject()}"
    where = run_url or "(no run URL)"
    text = f"Job Radar pipeline failed at {when}\n\nError: {error}\nRun: {where}\n"
    html = (
        f"<html><body><h3>Job Radar — pipeline failed</h3>"
        f"<p><b>When:</b> {escape(when)}</p>"
        f"<p><b>Error:</b> <code>{escape(error)}</code></p>"
        f"<p><b>Run:</b> <a href='{escape(where)}' target='_blank' rel='noopener noreferrer'>{escape(where)}</a></p>"
        f"</body></html>"
    )
    try:
        _smtp_send(subject, html, text)
    except Exception as e:
        log.error("failure email also failed: %s", e)
