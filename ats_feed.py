"""Fetch + normalize job postings from Lever, Greenhouse, Ashby, and Workday."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TypedDict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


class Job(TypedDict):
    id: str
    ats: str
    company: str
    title: str
    location: str
    team: str
    description: str
    url: str
    remote: bool
    posted_at: str | None  # ISO8601 or None


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "job-radar/0.1 (+github actions)"})
    return s


SESSION = make_session()
REQUEST_TIMEOUT = 15


def _iso_from_ms(ms: int | float | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return None


def fetch_lever(slug: str) -> list[dict[str, Any]]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    log.info("lever/%s -> %s (%d bytes)", slug, r.status_code, len(r.content))
    r.raise_for_status()
    data = r.json()
    return [{"_ats": "lever", "_company": slug, **j} for j in data]


def fetch_greenhouse(slug: str) -> list[dict[str, Any]]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    log.info("greenhouse/%s -> %s (%d bytes)", slug, r.status_code, len(r.content))
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    return [{"_ats": "greenhouse", "_company": slug, **j} for j in jobs]


def fetch_ashby(slug: str) -> list[dict[str, Any]]:
    """Public REST job board API.

    Verified working 2026-05; the older `non-user-graphql` schema dropped
    `publishedJobBoard` and now 400s.
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    log.info("ashby/%s -> %s (%d bytes)", slug, r.status_code, len(r.content))
    r.raise_for_status()
    jobs = r.json().get("jobs", []) or []
    return [{"_ats": "ashby", "_company": slug, **j} for j in jobs]


_WORKDAY_URL_RE = re.compile(
    r"https?://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/([^/?#]+)", re.IGNORECASE
)

# Workday's `locationsText` is unreliable: some tenants (Accenture) leave it
# empty, others (Roche, Nvidia) report "N Locations" for multi-city postings.
# We then have to fish the real city out of bulletFields or externalPath, or
# the location filter passes them all through as "empty location (pass)".
_WORKDAY_MULTI_LOC_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.IGNORECASE)
_WORKDAY_PATH_CITY_RE = re.compile(r"^/job/([^/]+)/", re.IGNORECASE)

# Tenants where Workday's listing is NOT reliably sorted by post date.
# Empirically verified 2026-05: Accenture returns Today-posted jobs at
# offsets 1000+ while showing 30+d-old jobs at offset 500, so the
# date-descending early-stop in fetch_workday would lose fresh roles here.
_WORKDAY_UNSORTED_TENANTS = frozenset({"accenture"})

# Workday's listing API returns a relative `postedOn` string like
# "Posted Today", "Posted Yesterday", "Posted 5 Days Ago", "Posted 30+ Days Ago".
# Day-level resolution only; anything older than 30d is clamped to "30+".
_WORKDAY_POSTED_DAYS_RE = re.compile(
    r"^\s*Posted\s+(\d+)(\+)?\s+Days?\s+Ago\s*$", re.IGNORECASE
)


def _parse_workday_posted_on(s: str | None) -> str | None:
    """Translate Workday's relative `postedOn` string to an ISO UTC datetime.

    Returns None if the string is missing or unparseable. "30+ Days Ago"
    resolves to 31 days back — older than the 7d recency window in
    job_monitor, so those listings get dropped at the email stage.
    """
    if not s:
        return None
    sl = s.strip().lower()
    now = datetime.now(timezone.utc)
    if sl == "posted today":
        return now.isoformat()
    if sl == "posted yesterday":
        return (now - timedelta(days=1)).isoformat()
    m = _WORKDAY_POSTED_DAYS_RE.match(s)
    if m:
        days = int(m.group(1))
        if m.group(2):  # "30+" sentinel — clamp past the recency window.
            days = max(days, 31)
        return (now - timedelta(days=days)).isoformat()
    return None


def _workday_page_is_all_stale(postings: list[dict[str, Any]]) -> bool:
    """True if every posting on the page is `Posted 30+ Days Ago`."""
    if not postings:
        return False
    for p in postings:
        po = (p.get("postedOn") or "").strip().lower()
        if po != "posted 30+ days ago":
            return False
    return True


def _workday_location(raw: dict[str, Any]) -> str:
    """Best-effort location string for a Workday posting.

    Fallback order: locationsText → bulletFields[1] → externalPath city slug.
    "Location Negotiable" and "N Locations" placeholders are skipped.
    """
    def _usable(s: str | None) -> bool:
        if not s:
            return False
        sl = s.strip().lower()
        return sl != "location negotiable" and not _WORKDAY_MULTI_LOC_RE.match(sl)

    loc = raw.get("locationsText") or ""
    if _usable(loc):
        return loc

    bf = raw.get("bulletFields") or []
    # bulletFields[0] is always the req ID; the second entry, when present,
    # is the city for Accenture-style tenants.
    if len(bf) > 1 and _usable(bf[1]):
        return bf[1]

    m = _WORKDAY_PATH_CITY_RE.match(raw.get("externalPath") or "")
    if m:
        return m.group(1).replace("-", " ")
    return loc  # may be "" or "N Locations" — caller decides what to do


def fetch_workday(careers_url: str) -> list[dict[str, Any]]:
    """Workday CXS frontend API — paginated POST.

    careers_url format: https://{tenant}.{wdserver}.myworkdayjobs.com/{board}
    e.g.  https://roche.wd3.myworkdayjobs.com/roche-ext
    """
    m = _WORKDAY_URL_RE.match(careers_url)
    if not m:
        raise ValueError(f"workday: cannot parse careers URL: {careers_url!r}")
    tenant, wdserver, board = m.group(1), m.group(2), m.group(3)
    base_url = f"https://{tenant}.{wdserver}.myworkdayjobs.com"
    api_url = f"{base_url}/wday/cxs/{tenant}/{board}/jobs"

    all_postings: list[dict[str, Any]] = []
    offset = 0
    # Workday CXS rejects limit > 20 with HTTP 400 (no error message body).
    limit = 20
    # Some tenants (e.g. homedepot) only return `total` on the first page —
    # later pages report total=0. Capture it once and reuse.
    total: int | None = None
    allow_early_stop = tenant.lower() not in _WORKDAY_UNSORTED_TENANTS

    while True:
        r = SESSION.post(
            api_url,
            json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
            timeout=REQUEST_TIMEOUT,
        )
        log.info("workday/%s/%s -> %s (%d bytes)", tenant, board, r.status_code, len(r.content))
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings") or []
        if not postings:
            break
        for p in postings:
            p["_ats"] = "workday"
            p["_company"] = tenant
            p["_base_url"] = base_url
            p["_board"] = board
        all_postings.extend(postings)
        offset += len(postings)
        if total is None:
            total = data.get("total") or 0
        if offset >= total:
            break
        # Most Workday tenants sort by post date descending: once we hit a
        # full page of "Posted 30+ Days Ago" everything past it is older
        # too, so further pages have no chance of clearing the 7d recency
        # filter. Skipped for tenants in _WORKDAY_UNSORTED_TENANTS.
        if allow_early_stop and _workday_page_is_all_stale(postings):
            log.info(
                "workday/%s/%s early-stop at offset %d: page entirely 30+d old",
                tenant, board, offset,
            )
            break

    return all_postings


ATS_FETCHERS: dict[str, Callable[[str], list[dict[str, Any]]]] = {
    "lever": fetch_lever,
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
}


def fetch_all_jobs(companies: list[dict[str, str]]) -> list[dict[str, Any]]:
    all_jobs: list[dict[str, Any]] = []
    for entry in companies:
        ats = entry["ats"]
        # Workday entries use careers_url; all others use slug.
        arg = entry.get("careers_url") if ats == "workday" else entry.get("slug", "")
        label = entry.get("name") or arg
        try:
            jobs = ATS_FETCHERS[ats](arg)
            all_jobs.extend(jobs)
            log.info("  %s/%s fetched %d jobs", ats, label, len(jobs))
        except Exception as e:
            log.warning("  [fetch fail] %s/%s: %s", ats, label, e)
        time.sleep(0.5)
    return all_jobs


def normalize_job(raw: dict[str, Any]) -> Job | None:
    """Map an ATS-specific raw posting into the unified Job schema.

    Returns None if the posting is missing the fields we need for dedup
    (id, title, url) — the orchestrator drops these and logs.
    """
    ats = raw.get("_ats")
    company_slug = raw.get("_company", "")
    try:
        if ats == "lever":
            categories = raw.get("categories") or {}
            location = categories.get("location") or ""
            return Job(
                id=str(raw["id"]),
                ats="lever",
                company=company_slug,
                title=raw.get("text", ""),
                location=location,
                team=categories.get("team") or categories.get("department") or "",
                description=(raw.get("descriptionPlain") or "")[:2000],
                url=raw.get("hostedUrl", ""),
                remote="remote" in location.lower(),
                posted_at=_iso_from_ms(raw.get("createdAt")),
            )
        if ats == "greenhouse":
            loc = (raw.get("location") or {}).get("name", "") or ""
            departments = raw.get("departments") or []
            team = departments[0].get("name", "") if departments else ""
            return Job(
                id=str(raw["id"]),
                ats="greenhouse",
                company=company_slug,
                title=raw.get("title", ""),
                location=loc,
                team=team,
                description=(raw.get("content") or "")[:2000],
                url=raw.get("absolute_url", ""),
                remote="remote" in loc.lower(),
                posted_at=raw.get("updated_at") or raw.get("first_published"),
            )
        if ats == "ashby":
            # REST shape: department, team, location, jobUrl, publishedAt, descriptionPlain.
            location = raw.get("location") or ""
            return Job(
                id=str(raw["id"]),
                ats="ashby",
                company=company_slug,
                title=raw.get("title", ""),
                location=location,
                team=raw.get("team") or raw.get("department") or "",
                description=(raw.get("descriptionPlain") or raw.get("descriptionHtml") or "")[:2000],
                url=raw.get("jobUrl") or raw.get("applyUrl", ""),
                remote=bool(raw.get("isRemote")) or "remote" in location.lower(),
                posted_at=raw.get("publishedAt"),
            )
        if ats == "workday":
            # CXS listings API: no posted_at, no description at listing level.
            # externalPath is the stable identifier; full URL is
            # {base_url}/{board}/{externalPath}. Omitting the board segment
            # 404s on every tenant we've checked.
            external_path = raw.get("externalPath", "")
            base_url = raw.get("_base_url", "")
            board = raw.get("_board", "")
            location = _workday_location(raw)
            job_url = (
                f"{base_url}/{board}/{external_path.lstrip('/')}"
                if external_path and board
                else ""
            )
            return Job(
                id=external_path,
                ats="workday",
                company=company_slug,
                title=raw.get("title", ""),
                location=location,
                team="",
                description="",
                url=job_url,
                remote="remote" in location.lower(),
                posted_at=_parse_workday_posted_on(raw.get("postedOn")),
            )
        log.warning("unknown ats: %s", ats)
        return None
    except (KeyError, TypeError) as e:
        log.warning("[normalize fail] ats=%s company=%s err=%s", ats, company_slug, e)
        return None
