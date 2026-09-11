"""SQLite-backed dedup store for job postings.

Hash key = sha256("{ats}:{ats_job_id}") — stable across URL changes and
recruiter title edits. Rows also carry posted_at so the orchestrator can
apply a "recent only" filter as a cache-eviction safety net.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ats_feed import Job

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("seen_jobs.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
  hash          TEXT PRIMARY KEY,
  ats           TEXT NOT NULL,
  company       TEXT NOT NULL,
  title         TEXT NOT NULL,
  url           TEXT NOT NULL,
  posted_at     TEXT,
  first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_first_seen ON seen_jobs(first_seen_at);

CREATE TABLE IF NOT EXISTS applied_jobs (
  hash        TEXT PRIMARY KEY,
  ats         TEXT NOT NULL,
  company     TEXT NOT NULL,
  title       TEXT NOT NULL,
  url         TEXT NOT NULL,
  score       INTEGER,
  bucket      TEXT,
  applied_at  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'applied',
  notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_applied_at ON applied_jobs(applied_at);
"""

# Valid application statuses (mirrors Tsenta's tracker states)
TRACKER_STATUSES = frozenset({"applied", "viewed", "replied", "interview", "offer", "rejected", "skipped"})


def job_hash(job: Job) -> str:
    key = f"{job['ats']}:{job['id']}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@contextmanager
def open_db(path: Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def filter_unseen(conn: sqlite3.Connection, jobs: Iterable[Job]) -> list[Job]:
    """Return jobs whose hash is NOT yet in the DB."""
    jobs_list = list(jobs)
    if not jobs_list:
        return []
    hashes = [job_hash(j) for j in jobs_list]
    placeholders = ",".join("?" * len(hashes))
    cur = conn.execute(
        f"SELECT hash FROM seen_jobs WHERE hash IN ({placeholders})", hashes
    )
    seen = {row[0] for row in cur.fetchall()}
    return [j for j, h in zip(jobs_list, hashes) if h not in seen]


def mark_seen(conn: sqlite3.Connection, jobs: Iterable[Job]) -> int:
    """Insert each job's hash. Returns count actually inserted (post-dedup)."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            job_hash(j),
            j["ats"],
            j["company"],
            j["title"],
            j["url"],
            j.get("posted_at"),
            now,
        )
        for j in jobs
    ]
    if not rows:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO seen_jobs "
        "(hash, ats, company, title, url, posted_at, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    inserted = cur.rowcount if cur.rowcount != -1 else len(rows)
    log.info("marked %d/%d jobs as seen", inserted, len(rows))
    return inserted


def row_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]


# ---------------------------------------------------------------------------
# Application tracker
# ---------------------------------------------------------------------------

def track_application(
    conn: sqlite3.Connection,
    job: Job,
    status: str = "applied",
    notes: str = "",
) -> bool:
    """Record that the user applied to a job.

    Returns True if inserted (new), False if hash already existed.
    Status can be any value from TRACKER_STATUSES.
    """
    now = datetime.now(timezone.utc).isoformat()
    h = job_hash(job)
    try:
        conn.execute(
            "INSERT INTO applied_jobs "
            "(hash, ats, company, title, url, score, bucket, applied_at, status, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                h,
                job["ats"],
                job["company"],
                job["title"],
                job["url"],
                job.get("score"),  # type: ignore[union-attr]
                job.get("bucket"),  # type: ignore[union-attr]
                now,
                status,
                notes or "",
            ),
        )
        log.info("tracker: recorded '%s' for %s/%s", status, job["company"], job["title"])
        return True
    except sqlite3.IntegrityError:
        log.debug("tracker: %s/%s already tracked", job["company"], job["title"])
        return False


def update_application_status(
    conn: sqlite3.Connection,
    job: Job,
    status: str,
    notes: str = "",
) -> bool:
    """Update status of an already-tracked application. Returns True if found."""
    h = job_hash(job)
    cur = conn.execute(
        "UPDATE applied_jobs SET status=?, notes=? WHERE hash=?",
        (status, notes, h),
    )
    return cur.rowcount > 0


def tracker_summary(
    conn: sqlite3.Connection,
    days: int = 7,
) -> dict[str, int]:
    """Return counts per status for applications tracked in the last `days` days."""
    cutoff = (
        datetime.now(timezone.utc).replace(microsecond=0)
        - __import__("datetime").timedelta(days=days)
    ).isoformat()
    cur = conn.execute(
        "SELECT status, COUNT(*) FROM applied_jobs WHERE applied_at >= ? GROUP BY status",
        (cutoff,),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def recent_applications(
    conn: sqlite3.Connection,
    days: int = 7,
    limit: int = 50,
) -> list[dict]:
    """Return recent applied jobs as dicts, newest first."""
    cutoff = (
        datetime.now(timezone.utc).replace(microsecond=0)
        - __import__("datetime").timedelta(days=days)
    ).isoformat()
    cur = conn.execute(
        "SELECT company, title, url, score, bucket, applied_at, status, notes "
        "FROM applied_jobs WHERE applied_at >= ? ORDER BY applied_at DESC LIMIT ?",
        (cutoff, limit),
    )
    cols = ["company", "title", "url", "score", "bucket", "applied_at", "status", "notes"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
