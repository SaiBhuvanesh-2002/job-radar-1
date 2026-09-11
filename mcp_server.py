"""Job Radar MCP Server.

Exposes the Job Radar pipeline as MCP tools that any MCP client can call
(Claude Code, Claude Desktop, OpenClaw, etc.).

Install:
  pip install "mcp[cli]"  # or: pip install mcp --break-system-packages

Run locally:
  python mcp_server.py

Wire into Claude Code (add to ~/.claude/settings.json or project .mcp.json):
  {
    "mcpServers": {
      "job-radar": {
        "command": "python",
        "args": ["/path/to/job-radar/mcp_server.py"],
        "env": {
          "GEMINI_API_KEY": "your-key",
          "GMAIL_USER": "you@gmail.com",
          "GMAIL_APP_PASS": "xxxx xxxx xxxx xxxx"
        }
      }
    }
  }

Available tools:
  search_jobs       — fetch + filter + score jobs right now (dry-run)
  get_digest        — return the current digest as text
  mark_applied      — record that you applied to a job
  update_status     — update an application's status
  get_tracker       — return your 7-day application tracker summary
  check_resume      — run ATS safety check on your resume
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# Add repo root to path so local modules resolve when run as a script.
_REPO = Path(__file__).resolve().parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

log = logging.getLogger("mcp_server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))

# ---------------------------------------------------------------------------
# Lazy imports — only import heavy modules when a tool is actually called
# ---------------------------------------------------------------------------

def _pipeline_imports():
    import alerts
    import filters
    import scorer
    import tailor
    from ats_feed import fetch_all_jobs, normalize_job
    from dedup import (
        filter_unseen, mark_seen, open_db, row_count,
        track_application, update_application_status,
        tracker_summary, recent_applications,
    )
    from job_monitor import is_recent, load_companies, DB_PATH, MAX_AGE_DAYS
    return {
        "alerts": alerts,
        "filters": filters,
        "scorer": scorer,
        "tailor": tailor,
        "fetch_all_jobs": fetch_all_jobs,
        "normalize_job": normalize_job,
        "filter_unseen": filter_unseen,
        "mark_seen": mark_seen,
        "open_db": open_db,
        "row_count": row_count,
        "track_application": track_application,
        "update_application_status": update_application_status,
        "tracker_summary": tracker_summary,
        "recent_applications": recent_applications,
        "is_recent": is_recent,
        "load_companies": load_companies,
        "DB_PATH": DB_PATH,
        "MAX_AGE_DAYS": MAX_AGE_DAYS,
    }


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_search_jobs(max_age_hours: float = 6, limit: int = 20) -> str:
    """Fetch, filter, score, and tailor jobs. Returns JSON list of results."""
    m = _pipeline_imports()
    companies = m["load_companies"]()
    raw_jobs = m["fetch_all_jobs"](companies)
    normalized = [
        nj for raw in raw_jobs
        if (nj := m["normalize_job"](raw)) and nj["id"] and nj["url"]
    ]
    matched = [j for j in normalized if m["filters"].job_passes(j)[0]]
    now = datetime.now(timezone.utc)
    max_age_days = max_age_hours / 24
    recent = [j for j in matched if m["is_recent"](j, now, max_age_days)]
    resume = m["scorer"].load_resume()
    scored = m["scorer"].score_jobs(recent[:limit], resume=resume)
    scored.sort(key=m["scorer"].sort_key)
    tailored = m["tailor"].tailor_jobs(scored, resume=resume)
    # Return serializable subset
    out = []
    for j in tailored:
        out.append({
            "company": j["company"],
            "title": j["title"],
            "location": j["location"],
            "url": j["url"],
            "score": j.get("score", 0),
            "bucket": j.get("bucket", "UNSCORED"),
            "rationale": j.get("rationale", ""),
            "posted_at": j.get("posted_at"),
            "remote": j.get("remote", False),
            "keywords_matched": j.get("keywords_matched", []),
            "keywords_missing": j.get("keywords_missing", []),
            "bullets_tailored": j.get("bullets_tailored", []),
            "why_company": j.get("why_company", ""),
        })
    return json.dumps({"jobs": out, "total": len(out)}, indent=2)


def _tool_get_digest(max_age_hours: float = 6) -> str:
    """Return the current digest as plain text (no email sent)."""
    m = _pipeline_imports()
    companies = m["load_companies"]()
    raw_jobs = m["fetch_all_jobs"](companies)
    normalized = [
        nj for raw in raw_jobs
        if (nj := m["normalize_job"](raw)) and nj["id"] and nj["url"]
    ]
    matched = [j for j in normalized if m["filters"].job_passes(j)[0]]
    now = datetime.now(timezone.utc)
    recent = [j for j in matched if m["is_recent"](j, now, max_age_hours / 24)]
    resume = m["scorer"].load_resume()
    scored = m["scorer"].score_jobs(recent, resume=resume)
    scored.sort(key=m["scorer"].sort_key)
    return m["alerts"].build_digest_text(scored)


def _tool_mark_applied(company: str, title: str, url: str, ats: str = "manual", notes: str = "") -> str:
    """Record that you applied to a job. Returns confirmation."""
    m = _pipeline_imports()
    from ats_feed import Job
    stub_job: Job = {
        "id": url,
        "ats": ats,
        "company": company,
        "title": title,
        "location": "",
        "team": "",
        "description": "",
        "url": url,
        "remote": False,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    with m["open_db"](m["DB_PATH"]) as conn:
        inserted = m["track_application"](conn, stub_job, status="applied", notes=notes)
    verb = "Recorded" if inserted else "Already tracked"
    return f"{verb}: {company} — {title}"


def _tool_update_status(company: str, title: str, url: str, status: str, notes: str = "") -> str:
    """Update status of an application (applied/viewed/replied/interview/offer/rejected)."""
    m = _pipeline_imports()
    from ats_feed import Job
    stub_job: Job = {
        "id": url,
        "ats": "manual",
        "company": company,
        "title": title,
        "location": "",
        "team": "",
        "description": "",
        "url": url,
        "remote": False,
        "posted_at": None,
    }
    with m["open_db"](m["DB_PATH"]) as conn:
        updated = m["update_application_status"](conn, stub_job, status=status, notes=notes)
    if updated:
        return f"Updated {company} — {title} → {status}"
    return f"Not found: {company} — {title}. Use mark_applied first."


def _tool_get_tracker(days: int = 7) -> str:
    """Return your application tracker summary for the last N days."""
    m = _pipeline_imports()
    with m["open_db"](m["DB_PATH"]) as conn:
        summary = m["tracker_summary"](conn, days=days)
        recent = m["recent_applications"](conn, days=days, limit=30)
    lines = [f"=== Last {days} days ==="]
    for status, count in sorted(summary.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {status:12s}: {count}")
    lines.append("\nRecent applications:")
    for app in recent:
        score_str = f" [{app['bucket']} {app['score']}%]" if app.get("score") else ""
        lines.append(f"  [{app['status']:10s}] {app['company']} — {app['title']}{score_str}")
    return "\n".join(lines)


def _tool_check_resume(resume_path: str = "resume.md") -> str:
    """Run ATS safety check on your resume. Returns a human-readable report."""
    # Import from check_resume module
    sys.path.insert(0, str(_REPO))
    import check_resume as cr
    import io
    from contextlib import redirect_stdout
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "ERROR: GEMINI_API_KEY not set."
    path = Path(resume_path)
    if not path.is_absolute():
        path = _REPO / resume_path
    if not path.exists():
        return f"ERROR: Resume not found at {path}"
    text = path.read_text().strip()
    result = cr.check_resume(text, api_key)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cr.print_report(result)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        # mcp 2.x renamed FastMCP → MCPServer
        from mcp.server.mcpserver import MCPServer as _Server
    except ImportError:
        try:
            # mcp 1.x
            from mcp.server.fastmcp import FastMCP as _Server
        except ImportError:
            print(
                "ERROR: mcp package not installed.\n"
                "Run: pip install 'mcp[cli]'\n"
                "Or:  pip install -e '.[mcp]'",
                file=sys.stderr,
            )
            sys.exit(1)

    mcp = _Server("job-radar")

    @mcp.tool()
    def search_jobs(max_age_hours: float = 6, limit: int = 20) -> str:
        """Fetch, filter, score and tailor jobs matching your resume right now.

        Args:
            max_age_hours: Only return jobs posted within this many hours (default 6).
            limit: Maximum number of jobs to score and tailor (default 20).
        """
        return _tool_search_jobs(max_age_hours=max_age_hours, limit=limit)

    @mcp.tool()
    def get_digest(max_age_hours: float = 6) -> str:
        """Return the current job digest as plain text (no email sent).

        Args:
            max_age_hours: Age window for freshness filtering (default 6).
        """
        return _tool_get_digest(max_age_hours=max_age_hours)

    @mcp.tool()
    def mark_applied(company: str, title: str, url: str, ats: str = "manual", notes: str = "") -> str:
        """Record that you manually applied to a job.

        Args:
            company: Company name.
            title: Job title.
            url: Application URL.
            ats: ATS platform (optional, default "manual").
            notes: Any notes to store (optional).
        """
        return _tool_mark_applied(company=company, title=title, url=url, ats=ats, notes=notes)

    @mcp.tool()
    def update_status(company: str, title: str, url: str, status: str, notes: str = "") -> str:
        """Update an application's status in the tracker.

        Args:
            company: Company name.
            title: Job title.
            url: Application URL (used to identify the record).
            status: New status — one of: applied, viewed, replied, interview, offer, rejected.
            notes: Optional notes.
        """
        return _tool_update_status(company=company, title=title, url=url, status=status, notes=notes)

    @mcp.tool()
    def get_tracker(days: int = 7) -> str:
        """Return your application tracker summary.

        Args:
            days: How many days back to look (default 7).
        """
        return _tool_get_tracker(days=days)

    @mcp.tool()
    def check_resume(resume_path: str = "resume.md") -> str:
        """Run an ATS safety check on your resume.

        Args:
            resume_path: Path to the resume file (default: resume.md in the repo root).
        """
        return _tool_check_resume(resume_path=resume_path)

    mcp.run()


if __name__ == "__main__":
    main()
