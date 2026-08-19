"""Title, seniority, and location filters for Job Radar.

Title-only positive matching (descriptions are too noisy — Intercom matched 166
postings against "AI"-laden descriptions). Negative matchers reject obvious
mismatches. Location filter blocks foreign markets while allowing US + Remote.

Positive matching uses word-boundary regex so short tokens like 'rag' and
'llm' don't accidentally match 'sto**rag**e' or 'ho**llm**ark'. Seniority
block also uses word boundaries ('lead' catches 'Tech Lead' but not
'leadership'). Role block and location lists are case-insensitive substring,
since their entries are multi-word and the substring matches are intentional.
"""
from __future__ import annotations

import re

from ats_feed import Job

# ---------- Title positives ----------
# A title must contain at least one of these phrases to pass.
TITLE_POSITIVE: tuple[str, ...] = (
    "data scientist",
    "ml engineer",
    "machine learning engineer",
    "ai engineer",
    "applied scientist",
    "applied ai",
    "applied ml",
    "research engineer",
    "research scientist",
    "ml researcher",
    "machine learning researcher",
    "ai researcher",
    "genai",
    "senior ai engineer",
    "genai engineer",
    "generative ai engineer",
    "conversational ai engineer",
    "enterprise ai engineer",
    "ai software engineer",
    "llm",              
    "rag",              
    "agentic",          
    "ai platform",
    "nlp engineer",
    "gen ai",
    "generative ai",
    "mlops",
    "llm engineer",
    "ai/ml",
    "inference engineer",
    "training engineer",
    "ml framework",
    "ai safety",
    "ai alignment",          # AI alignment research
)

# ---------- Title negatives (always reject) ----------
# Intern/junior stay blocked. Entry-level / graduate / new-grad are allowed.
# Staff+ and management imply more seniority than we're targeting.
# Word-boundary regex so 'lead' catches 'Tech Lead' / 'Lead, ML' but not
# 'Leadership' or 'Leads team'.
SENIORITY_BLOCK_RE = re.compile(
    r"\b(?:"
    r"intern|internship|junior|"
    r"manager|director|head\s+of|chief|"
    r"principal|staff|lead|"
    r"vp|vice\s+president|"
    r"founding"
    r")\b",
    re.IGNORECASE,
)

# Roles that are NOT data science / ML / AI engineering.
ROLE_BLOCK: tuple[str, ...] = (
    "data analyst",
    "business analyst",
    "biz analyst",
    "bi developer",
    "bi engineer",
    "power bi",
    "powerbi",
    "tableau",
    "looker",
    "qa engineer",
    "test engineer",
    "sre",
    "site reliability",
    "devops",
    "security engineer",
    "ios developer",
    "android developer",
    "mobile developer",
    "frontend developer",
    "front-end developer",
    "embedded",
    "firmware",
    "fpga",
    "asic",
    "blockchain",
    "web3",
    "crypto",
    "salesforce",
    "sap ",
    "oracle",
    "mainframe",
    "cobol",
    " .net",
    "java developer",  # bare "java " is too dangerous (javascript false-positive)
    "php developer",
    "ruby developer",
    "marketing",
    "recruiter",
    "recruiting",
    "sales engineer",
    "account executive",
    "customer success",
    "people operations",
    # Customer-facing / enablement roles that title-positives like
    # "applied ai" / "ai" accidentally drag in. Examples observed in the wild:
    # "Applied AI Coach", "Applied AI Claude Evangelist".
    "coach",
    "evangelist",
    "advocate",
    # 'researcher' as a positive can drag in non-ML researchers; exclude here.
    "user researcher",
    "ux researcher",
    "market research",
    "people research",
    "talent research",
)

# ---------- Location ----------
# Case-insensitive. Block list takes precedence over allow.
LOCATION_BLOCK: tuple[str, ...] = (
    "india", "bengaluru", "bangalore", "hyderabad", "mumbai", "delhi", "pune", "chennai",
    "united kingdom", "uk", "london", "manchester", "edinburgh", "dublin", "ireland",
    "germany", "berlin", "munich", "hamburg", "frankfurt",
    "france", "paris",
    "spain", "madrid", "barcelona",
    "netherlands", "amsterdam",
    "italy", "rome", "milan",
    "portugal", "lisbon",
    "switzerland", "zurich", "geneva", "lausanne",
    "sweden", "stockholm",
    "norway", "oslo",
    "denmark", "copenhagen",
    "finland", "helsinki",
    "poland", "warsaw",
    "ukraine", "kyiv",
    "singapore",
    "japan", "tokyo", "osaka",
    "china", "beijing", "shanghai", "shenzhen", "hong kong",
    "korea", "seoul",
    "taiwan", "taipei",
    "australia", "sydney", "melbourne",
    "new zealand", "auckland",
    "brazil", "são paulo", "sao paulo", "rio",
    "mexico", "mexico city",
    "argentina", "buenos aires",
    "canada", "toronto", "vancouver", "montreal", "ottawa",
    "israel", "tel aviv",
    "uae", "dubai", "abu dhabi",
    "south africa", "cape town", "johannesburg",
    "vietnam", "ho chi minh",
    "thailand", "bangkok",
    "philippines", "manila",
    "indonesia", "jakarta",
    "turkey", "istanbul", "ankara",
    "emea", "apac", "latam",  # region tags — usually non-US-only
    "europe", "eu only",
)

LOCATION_ALLOW: tuple[str, ...] = (
    "united states", "usa", "u.s.", "u.s.a",
    "us-", "-us", "us only", "us based", "us-based",
    "north america",  # usually US + Canada; combined with block on "canada" still works
    "americas",
    "remote",  # plain "Remote" allowed per user pref (US implied)
    # Major US metros
    "georgia",
    "atlanta",
    "san francisco", "bay area", "sf bay", "silicon valley", "mountain view", "palo alto",
    "new york", "nyc", "manhattan", "brooklyn",
    "los angeles", "la,", "santa monica",
    "seattle", "bellevue", "redmond",
    "austin", "houston", "dallas",
    "boston", "cambridge",
    "chicago",
    "denver", "boulder",
    "miami",
    "washington", "dc,", "arlington",
    "portland",
    "philadelphia",
    "san diego",
    "minneapolis",
    "phoenix",
    "nashville",
    "pittsburgh",
)


def _norm(s: str | None) -> str:
    return (s or "").lower()


def _build_positive_re(terms: tuple[str, ...]) -> re.Pattern[str]:
    """Build a single word-boundary regex from positive terms.

    Word boundaries prevent short tokens like 'rag' from matching 'storage'
    or 'llm' from matching 'hollmark'. re.escape handles slashes in 'ai/ml'.
    """
    escaped = [re.escape(t) for t in terms]
    pattern = r"\b(?:" + "|".join(escaped) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


TITLE_POSITIVE_RE = _build_positive_re(TITLE_POSITIVE)


def title_passes(title: str) -> tuple[bool, str]:
    """Return (pass, reason). Reason describes why if rejected."""
    t = _norm(title)
    if not t:
        return False, "empty title"

    m = SENIORITY_BLOCK_RE.search(t)
    if m:
        return False, f"seniority: {m.group(0)!r}"
    for bad in ROLE_BLOCK:
        if bad in t:
            return False, f"role: {bad.strip()!r}"

    m = TITLE_POSITIVE_RE.search(t)
    if m:
        return True, f"matched: {m.group(0)!r}"
    return False, "no positive match"


def location_passes(location: str) -> tuple[bool, str]:
    """Return (pass, reason). Empty location passes (lenient on missing data)."""
    loc = _norm(location)
    if not loc:
        return True, "empty location (pass)"

    for bad in LOCATION_BLOCK:
        if bad in loc:
            return False, f"blocked: {bad!r}"

    for good in LOCATION_ALLOW:
        if good in loc:
            return True, f"allowed: {good!r}"
    return False, "no US/Remote signal"


def job_passes(job: Job) -> tuple[bool, str]:
    title_ok, title_reason = title_passes(job["title"])
    if not title_ok:
        return False, f"title: {title_reason}"
    loc_ok, loc_reason = location_passes(job["location"])
    if not loc_ok:
        return False, f"location: {loc_reason}"
    return True, f"{title_reason} | {loc_reason}"
