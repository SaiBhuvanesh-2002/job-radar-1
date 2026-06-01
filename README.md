# Job Radar

A personal job-search automation that polls 100+ company ATS feeds every 30 minutes, filters for mid-senior AI/ML IC roles in the US, scores each new posting against your resume with Gemini, and emails a ranked HTML digest.

Runs free on GitHub Actions. Free-tier Gemini covers the scoring.

---

## What it does, step by step

Every 30 minutes [GitHub Actions](.github/workflows/job_monitor.yml) runs `job_monitor.py`, which executes this pipeline:

1. **Fetch** — pulls open postings from every company in [companies.json](companies.json) via their ATS API. Supported ATSes: **Lever**, **Greenhouse**, **Ashby**, **Workday**. Implementation lives in [ats_feed.py](ats_feed.py).
2. **Normalize** — flattens each ATS's raw JSON into a unified `Job` schema (`id`, `title`, `company`, `location`, `team`, `description`, `url`, `remote`, `posted_at`). Postings missing the fields needed for dedup are dropped.
3. **Filter** — applies title/location rules in [filters.py](filters.py):
   - **Title positives**: must contain one of *ML Engineer, AI Engineer, Data Scientist, Applied Scientist, Research Engineer, GenAI, LLM, RAG, MLOps, …* (~30 phrases). Word-boundary regex so `rag` doesn't match *sto**rag**e*.
   - **Seniority block**: rejects *intern, junior, manager, director, head of, principal, staff, lead, founding, VP*.
   - **Role block**: rejects *analyst, frontend, mobile, embedded, blockchain, devops, sales, customer success, coach, evangelist, advocate*, etc.
   - **Location**: allow-list of US states/metros + `remote`; block-list of UK/EU/APAC/LATAM/Canada cities and countries. Empty locations pass (lenient).
4. **Dedup** — hashes `sha256(ats:job_id)` for every passing job and skips ones already seen in the SQLite store ([dedup.py](dedup.py)). The DB is persisted across runs via `actions/cache`.
5. **Recency safety net** — if `actions/cache` evicts the dedup DB, the orchestrator drops any "new" posting older than 7 days so a cache miss can't flood your inbox with stale jobs ([job_monitor.py:38](job_monitor.py#L38)).
6. **Score with Gemini** — [scorer.py](scorer.py) loads your resume (from `RESUME_CONTENT` env / `resume.md`) and sends each batch of 5 jobs to `gemini-2.5-flash`. Each job gets `score` (1-10), `bucket` (`HIGH` ≥8, `MED` ≥5, `LOW` <5), and a one-sentence `rationale` citing concrete resume signals. Transient 503/429 errors are retried with exponential backoff; any final failure falls through to `UNSCORED` so the email still ships.
7. **Sort** — `HIGH → MED → LOW → UNSCORED`, alphabetical within bucket.
8. **Email** — [alerts.py](alerts.py) builds a Gmail-safe HTML digest with two-column cards: title + meta + "View posting →" button on the left, color-coded score panel on the right (emerald for HIGH, amber for MED, slate for LOW). Posted times render in **ET** (`May 23, 4:25 PM ET`) with auto EST/EDT switching. All job links open in a new tab. Sends via Gmail SMTP using an App Password.
9. **Persist** — every new job (recent or not) is marked seen in SQLite so we don't re-score or re-email it next run.

If the pipeline itself throws, a best-effort failure email goes out and the GitHub Actions run is marked failed.

---

## Layout

```
ats_feed.py          fetch + normalize for Lever/Greenhouse/Ashby/Workday
filters.py           title + seniority + role + location rules (your targeting)
dedup.py             SQLite-backed seen-jobs store
scorer.py            Gemini 2.5 Flash resume-vs-job scoring, batched + retried
alerts.py            HTML digest formatter + Gmail SMTP sender
job_monitor.py       orchestrator wired by GH Actions
verify_companies.py  one-shot probe to vet companies.json before commit
companies.json       100 verified-working {ats, slug or careers_url} entries
resume.example.md    template; copy to resume.md (gitignored) and fill in
tests/               pytest — 30 tests covering filters, normalize, scorer
.github/workflows/   cron schedule (every 30 min) + workflow_dispatch
```

---

## Setup

### Local

```bash
git clone <repo> && cd job-radar
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env          # fill in GMAIL_USER, GMAIL_APP_PASS, GEMINI_API_KEY
cp resume.example.md resume.md  # replace with your actual resume content

python job_monitor.py --dry-run   # fetches, filters, scores, logs — no email
python job_monitor.py --seed      # first run: mark current postings as seen
                                  # (run ONCE so the first real run doesn't flood)
python job_monitor.py             # normal run
```

### GitHub Actions

In your repo's **Settings → Secrets and variables → Actions**, add:

| Secret | What |
|---|---|
| `GMAIL_USER` | Gmail address that owns the App Password |
| `GMAIL_APP_PASS` | 16-char App Password (Google Account → Security → App passwords; requires 2FA) |
| `GEMINI_API_KEY` | Google AI Studio key — https://aistudio.google.com/apikey (NOT a Vertex AI key) |
| `RESUME_CONTENT` | Paste the full text of your `resume.md` |

The workflow runs on a `*/30 * * * *` cron and can also be triggered manually with `mode: normal | dry-run | seed`.

---

## Customizing your targeting

Open [filters.py](filters.py) and edit these tuples in place:

- `TITLE_POSITIVE` — phrases that mark a role as in-scope. Title-only matching; descriptions are too noisy.
- `SENIORITY_BLOCK_RE` — seniority words to reject (word-boundary regex).
- `ROLE_BLOCK` — non-engineering roles that title positives accidentally drag in (e.g. *coach*, *evangelist*).
- `LOCATION_ALLOW` / `LOCATION_BLOCK` — block-list wins. Empty locations pass.

Rerun `python job_monitor.py --dry-run` to see how your changes affect today's batch.

---

## Adding companies

Add entries to [companies.json](companies.json). For each new company, run `python verify_companies.py` first — it probes the ATS endpoint and reports companies that return 0 jobs or 404. Don't commit unverified entries; the user-facing memory of which slugs work where is `companies.json` itself.

---

## How scoring decides HIGH / MED / LOW

The Gemini prompt in [scorer.py](scorer.py) hands the model both your resume and the job's title + company + location + team + (truncated) description, then asks for an integer 1-10 with a one-sentence rationale citing concrete resume signals (specific tools, specific years, specific projects). Buckets:

- **HIGH (8-10)** — role title, seniority, and tech stack all align
- **MED (5-7)** — role/seniority match, stack partial; OR strong stack, adjacent role
- **LOW (1-4)** — major mismatch on seniority, stack, or role family

Rationales reference your resume specifically (e.g. *"Strong: 3+ yrs RAG/LangChain match"* vs. *"Mismatch: requires 7+ yrs and TS frontend"*). If you find scores are systematically off, tune `_PROMPT_TEMPLATE` in [scorer.py](scorer.py).

---

## Development

```bash
python -m pytest tests/             # 30 tests, ~0.1s
python job_monitor.py --dry-run     # full pipeline, no email, no DB writes
```

Cost ceiling per day at the current cadence (48 runs × ~5-20 jobs × 1 Gemini call per 5 jobs) is well under the 1500 req/day free-tier limit for `gemini-2.5-flash`.
