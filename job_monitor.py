"""Job Radar orchestrator — fetch -> normalize -> dedup -> filter -> email -> persist.

Usage:
  python job_monitor.py              # normal run: send digest of new jobs
  python job_monitor.py --dry-run    # everything except sending email + DB writes
  python job_monitor.py --seed       # mark all current jobs as seen WITHOUT emailing
                                     #   (run once on first deploy so first real run
                                     #    doesn't flood the inbox)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import alerts
import filters
import scorer
from ats_feed import Job, fetch_all_jobs, normalize_job
from dedup import filter_unseen, mark_seen, open_db, row_count

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("job_monitor")

REPO_ROOT = Path(__file__).resolve().parent
COMPANIES_PATH = REPO_ROOT / "companies.json"
DB_PATH = REPO_ROOT / "seen_jobs.db"

# Cache-eviction safety net: even on dedup miss, never email jobs older than this.
MAX_AGE_DAYS = 1


def load_companies() -> list[dict[str, str]]:
    if not COMPANIES_PATH.exists():
        log.error(
            "companies.json not found at %s — run verify_companies.py first "
            "and copy the working rows in.",
            COMPANIES_PATH,
        )
        sys.exit(2)
    return json.loads(COMPANIES_PATH.read_text())


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Accept '...Z' suffix from some ATSes.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_recent(job: Job, now: datetime, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """Whether the job's posted_at is within max_age_days of now.

    Jobs without a posted_at are kept (the dedup hash is the only signal we have).
    """
    posted = _parse_iso(job.get("posted_at"))
    if posted is None:
        return True
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return (now - posted) <= timedelta(days=max_age_days)


def run(dry_run: bool = False, seed: bool = False) -> int:
    load_dotenv()
    companies = load_companies()
    log.info("loaded %d companies from %s", len(companies), COMPANIES_PATH)

    raw_jobs = fetch_all_jobs(companies)
    log.info("fetched %d raw postings", len(raw_jobs))

    normalized: list[Job] = []
    for raw in raw_jobs:
        nj = normalize_job(raw)
        if nj is None:
            continue
        if not nj["id"] or not nj["url"]:
            continue
        normalized.append(nj)
    log.info("normalized %d / %d", len(normalized), len(raw_jobs))

    matched: list[Job] = []
    reject_reasons: dict[str, int] = {}
    for j in normalized:
        passed, reason = filters.job_passes(j)
        if passed:
            matched.append(j)
        else:
            reject_reasons[reason.split(":")[0]] = reject_reasons.get(reason.split(":")[0], 0) + 1
    log.info("filter-matched %d / %d", len(matched), len(normalized))
    if reject_reasons:
        top = sorted(reject_reasons.items(), key=lambda kv: -kv[1])[:5]
        log.info("top reject reasons: %s", top)

    with open_db(DB_PATH) as conn:
        log.info("dedup DB row count (before): %d", row_count(conn))
        if seed:
            inserted = 0 if dry_run else mark_seen(conn, normalized)
            log.info(
                "seed mode: %s %d jobs as seen (skipping email)",
                "would mark" if dry_run else "marked",
                inserted if not dry_run else len(normalized),
            )
            return 0

        new_jobs = filter_unseen(conn, matched)
        log.info("after dedup: %d new jobs", len(new_jobs))

        now = datetime.now(timezone.utc)
        recent = [j for j in new_jobs if is_recent(j, now)]
        dropped = len(new_jobs) - len(recent)
        if dropped:
            log.warning(
                "dropped %d new-but-old jobs (>%dd) — likely cache-miss recovery",
                dropped,
                MAX_AGE_DAYS,
            )

        # v2: score the recent batch against the user's resume.
        # Falls through to UNSCORED if no resume or no API key — pipeline still emails.
        resume = scorer.load_resume()
        scored = scorer.score_jobs(recent, resume=resume)
        scored.sort(key=scorer.sort_key)

        if dry_run:
            log.info("dry-run: would email %d jobs", len(scored))
            for j in scored:
                log.info(
                    "  - [%s %s] %s | %s | %s",
                    j.get("bucket", "UNSCORED"),
                    j.get("score", 0),
                    j["company"], j["title"], j["url"],
                )
            return 0

        if scored:
            alerts.send_digest(scored)

        # Persist ALL new jobs (recent or not) so we don't re-evaluate them later.
        mark_seen(conn, new_jobs)
        log.info("dedup DB row count (after): %d", row_count(conn))

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Job Radar runner")
    parser.add_argument("--dry-run", action="store_true", help="don't send email or write DB")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="mark all current jobs as seen without sending email (first-run safety)",
    )
    args = parser.parse_args(argv)
    try:
        return run(dry_run=args.dry_run, seed=args.seed)
    except Exception as e:
        log.exception("pipeline failed: %s", e)
        # Best-effort failure email — only if creds are loaded and not dry-running.
        if not args.dry_run and os.environ.get("GMAIL_USER"):
            try:
                alerts.send_failure(str(e), os.environ.get("GITHUB_RUN_URL"))
            except Exception:
                log.exception("failure-notification email itself failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
