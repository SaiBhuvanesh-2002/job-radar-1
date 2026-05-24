"""Gemini-based resume-vs-job scorer (Job Radar v2).

Calls Google AI Studio (https://aistudio.google.com) via API key — NOT Vertex AI.
Resume is loaded from RESUME_CONTENT env var (CI) or `resume.md` (local). If
neither is available, scoring is skipped and the pipeline emails unscored jobs
so V1 behavior is preserved as a graceful fallback.

Each scored job carries:
  score      — int 1-10
  bucket     — "HIGH" (8-10), "MED" (5-7), "LOW" (1-4), or "UNSCORED"
  rationale  — one-sentence explanation citing concrete resume signals

Failures (missing key, API error, malformed JSON) downgrade jobs to UNSCORED
rather than blocking the digest — the email must always go out.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, NotRequired

from ats_feed import Job

log = logging.getLogger(__name__)

RESUME_FILENAME = "resume.md"
DEFAULT_MODEL = "gemini-2.5-flash"
BATCH_SIZE = 5
# Description trim — keep prompts well under the input window without
# starving the model of stack/seniority signals. 1200 chars ~= 300 tokens.
MAX_DESC_CHARS = 1200

# Empirically Gemini Flash 503s ~10% of the time during peak hours. The SDK
# does fast internal retries but they don't outlast a sustained spike, so we
# add a slower outer retry on transient 5xx/429.
MAX_BATCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (4, 12)  # waits before attempts 2 and 3
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class ScoredJob(Job):
    score: NotRequired[int]
    bucket: NotRequired[str]
    rationale: NotRequired[str]


def load_resume(repo_root: Path | None = None) -> str | None:
    """Resume content from RESUME_CONTENT env var (CI) or resume.md (local).

    Returns None when neither is available, which tells the scorer to skip
    LLM work and the pipeline to fall through to unscored email.
    """
    env_text = os.environ.get("RESUME_CONTENT", "").strip()
    if env_text:
        log.info("scorer: loaded resume from RESUME_CONTENT env (%d chars)", len(env_text))
        return env_text
    root = repo_root or Path(__file__).resolve().parent
    p = root / RESUME_FILENAME
    if p.exists():
        text = p.read_text().strip()
        if text:
            log.info("scorer: loaded resume from %s (%d chars)", p, len(text))
            return text
    log.info("scorer: no resume found (RESUME_CONTENT env or %s)", p)
    return None


_PROMPT_TEMPLATE = """\
You are ranking job postings against a candidate's resume. For EACH job, return:
- score: integer 1-10 (10 = perfect fit, 1 = clearly wrong role for this candidate)
- rationale: ONE sentence, <=140 chars, citing concrete reasons (e.g. "Strong: 3+ yrs LLM infra + Python match" or "Mismatch: requires 7+ yrs and TS frontend")

Score guide:
- 8-10 (HIGH): role title, seniority, AND tech stack all align with the resume
- 5-7  (MED):  role/seniority match but stack is partial, OR strong stack but adjacent role
- 1-4  (LOW):  major mismatch on seniority, stack, or role family

Cite real resume experience in rationales, not generic phrases. The candidate is mid-senior (3-5 yrs) targeting AI/ML IC roles in the US — do NOT penalize US-based or Remote roles.

Return STRICT JSON: a list with one object per job, in the same order, with fields {{"index", "score", "rationale"}}. No prose, no markdown fences.

=== RESUME ===
{resume}
=== END RESUME ===

=== JOBS ===
{jobs_block}
=== END JOBS ===
"""


def _format_jobs_block(jobs: list[Job]) -> str:
    lines = []
    for i, j in enumerate(jobs):
        desc = (j.get("description") or "")[:MAX_DESC_CHARS]
        lines.append(
            f"[{i}] company={j['company']} | title={j['title']} | "
            f"location={j['location']} | team={j.get('team') or ''}\n"
            f"description: {desc}\n"
        )
    return "\n".join(lines)


def _bucket_for(score: int) -> str:
    if score >= 8:
        return "HIGH"
    if score >= 5:
        return "MED"
    return "LOW"


def _unscored(j: Job) -> ScoredJob:
    return {**j, "score": 0, "bucket": "UNSCORED", "rationale": ""}


def _call_gemini(prompt: str, model_name: str, api_key: str) -> str:
    """One Gemini call, returns raw response text. Raises on API error."""
    # Import lazily so test environments without google-genai installed still
    # exercise the fallback paths via monkeypatch.
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return resp.text or ""


def _is_transient(exc: BaseException) -> bool:
    """True for 5xx/429 errors worth retrying."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in TRANSIENT_STATUS_CODES:
        return True
    msg = str(exc)
    return any(str(c) in msg for c in TRANSIENT_STATUS_CODES) and (
        "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg or "INTERNAL" in msg
    )


def _score_batch(
    jobs: list[Job],
    resume: str,
    model: str,
    api_key: str,
    call_fn=_call_gemini,
) -> list[dict[str, Any]]:
    prompt = _PROMPT_TEMPLATE.format(resume=resume, jobs_block=_format_jobs_block(jobs))
    last_exc: BaseException | None = None
    for attempt in range(MAX_BATCH_ATTEMPTS):
        try:
            raw = call_fn(prompt, model, api_key)
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError(f"expected JSON list, got {type(data).__name__}")
            return data
        except Exception as e:
            last_exc = e
            if attempt < MAX_BATCH_ATTEMPTS - 1 and _is_transient(e):
                wait = RETRY_BACKOFF_SECONDS[attempt]
                log.warning(
                    "scorer: transient error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, MAX_BATCH_ATTEMPTS, wait, str(e)[:140],
                )
                time.sleep(wait)
                continue
            raise
    # Unreachable in practice — loop either returns or raises — but keeps mypy happy.
    assert last_exc is not None
    raise last_exc


def score_jobs(
    jobs: list[Job],
    resume: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    *,
    call_fn=_call_gemini,
) -> list[ScoredJob]:
    """Score jobs with Gemini. UNSCORED fallback on any failure.

    `call_fn` is injectable so tests can swap the network call.
    """
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    if not jobs:
        return []
    if not resume:
        log.info("scorer: no resume — passing through %d jobs as UNSCORED", len(jobs))
        return [_unscored(j) for j in jobs]
    if not api_key:
        log.warning("scorer: GEMINI_API_KEY not set — passing through %d UNSCORED", len(jobs))
        return [_unscored(j) for j in jobs]

    scored: list[ScoredJob] = []
    bucket_counts = {"HIGH": 0, "MED": 0, "LOW": 0, "UNSCORED": 0}

    for start in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[start : start + BATCH_SIZE]
        try:
            results = _score_batch(batch, resume, model, api_key, call_fn=call_fn)
        except Exception as e:
            log.exception(
                "scorer: batch failed (offset %d, n=%d): %s — UNSCORED fallback",
                start, len(batch), e,
            )
            for j in batch:
                sj = _unscored(j)
                scored.append(sj)
                bucket_counts["UNSCORED"] += 1
            continue

        by_idx: dict[int, dict[str, Any]] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int):
                by_idx[idx] = item

        for i, j in enumerate(batch):
            r = by_idx.get(i)
            score: int | None = None
            if r is not None:
                raw_score = r.get("score")
                try:
                    score = int(raw_score) if raw_score is not None else None
                except (TypeError, ValueError):
                    score = None
            if score is None:
                scored.append(_unscored(j))
                bucket_counts["UNSCORED"] += 1
                continue
            score = max(1, min(10, score))
            bucket = _bucket_for(score)
            sj: ScoredJob = {
                **j,
                "score": score,
                "bucket": bucket,
                "rationale": str(r.get("rationale", ""))[:300] if r else "",
            }
            scored.append(sj)
            bucket_counts[bucket] += 1

    log.info(
        "scorer: scored %d jobs — HIGH=%d MED=%d LOW=%d UNSCORED=%d",
        len(scored),
        bucket_counts["HIGH"], bucket_counts["MED"],
        bucket_counts["LOW"], bucket_counts["UNSCORED"],
    )
    return scored


def sort_key(j: ScoredJob) -> tuple[int, int, str, str]:
    """Sort: scored jobs by score desc, then alpha by company/title.

    UNSCORED jobs (score=0) bubble to the bottom.
    """
    s = j.get("score") or 0
    # Negative score for desc sort. Bucket=UNSCORED → s=0 → sorts last.
    return (-s, 0, j.get("company") or "", j.get("title") or "")
