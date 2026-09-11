"""ATS resume safety check — Job Radar.

Runs your resume through Gemini and checks for common ATS-rejection patterns:
  • Parseable text (no images / text-boxes)
  • Standard fonts and heading hierarchy
  • Keyword coverage for your target role
  • Formatting issues that confuse ATS parsers

Usage:
  python check_resume.py                   # checks resume.md, prints report
  python check_resume.py --resume path.md  # checks a specific file
  python check_resume.py --json            # machine-readable JSON output

Exits 0 if score >= 70, 1 otherwise — safe to wire into CI.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("check_resume")

DEFAULT_MODEL = "gemini-2.5-flash"

_CHECK_PROMPT = """\
You are an expert ATS (Applicant Tracking System) resume reviewer. Analyze the resume below and return STRICT JSON with these fields:

- score: integer 0-100 (overall ATS friendliness)
- checks: object with these boolean fields:
  - parseable_text: true if all text is plain readable text (not inside tables, text boxes, or as images)
  - standard_fonts: true if only standard fonts are likely used (no decorative/icon fonts)
  - semantic_headings: true if section headings follow a clear, ATS-readable pattern (EXPERIENCE, EDUCATION, SKILLS, etc.)
  - contact_info_present: true if name, email, phone or LinkedIn are present
  - dates_formatted: true if employment dates are in a consistent, parseable format
  - no_headers_footers: true if critical info is NOT locked inside headers/footers
  - keywords_ai_ml: true if the resume includes AI/ML keywords (machine learning, deep learning, Python, TensorFlow/PyTorch, NLP, etc.)
  - no_tables: true if no complex multi-column tables are used (single-column tables OK)
  - no_graphics: true if no charts, logos, or infographic elements are present
  - action_verbs: true if bullet points start with strong action verbs
- issues: list of specific problems found (each a short string, e.g. "Employment gap Jan–Mar 2023 unexplained")
- suggestions: list of up to 5 actionable improvement tips (short strings)
- keywords_present: list of AI/ML keywords found in the resume
- keywords_missing: list of important AI/ML keywords NOT found (suggest additions)

Return ONLY valid JSON — no markdown fences, no prose.

=== RESUME ===
{resume}
=== END RESUME ===
"""

_CHECK_LABELS = {
    "parseable_text":      "Parseable text (no images/boxes)",
    "standard_fonts":      "Standard fonts",
    "semantic_headings":   "Semantic section headings",
    "contact_info_present":"Contact info present",
    "dates_formatted":     "Dates consistently formatted",
    "no_headers_footers":  "No critical info in headers/footers",
    "keywords_ai_ml":      "AI/ML keywords present",
    "no_tables":           "No complex tables",
    "no_graphics":         "No decorative graphics",
    "action_verbs":        "Bullets start with action verbs",
}


def _call_gemini(prompt: str, model: str, api_key: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return resp.text or ""


def check_resume(
    resume_text: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Run Gemini ATS check on resume_text. Returns the parsed result dict."""
    prompt = _CHECK_PROMPT.format(resume=resume_text)
    raw = _call_gemini(prompt, model, api_key)
    return json.loads(raw)


def print_report(result: dict) -> None:
    score = result.get("score", 0)
    checks = result.get("checks", {})
    issues = result.get("issues", [])
    suggestions = result.get("suggestions", [])
    kw_present = result.get("keywords_present", [])
    kw_missing = result.get("keywords_missing", [])

    bar = "█" * (score // 5) + "░" * (20 - score // 5)
    grade = "✅ PASS" if score >= 70 else "⚠️  NEEDS WORK" if score >= 50 else "❌ FAIL"

    print(f"\n{'='*54}")
    print(f"  Job Radar — ATS Resume Safety Check")
    print(f"{'='*54}")
    print(f"  Score: {score}/100  {bar}  {grade}")
    print(f"{'='*54}\n")

    print("Checks:")
    for key, label in _CHECK_LABELS.items():
        val = checks.get(key, False)
        icon = "✅" if val else "❌"
        print(f"  {icon}  {label}")

    if issues:
        print("\nIssues found:")
        for i in issues:
            print(f"  ⚠  {i}")

    if suggestions:
        print("\nSuggestions:")
        for s in suggestions:
            print(f"  →  {s}")

    if kw_present:
        print(f"\nAI/ML keywords found ({len(kw_present)}): {', '.join(kw_present)}")

    if kw_missing:
        print(f"\nKeywords to consider adding: {', '.join(kw_missing)}")

    print()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="ATS resume safety check")
    parser.add_argument(
        "--resume", default="resume.md",
        help="Path to resume file (default: resume.md)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of human-readable report",
    )
    parser.add_argument(
        "--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        help="Gemini model to use",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set.", file=sys.stderr)
        return 2

    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"ERROR: Resume file not found: {resume_path}", file=sys.stderr)
        return 2

    resume_text = resume_path.read_text().strip()
    if not resume_text:
        print(f"ERROR: Resume file is empty: {resume_path}", file=sys.stderr)
        return 2

    print(f"Checking {resume_path} ({len(resume_text)} chars) with {args.model}…")

    try:
        result = check_resume(resume_text, api_key, model=args.model)
    except Exception as e:
        print(f"ERROR: Gemini call failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)

    score = result.get("score", 0)
    return 0 if score >= 70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
