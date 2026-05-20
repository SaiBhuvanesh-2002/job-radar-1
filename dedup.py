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
"""


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
