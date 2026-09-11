"""Per-job resume tailoring — Job Radar.

For HIGH and MED scored jobs, calls Gemini to produce:
  1. A short keyword-alignment note (keywords the JD emphasises that are / are not in resume).
  2. Up to 3 rewritten resume bullet points tailored to the JD.
  3. A draft answer to the "Why this company?" open-ended question.

The output is woven into the email digest card for each job so the user can
copy-paste when they apply — no auto-submission, just prep.

Failures are silent: a TailoredJob without tailoring fields is still emailed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, NotRequired

from scorer import ScoredJob, _call_gemini, load_resume

log = logging.getLogger(__name__)

# Only tailor HIGH and MED — skip LOW and UNSCORED to save quota.
TAILOR_BUCKETS = {"HIGH", "MED"}
# Limit per run to avoid blowing through API quota on a cache-miss day.
MAX_TAILOR_PER_RUN = 20
DEFAULT_MODEL = "gemini-2.5-flash"
MAX_DESC_CHARS = 2000


class TailoredJob(ScoredJob):
    keywords_matched: NotRequired[list[str]]    # JD keywords found in resume
    keywords_missing: NotRequired[list[str]]    # JD keywords NOT in resume
    bullets_tailored: NotRequired[list[str]]    # rewritten resume bullets for this JD
    why_company: NotRequired[str]               # draft open-ended answer


_TAILOR_PROMPT = """\
You are helping a job applicant tailor their materials for a specific role. Study the JD and the resume below, then return STRICT JSON with these fields:

- keywords_matched: list of up to 6 skill/tech keywords that appear in BOTH the JD and the resume (exact or close match)
- keywords_missing: list of up to 4 important JD keywords NOT clearly in the resume
- bullets_tailored: list of 2-3 rewritten resume bullet points. Take REAL bullets from the resume and rephrase them to echo JD language — keep the substance true, just sharpen the wording and add JD keywords where honest. Start each with an action verb.
- why_company: one paragraph (3-5 sentences) that could answer "Why are you interested in this role/company?" — draw on the JD's mission/stack, not generic praise. Sound like a real person, not a bot.

Return ONLY valid JSON — no markdown fences, no prose.

=== RESUME ===
{resume}
=== END RESUME ===

=== JOB: {title} at {company} ===
{description}
=== END JOB ===
"""


def _tailor_job(
    job: ScoredJob,
    resume: str,
    api_key: str,
    model: str,
    call_fn=_call_gemini,
) -> TailoredJob:
    """Tailor a single job. Returns the job with tailoring fields added."""
    desc = (job.get("description") or "")[:MAX_DESC_CHARS]
    if not desc:
        log.info("tailor: no description for %s/%s — skipping", job["company"], job["title"])
        return {**job}  # type: ignore[return-value]

    prompt = _TAILOR_PROMPT.format(
        resume=resume,
        title=job["title"],
        company=job["company"],
        description=desc,
    )
    try:
        raw = call_fn(prompt, model, api_key)
        data = json.loads(raw)
        return {
            **job,
            "keywords_matched": data.get("keywords_matched") or [],
            "keywords_missing": data.get("keywords_missing") or [],
            "bullets_tailored": data.get("bullets_tailored") or [],
            "why_company": (data.get("why_company") or "").strip(),
        }  # type: ignore[return-value]
    except Exception as e:
        log.warning("tailor: failed for %s/%s: %s", job["company"], job["title"], e)
        return {**job}  # type: ignore[return-value]


def tailor_jobs(
    jobs: list[ScoredJob],
    resume: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    *,
    call_fn=_call_gemini,
) -> list[TailoredJob]:
    """Add tailoring fields to HIGH/MED jobs. LOW/UNSCORED pass through unchanged.

    Respects MAX_TAILOR_PER_RUN to avoid quota blowouts on large cache-miss runs.
    """
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    resume = resume or load_resume()

    if not resume or not api_key:
        log.info("tailor: no resume or API key — skipping tailoring for all jobs")
        return list(jobs)  # type: ignore[return-value]

    result: list[TailoredJob] = []
    tailor_count = 0

    for job in jobs:
        bucket = (job.get("bucket") or "UNSCORED").upper()
        if bucket not in TAILOR_BUCKETS or tailor_count >= MAX_TAILOR_PER_RUN:
            result.append({**job})  # type: ignore[arg-type]
            continue
        tailored = _tailor_job(job, resume, api_key, model, call_fn=call_fn)
        result.append(tailored)
        tailor_count += 1
        time.sleep(0.3)  # light throttle between calls

    log.info("tailor: tailored %d / %d jobs", tailor_count, len(jobs))
    return result
