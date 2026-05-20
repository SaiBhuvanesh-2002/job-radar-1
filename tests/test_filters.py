"""Filter regression tests — the user's actual targeting criteria."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from filters import title_passes, location_passes, job_passes  # noqa: E402


# ---------------- title ----------------

ACCEPT_TITLES = [
    "Senior Data Scientist",
    "Data Scientist, Experimentation",
    "Machine Learning Engineer",
    "Senior ML Engineer, Recommendations",
    "AI Engineer",
    "Applied AI Engineer",
    "Applied Scientist, Search",
    "Research Engineer, LLM Training",
    "Generative AI Engineer",
    "GenAI Engineer (Senior)",
    "Agentic AI Engineer",
    "MLOps Engineer",
    "LLM Engineer",
    "Senior AI/ML Engineer",
]

REJECT_TITLES_BY_SENIORITY = [
    ("Junior Data Scientist", "junior"),
    ("Data Scientist Intern", "intern"),
    ("ML Engineering Manager", "manager"),
    ("Director of Machine Learning", "director"),
    ("VP Engineering, ML", "vp"),
    ("Head of Data Science", "head of"),
    ("Staff ML Engineer", "staff"),
    ("Principal Data Scientist", "principal"),
    ("ML Engineer, Tech Lead", "lead"),
    ("Founding AI Engineer", "founding"),
    ("Data Science Graduate Program", "graduate"),
]

REJECT_TITLES_BY_ROLE = [
    "Senior Data Analyst",
    "Business Analyst",
    "Power BI Developer",
    "Tableau Developer",
    "Frontend Developer",
    "iOS Developer",
    "Android Developer",
    "Embedded Systems Engineer",
    "Blockchain Engineer",
    "Crypto Engineer",
    "Salesforce Admin",
    ".NET Developer",
    "Java Developer",
    "PHP Developer",
    "Customer Success Manager",
    "Marketing Operations Lead",
    "Recruiter, Engineering",
    "DevOps Engineer",
    "Site Reliability Engineer",
]

REJECT_TITLES_BY_NO_MATCH = [
    "Software Engineer",
    "Senior Software Engineer",
    "Backend Engineer",
    "Product Designer",
    "Technical Program Manager, Infrastructure",  # caught by 'manager' first
    # Regression: short positives ('rag', 'llm', 'nlp', 'genai') must NOT
    # match as substrings of unrelated words.
    "Senior Engineer, Storage Control Plane",     # 'rag' inside 'storage'
    "Senior Software Engineer, Storage Engineer",
    "Sr. Engineer, Storage",
    "Solutions Architect - Storage",
    "Fragmentation Specialist",                   # 'rag' inside 'fragmentation'
    "Hallmark Brand Strategist",                  # 'llm' inside 'hallmark'... not exactly but pattern check
]


def test_accept_titles():
    for t in ACCEPT_TITLES:
        ok, reason = title_passes(t)
        assert ok, f"expected accept: {t!r} → {reason}"


def test_reject_seniority():
    for t, hint in REJECT_TITLES_BY_SENIORITY:
        ok, reason = title_passes(t)
        assert not ok, f"expected reject: {t!r} → {reason}"


def test_reject_role():
    for t in REJECT_TITLES_BY_ROLE:
        ok, reason = title_passes(t)
        assert not ok, f"expected reject: {t!r} → {reason}"


def test_reject_no_positive_match():
    for t in REJECT_TITLES_BY_NO_MATCH:
        ok, _ = title_passes(t)
        assert not ok, f"expected reject (no positive): {t!r}"


# ---------------- location ----------------

ACCEPT_LOCATIONS = [
    "San Francisco, CA",
    "New York, NY",
    "Remote",
    "Remote (US)",
    "Remote - United States",
    "Seattle, WA",
    "Austin, TX",
    "USA",
    "United States",
    "Boston / New York",
    "US-Remote",
    "",  # empty passes
]

REJECT_LOCATIONS = [
    "London, UK",
    "Bengaluru, India",
    "Hyderabad",
    "Berlin",
    "Paris, France",
    "Singapore",
    "Tokyo, Japan",
    "Toronto, Canada",
    "Sydney, Australia",
    "São Paulo",
    "Remote (UK)",
    "EMEA",
    "Europe",
    "Remote - Europe",
    "Madrid, Spain",
]


def test_accept_locations():
    for loc in ACCEPT_LOCATIONS:
        ok, reason = location_passes(loc)
        assert ok, f"expected accept: {loc!r} → {reason}"


def test_reject_locations():
    for loc in REJECT_LOCATIONS:
        ok, reason = location_passes(loc)
        assert not ok, f"expected reject: {loc!r} → {reason}"


# ---------------- combined ----------------

def _job(title: str, location: str = "Remote") -> dict:
    return {
        "id": "x", "ats": "test", "company": "test", "title": title,
        "location": location, "team": "", "description": "",
        "url": "https://x", "remote": True, "posted_at": None,
    }


def test_job_passes_combined():
    ok, _ = job_passes(_job("Senior ML Engineer", "Remote (US)"))
    assert ok

    ok, reason = job_passes(_job("Senior ML Engineer", "London"))
    assert not ok and "location" in reason

    ok, reason = job_passes(_job("Staff ML Engineer", "Remote (US)"))
    assert not ok and "title" in reason
