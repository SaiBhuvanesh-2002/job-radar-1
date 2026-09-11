"""Probe ATS endpoints before adding rows to companies.json.

Usage:
  python verify_companies.py                          # built-in CANDIDATES
  python verify_companies.py --file companies.json    # probe every row in a file
  python verify_companies.py --file companies_full.json --only-new --limit 50
  python verify_companies.py --file companies_full.json --filter ai --only-new --limit 30
  python verify_companies.py --json                   # emit working rows as JSON

Supported ATS values match ats_feed.py: lever, greenhouse, ashby, workday,
bamboohr, icims, workable, jazzhr, rippling.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

from ats_feed import ATS_FETCHERS, fetch_workday

REPO_ROOT = Path(__file__).resolve().parent
COMPANIES_PATH = REPO_ROOT / "companies.json"

# Hand-picked AI/ML boards — quick smoke list when no --file is passed.
CANDIDATES: list[dict[str, str]] = [
    {"name": "Cohere", "ats": "ashby", "slug": "cohere"},
    {"name": "Inflection AI", "ats": "greenhouse", "slug": "inflectionai"},
    {"name": "Together AI", "ats": "greenhouse", "slug": "togetherai"},
    {"name": "Mistral AI", "ats": "lever", "slug": "mistral"},
    {"name": "Character.AI", "ats": "ashby", "slug": "character"},
    {"name": "insitro", "ats": "ashby", "slug": "insitro"},
    {"name": "RunPod", "ats": "greenhouse", "slug": "runpod"},
    {"name": "Recursion", "ats": "greenhouse", "slug": "recursionpharmaceuticals"},
    {"name": "Harvey", "ats": "ashby", "slug": "harvey"},
    {"name": "Pinecone", "ats": "ashby", "slug": "pinecone"},
    {"name": "Figma", "ats": "greenhouse", "slug": "figma"},
    {"name": "Robinhood", "ats": "greenhouse", "slug": "robinhood"},
    {"name": "Airbnb", "ats": "greenhouse", "slug": "airbnb"},
]


def _company_key(row: dict[str, str]) -> tuple[str, str]:
    ats = row["ats"].lower()
    if ats == "workday":
        return ats, (row.get("careers_url") or "").strip().lower()
    return ats, (row.get("slug") or "").strip().lower()


def load_existing_keys(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    rows = json.loads(path.read_text())
    return {_company_key(r) for r in rows}


def probe(row: dict[str, str]) -> tuple[bool, int, str]:
    ats = row["ats"].lower()
    try:
        if ats == "workday":
            url = row.get("careers_url", "")
            if not url:
                return False, 0, "missing careers_url"
            jobs = fetch_workday(url)
        elif ats in ATS_FETCHERS:
            slug = row.get("slug", "")
            if not slug:
                return False, 0, "missing slug"
            jobs = ATS_FETCHERS[ats](slug)
        else:
            return False, 0, f"unknown ats: {ats}"
        return True, len(jobs), "ok"
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        return False, 0, f"HTTP {code}"
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {e}"


def load_candidates(
    file_path: Path | None,
    *,
    only_new: bool,
    filter_re: re.Pattern[str] | None,
    limit: int | None,
) -> list[dict[str, str]]:
    if file_path is None:
        rows = list(CANDIDATES)
    else:
        rows = json.loads(file_path.read_text())
        if not isinstance(rows, list):
            raise SystemExit(f"{file_path}: expected a JSON array")

    if only_new:
        existing = load_existing_keys(COMPANIES_PATH)
        rows = [r for r in rows if _company_key(r) not in existing]

    if filter_re is not None:
        rows = [
            r for r in rows
            if filter_re.search(r.get("name", ""))
            or filter_re.search(r.get("slug", ""))
            or filter_re.search(r.get("careers_url", ""))
        ]

    if limit is not None:
        rows = rows[:limit]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe ATS company boards")
    parser.add_argument(
        "--file", type=Path, default=None,
        help="JSON file of {name, ats, slug|careers_url} rows (default: built-in CANDIDATES)",
    )
    parser.add_argument(
        "--only-new", action="store_true",
        help="skip rows already present in companies.json",
    )
    parser.add_argument(
        "--filter", metavar="REGEX", default=None,
        help="keep rows whose name/slug/url matches this regex (case-insensitive)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="probe at most N rows (useful with companies_full.json)",
    )
    parser.add_argument("--json", action="store_true", help="emit working rows as JSON")
    parser.add_argument(
        "--delay", type=float, default=0.15,
        help="seconds between probes (default 0.15)",
    )
    args = parser.parse_args()

    filter_re = re.compile(args.filter, re.I) if args.filter else None
    candidates = load_candidates(
        args.file, only_new=args.only_new, filter_re=filter_re, limit=args.limit,
    )

    if not candidates:
        print("No candidates to probe.", file=sys.stderr)
        return 1

    print(f"{'NAME':<36} {'ATS':<12} {'KEY':<28} {'COUNT':>5}  STATUS")
    print("-" * 96)

    working: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()

    for row in candidates:
        name = row.get("name", "?")
        ats = row.get("ats", "?").lower()
        key_display = row.get("slug") or (row.get("careers_url") or "")[:28]
        ok, count, status = probe(row)
        mark = "OK " if ok and count else "FAIL"
        print(f"{name:<36} {ats:<12} {key_display:<28} {count:>5}  {mark}  {status}")
        if ok and count:
            ck = _company_key(row)
            if ck not in seen_keys:
                seen_keys.add(ck)
                working.append({
                    "name": name,
                    "ats": ats,
                    **({"careers_url": row["careers_url"]} if ats == "workday"
                       else {"slug": row["slug"]}),
                })
        if args.delay:
            time.sleep(args.delay)

    print()
    print(f"# {len(working)} working / {len(candidates)} probed")
    if args.json:
        print(json.dumps(working, indent=2))
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(main())
