"""One-off probe: confirms each candidate company's ATS endpoint is reachable
and returns a non-empty list of postings.

Usage:
  python verify_companies.py            # uses CANDIDATES below
  python verify_companies.py --json     # also emit a companies.json snippet

Edit CANDIDATES, run, then paste the working rows into companies.json.
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

from ats_feed import fetch_ashby, fetch_greenhouse, fetch_lever

# (display_name, ats, slug) — slugs sourced from santifer/career-ops/templates/portals.example.yml
# (Greenhouse/Ashby/Lever entries only; Workable + custom portals skipped for v1).
# Run this script to confirm each slug is live before adding to companies.json.
CANDIDATES: list[tuple[str, str, str]] = [
    ("Modal", "ashby", "modal"),
    ("OpenAI", "ashby", "openai"),
    ("Anthropic", "greenhouse", "anthropic"),
    ("PolyAI", "greenhouse", "polyai"),
    ("Intercom", "greenhouse", "intercom"),
    ("Hume AI", "greenhouse", "humeai"),
    ("ElevenLabs", "ashby", "elevenlabs"),
    ("Deepgram", "ashby", "deepgram"),
    ("Vapi", "ashby", "vapi"),
    ("Bland AI", "ashby", "bland"),
    ("Airtable", "greenhouse", "airtable"),
    ("Vercel", "greenhouse", "vercel"),
    ("Arize AI", "greenhouse", "arizeai"),
    ("RunPod", "greenhouse", "runpod"),
    ("Weights & Biases", "greenhouse", "coreweave"),
    ("Glean", "greenhouse", "gleanwork"),
    ("Sierra", "ashby", "sierra"),
    ("Decagon", "ashby", "decagon"),
    ("Lindy", "ashby", "lindy"),
    ("n8n", "ashby", "n8n"),
    ("Zapier", "ashby", "zapier"),
    ("Boomi", "greenhouse", "boomilp"),
    ("Cohere", "ashby", "cohere"),
    ("LangChain", "ashby", "langchain"),
    ("Pinecone", "ashby", "pinecone"),
    ("Mistral AI", "lever", "mistral"),
    ("Palantir", "lever", "palantir"),
    ("Later", "greenhouse", "later"),
    ("Safari AI", "greenhouse", "safariai"),
    ("Hootsuite", "greenhouse", "hootsuite"),
    ("Klue", "ashby", "klue"),
    ("Glacis AI", "ashby", "glacis-ai"),
    ("Attio", "ashby", "attio"),
    ("Black Forest Labs", "greenhouse", "blackforestlabs"),
    ("Contentful", "greenhouse", "contentful"),
    ("GetYourGuide", "greenhouse", "getyourguide"),
    ("HelloFresh", "greenhouse", "hellofresh"),
    ("N26", "greenhouse", "n26"),
    ("Trade Republic", "greenhouse", "traderepublicbank"),
    ("SumUp", "greenhouse", "sumup"),
    ("Scandit", "greenhouse", "scandit"),
    ("Cradle", "ashby", "cradlebio"),
    ("Photoroom", "ashby", "photoroom"),
    ("Stability AI", "greenhouse", "stabilityai"),
    ("Lovable", "ashby", "lovable"),
    ("Spotify", "lever", "spotify"),
    ("Amplemarket", "greenhouse", "amplemarket"),
    ("Perplexity", "ashby", "perplexity"),
    ("Clay Labs", "ashby", "claylabs"),
    ("Hightouch", "greenhouse", "hightouch"),
    ("WorkOS", "ashby", "workos"),
    ("Supabase", "ashby", "supabase"),
    ("Resend", "ashby", "resend"),
    ("Clerk", "ashby", "clerk"),
    ("Inngest", "ashby", "inngest"),
    ("PlanetScale", "greenhouse", "planetscale"),
    # Added 2026-05-20 (user request, all probed live)
    ("Databricks", "greenhouse", "databricks"),
    ("Observe.AI", "greenhouse", "observeai"),
    ("Snowflake", "ashby", "snowflake"),
    ("AlphaSense", "greenhouse", "alphasense"),
    ("Affirm", "greenhouse", "affirm"),
    ("Cresta", "greenhouse", "cresta"),
    ("Datadog", "greenhouse", "datadog"),
    ("Stripe", "greenhouse", "stripe"),
    ("Harvey", "ashby", "harvey"),
    ("Vanta", "ashby", "vanta"),
    ("Writer", "ashby", "writer"),
    ("Abridge", "ashby", "abridge"),
    ("Scale AI", "greenhouse", "scaleai"),
    ("Twilio", "greenhouse", "twilio"),
    ("Smartsheet", "greenhouse", "smartsheet"),
    # Probed-but-not-supported (breadcrumbs — these run on ATSes outside v1's
    # Lever/Greenhouse/Ashby/Workday set; revisit if we add adapters):
    # - iCIMS:           Cotiviti, ICE
    # - SmartRecruiters: Bayer*, Moveworks (now ServiceNow), ServiceNow, Syngenta
    #                    (*Bayer also runs SAP SuccessFactors)
    # - SAP SuccessFactors: EY, John Deere, AGCO
    # - TalentBrew/Avature: Intuit, Cargill, Cox Enterprises, Delta Air Lines
    # - Eightfold:       Trimble, BCG X (also Phenom)
    # - Oracle Taleo:    Optum / UnitedHealth
    # - Custom portals:  Microsoft, Google, Amazon, Elastic, McKinsey QuantumBlack
]


def probe(name: str, ats: str, slug: str) -> tuple[bool, int, str]:
    try:
        if ats == "lever":
            jobs = fetch_lever(slug)
        elif ats == "greenhouse":
            jobs = fetch_greenhouse(slug)
        elif ats == "ashby":
            jobs = fetch_ashby(slug)
        else:
            return False, 0, f"unknown ats: {ats}"
        return True, len(jobs), "ok"
    except requests.HTTPError as e:
        return False, 0, f"HTTP {e.response.status_code}"
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit companies.json snippet")
    args = parser.parse_args()

    print(f"{'NAME':<32} {'ATS':<12} {'SLUG':<24} {'COUNT':>5}  STATUS")
    print("-" * 92)

    working: list[dict[str, str]] = []
    seen_display: set[str] = set()  # dedupe by display prefix so one hit per company
    for name, ats, slug in CANDIDATES:
        ok, count, status = probe(name, ats, slug)
        mark = "OK " if ok else "FAIL"
        print(f"{name:<32} {ats:<12} {slug:<24} {count:>5}  {mark}  {status}")
        if ok and count > 0:
            display = name.split(" (")[0]
            if display not in seen_display:
                seen_display.add(display)
                working.append({"name": display, "ats": ats, "slug": slug})

    print()
    print(f"# {len(working)} working rows")
    if args.json:
        print(json.dumps(working, indent=2))
    else:
        print("# pass --json to emit a companies.json snippet")
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(main())
