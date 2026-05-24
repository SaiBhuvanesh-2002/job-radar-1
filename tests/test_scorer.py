"""Scorer tests — no live Gemini call, the client is swapped via call_fn."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scorer  # noqa: E402


def _job(id_: str = "1", title: str = "ML Engineer") -> dict:
    return {
        "id": id_, "ats": "test", "company": "acme", "title": title,
        "location": "Remote", "team": "", "description": "Build ML stuff",
        "url": f"https://x/{id_}", "remote": True, "posted_at": None,
    }


def test_bucket_thresholds():
    assert scorer._bucket_for(10) == "HIGH"
    assert scorer._bucket_for(8) == "HIGH"
    assert scorer._bucket_for(7) == "MED"
    assert scorer._bucket_for(5) == "MED"
    assert scorer._bucket_for(4) == "LOW"
    assert scorer._bucket_for(1) == "LOW"


def test_empty_jobs_returns_empty():
    assert scorer.score_jobs([]) == []


def test_no_resume_returns_unscored():
    jobs = [_job("1"), _job("2")]
    out = scorer.score_jobs(jobs, resume=None, api_key="fake")
    assert len(out) == 2
    assert all(j["bucket"] == "UNSCORED" and j["score"] == 0 for j in out)


def test_no_api_key_returns_unscored():
    jobs = [_job("1")]
    out = scorer.score_jobs(jobs, resume="resume text", api_key="")
    assert out[0]["bucket"] == "UNSCORED"


def test_full_scoring_path():
    jobs = [_job("1", "ML Engineer"), _job("2", "Data Scientist")]

    def fake_call(prompt, model, api_key):
        assert "resume text" in prompt
        assert "ML Engineer" in prompt
        assert "Data Scientist" in prompt
        return json.dumps([
            {"index": 0, "score": 9, "rationale": "Strong PyTorch + RAG match"},
            {"index": 1, "score": 4, "rationale": "Mismatch: heavy stats focus"},
        ])

    out = scorer.score_jobs(jobs, resume="resume text", api_key="fake", call_fn=fake_call)
    assert len(out) == 2
    assert out[0]["score"] == 9 and out[0]["bucket"] == "HIGH"
    assert "PyTorch" in out[0]["rationale"]
    assert out[1]["score"] == 4 and out[1]["bucket"] == "LOW"


def test_malformed_response_falls_back_unscored():
    jobs = [_job("1")]

    def fake_call(prompt, model, api_key):
        return "not json at all"

    out = scorer.score_jobs(jobs, resume="r", api_key="k", call_fn=fake_call)
    assert out[0]["bucket"] == "UNSCORED"


def test_api_exception_falls_back_unscored():
    jobs = [_job("1"), _job("2")]

    def fake_call(prompt, model, api_key):
        raise RuntimeError("simulated rate limit")

    out = scorer.score_jobs(jobs, resume="r", api_key="k", call_fn=fake_call)
    assert all(j["bucket"] == "UNSCORED" for j in out)


def test_transient_503_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(scorer, "RETRY_BACKOFF_SECONDS", (0, 0))  # no sleep in tests
    jobs = [_job("1")]
    attempts = {"n": 0}

    def flaky_call(prompt, model, api_key):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("503 UNAVAILABLE high demand")
        return json.dumps([{"index": 0, "score": 8, "rationale": "great fit"}])

    out = scorer.score_jobs(jobs, resume="r", api_key="k", call_fn=flaky_call)
    assert attempts["n"] == 2  # retried once
    assert out[0]["bucket"] == "HIGH" and out[0]["score"] == 8


def test_permanent_error_not_retried():
    jobs = [_job("1")]
    attempts = {"n": 0}

    def fake_call(prompt, model, api_key):
        attempts["n"] += 1
        raise RuntimeError("400 INVALID_ARGUMENT bad prompt")

    out = scorer.score_jobs(jobs, resume="r", api_key="k", call_fn=fake_call)
    assert attempts["n"] == 1  # 400 is not transient — no retry
    assert out[0]["bucket"] == "UNSCORED"


def test_missing_index_in_response_unscored_for_that_job():
    jobs = [_job("1"), _job("2")]

    def fake_call(prompt, model, api_key):
        # Only return job 1's score, drop job 0
        return json.dumps([{"index": 1, "score": 7, "rationale": "ok fit"}])

    out = scorer.score_jobs(jobs, resume="r", api_key="k", call_fn=fake_call)
    assert out[0]["bucket"] == "UNSCORED"
    assert out[1]["score"] == 7 and out[1]["bucket"] == "MED"


def test_score_clamped_to_range():
    jobs = [_job("1"), _job("2")]

    def fake_call(prompt, model, api_key):
        return json.dumps([
            {"index": 0, "score": 99, "rationale": "x"},
            {"index": 1, "score": -5, "rationale": "y"},
        ])

    out = scorer.score_jobs(jobs, resume="r", api_key="k", call_fn=fake_call)
    assert out[0]["score"] == 10
    assert out[1]["score"] == 1


def test_batching_chunks_correctly():
    # 12 jobs at BATCH_SIZE=5 → 3 batches (5, 5, 2)
    jobs = [_job(str(i)) for i in range(12)]
    call_count = {"n": 0}

    def fake_call(prompt, model, api_key):
        call_count["n"] += 1
        # Count [N] tags in prompt — one per job
        count = prompt.count("[0] company") + prompt.count("[1] company") + \
                prompt.count("[2] company") + prompt.count("[3] company") + \
                prompt.count("[4] company")
        return json.dumps([
            {"index": i, "score": 5, "rationale": "med"} for i in range(count)
        ])

    out = scorer.score_jobs(jobs, resume="r", api_key="k", call_fn=fake_call)
    assert len(out) == 12
    assert call_count["n"] == 3  # 5 + 5 + 2
    assert all(j["score"] == 5 for j in out)


def test_sort_key_orders_high_first_unscored_last():
    jobs = [
        {**_job("a"), "score": 0, "bucket": "UNSCORED", "rationale": ""},
        {**_job("b"), "score": 9, "bucket": "HIGH", "rationale": ""},
        {**_job("c"), "score": 6, "bucket": "MED", "rationale": ""},
        {**_job("d"), "score": 2, "bucket": "LOW", "rationale": ""},
    ]
    jobs.sort(key=scorer.sort_key)
    assert [j["id"] for j in jobs] == ["b", "c", "d", "a"]


def test_load_resume_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_CONTENT", "from env")
    # Even if resume.md exists, env should win
    (tmp_path / "resume.md").write_text("from file")
    assert scorer.load_resume(repo_root=tmp_path) == "from env"


def test_load_resume_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("RESUME_CONTENT", raising=False)
    (tmp_path / "resume.md").write_text("from file")
    assert scorer.load_resume(repo_root=tmp_path) == "from file"


def test_load_resume_returns_none_when_neither(monkeypatch, tmp_path):
    monkeypatch.delenv("RESUME_CONTENT", raising=False)
    assert scorer.load_resume(repo_root=tmp_path) is None
