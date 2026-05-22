"""Schema-drift canary: each ATS's raw fixture must normalize cleanly.

If an ATS changes its response shape, the corresponding parse path in
ats_feed.normalize_job will start failing here long before users notice
missing alerts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the repo root importable so `import ats_feed` works under pytest.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datetime import datetime, timezone  # noqa: E402

from ats_feed import _parse_workday_posted_on, normalize_job  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

REQUIRED_KEYS = {
    "id", "ats", "company", "title", "location", "team",
    "description", "url", "remote", "posted_at",
}


def _load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


def test_lever_normalizes():
    raw = _load("lever_sample.json")[0]
    raw["_ats"] = "lever"
    raw["_company"] = "example"
    job = normalize_job(raw)
    assert job is not None
    assert set(job.keys()) == REQUIRED_KEYS
    assert job["id"] == "abc123-lever-id"
    assert job["ats"] == "lever"
    assert job["title"] == "Senior AI Engineer"
    assert job["team"] == "Applied AI"
    assert job["remote"] is True
    assert job["posted_at"] is not None
    assert job["url"].startswith("https://")


def test_greenhouse_normalizes():
    raw = _load("greenhouse_sample.json")["jobs"][0]
    raw["_ats"] = "greenhouse"
    raw["_company"] = "example"
    job = normalize_job(raw)
    assert job is not None
    assert set(job.keys()) == REQUIRED_KEYS
    assert job["id"] == "4567890"  # coerced to str
    assert job["ats"] == "greenhouse"
    assert job["title"] == "ML Platform Engineer"
    assert job["team"] == "ML Infrastructure"
    assert job["remote"] is True
    assert job["posted_at"] is not None


def test_ashby_normalizes():
    raw = _load("ashby_sample.json")["jobs"][0]
    raw["_ats"] = "ashby"
    raw["_company"] = "example"
    job = normalize_job(raw)
    assert job is not None
    assert set(job.keys()) == REQUIRED_KEYS
    assert job["id"] == "ashby-job-uuid-001"
    assert job["ats"] == "ashby"
    assert job["team"] == "Research Engineering"
    assert job["remote"] is True
    assert job["posted_at"] == "2026-05-12T09:00:00.000+00:00"
    assert job["url"].startswith("https://")


def test_missing_id_returns_none():
    job = normalize_job({"_ats": "lever", "_company": "example", "text": "no id"})
    assert job is None


def test_unknown_ats_returns_none():
    job = normalize_job({"_ats": "taleo", "_company": "example"})
    assert job is None


def test_workday_normalizes():
    raw = {
        "_ats": "workday",
        "_company": "roche",
        "_base_url": "https://roche.wd3.myworkdayjobs.com",
        "_board": "roche-ext",
        "title": "ML Engineer",
        "externalPath": "/job/South-San-Francisco/ML-Engineer_JR-12345",
        "locationsText": "South San Francisco, CA, USA",
        "postedOn": "Posted Today",
    }
    job = normalize_job(raw)
    assert job is not None
    assert job["ats"] == "workday"
    assert job["title"] == "ML Engineer"
    assert job["id"] == raw["externalPath"]
    assert job["url"] == "https://roche.wd3.myworkdayjobs.com/roche-ext/job/South-San-Francisco/ML-Engineer_JR-12345"
    assert job["location"] == "South San Francisco, CA, USA"
    assert job["posted_at"] is not None


def test_workday_posted_on_parser():
    now = datetime.now(timezone.utc)

    today = _parse_workday_posted_on("Posted Today")
    assert today is not None
    assert (now - datetime.fromisoformat(today)).total_seconds() < 5

    yest = datetime.fromisoformat(_parse_workday_posted_on("Posted Yesterday"))
    assert 0.9 < (now - yest).total_seconds() / 86400 < 1.1

    five = datetime.fromisoformat(_parse_workday_posted_on("Posted 5 Days Ago"))
    assert 4.9 < (now - five).total_seconds() / 86400 < 5.1

    # "30+ Days Ago" clamps past the 7d recency window so job_monitor drops it.
    stale = datetime.fromisoformat(_parse_workday_posted_on("Posted 30+ Days Ago"))
    assert (now - stale).total_seconds() / 86400 > 30

    assert _parse_workday_posted_on(None) is None
    assert _parse_workday_posted_on("") is None
    assert _parse_workday_posted_on("Posted soon") is None
