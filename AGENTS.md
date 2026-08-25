# Job Radar — Agent Guide

Hand this file to an AI agent (Claude, Cursor, etc.) as the source of truth for how this repo works. Prefer this over guessing from partial file reads.

---

## One-sentence summary

Personal job-search bot: every ~30 minutes it polls **113** company ATS boards, keeps only **US / remote mid-level AI–ML IC roles posted in the last 6 hours**, scores them vs the owner’s resume with **Gemini**, and emails a ranked Gmail HTML digest.

Owner goal: apply **early** (before Jobright/Simplify piles up 30–50 applicants) and submit **more relevant apps per day**.

---

## Pipeline (always this order)

```
companies.json
    → ats_feed.fetch_all_jobs / normalize_job
    → filters.job_passes          (title + location)
    → dedup.filter_unseen         (SQLite seen_jobs.db)
    → is_recent (MAX_AGE_DAYS)    (drop stale / undated)
    → scorer.score_jobs           (Gemini; optional)
    → alerts.send_digest          (Gmail SMTP)
    → dedup.mark_seen             (persist ALL new, even if not emailed)
```

Orchestrator: `job_monitor.py`  
CI: `.github/workflows/job_monitor.yml` (`*/30` cron + `workflow_dispatch`)

### Run modes

| Command | Behavior |
|---------|----------|
| `python job_monitor.py` | Normal: email + write DB |
| `python job_monitor.py --dry-run` | Full pipeline, **no** email, **no** DB writes |
| `python job_monitor.py --seed` | Mark **all current** normalized jobs seen, **no** email (first-deploy safety) |

---

## File map (everything that matters)

| File | Role |
|------|------|
| `job_monitor.py` | Orchestrator. `MAX_AGE_DAYS = 0.25` (**6 hours**). Recency drop + seed/dry-run. |
| `ats_feed.py` | Fetch + normalize Lever / Greenhouse / Ashby / Workday → unified `Job`. |
| `filters.py` | Title positives, seniority/role blocks, US/remote location allow/block. |
| `dedup.py` | SQLite `seen_jobs.db`; hash = `sha256(ats:job_id)`. |
| `scorer.py` | Gemini 2.5 Flash batch scoring (size 5); HIGH/MED/LOW/UNSCORED. |
| `alerts.py` | Gmail-safe HTML digest + failure emails. |
| `companies.json` | Company list: `{name, ats, slug}` or Workday `{name, ats, careers_url}`. |
| `verify_companies.py` | Probe ATS endpoints before adding companies. |
| `resume.example.md` | Template only. Real resume = `resume.md` (gitignored) or secret `RESUME_CONTENT`. |
| `.env.example` | Local secret template. Real `.env` is gitignored. |
| `.github/workflows/job_monitor.yml` | Actions: checkout, Python, restore/save `seen_jobs.db` cache, run pipeline. |
| `tests/` | pytest for filters, normalize, scorer. |
| `reference/` | Optional notes/examples; not on the hot path. |

**Do not commit:** `.env`, `resume.md`, `seen_jobs.db`.

---

## Unified `Job` schema

Every ATS is flattened to:

```
id, ats, company, title, location, team, description, url, remote, posted_at
```

After scoring (optional): `score`, `bucket`, `rationale`.

### `posted_at` sources (important for freshness)

| ATS | Date used |
|-----|-----------|
| Lever | `createdAt` (ms → ISO) |
| Greenhouse | **`first_published`**, then `updated_at` (edits must not fake “new”) |
| Ashby | `publishedAt` |
| Workday | Relative `postedOn` (“Posted Today”, “Posted 5 Days Ago”, “Posted 30+ Days Ago”) |

Jobs with **no** `posted_at` are **dropped** by recency (not emailed).

---

## Filtering rules (current intent)

Defined in `filters.py`. Title-only positives (descriptions are too noisy).

**Must match** at least one title positive (word-boundary regex), including e.g.:

- data scientist / data science  
- ML / machine learning / deep learning / AI engineer  
- applied scientist / research engineer / research scientist  
- GenAI / LLM / RAG / agentic / MLOps / NLP / computer vision  
- prompt engineer / foundation model / artificial intelligence  
- “machine learning” also catches titles like *Software Engineer, Machine Learning*

**Always reject (seniority):** intern, junior, manager, director, head of, chief, principal, staff, lead, VP, founding.

**Always reject (role family):** analyst, BI/Tableau, frontend/mobile, devops/SRE, sales/CS, coach/evangelist/advocate, etc.

**Location:** block non-US markets (UK/EU/APAC/Canada/…); allow US metros + `remote` + USA. Empty location **passes**.

Entry-level / graduate / new-grad titles are **allowed** (not blocked).

---

## Recency + dedup (why emails are sparse or “old”)

1. **Dedup:** once a job ID is in `seen_jobs.db`, it never emails again.  
2. **Recency:** only jobs with `posted_at` within **6 hours** are emailed (`MAX_AGE_DAYS = 0.25`).  
3. **Cache:** Actions restores/saves `seen_jobs.db` via `actions/cache`. Cache miss → many IDs look “new”; recency is the flood safety net. Stale-but-new are still `mark_seen` so they don’t loop.  
4. **Outages:** if Actions is down for days, catch-up digests can include anything still inside the age window that wasn’t seen yet.

Owner cares about applicant order (Jobright often shows 30–40 applies within ~20h). Prefer **stricter age** over flooding with day-old roles.

---

## Scoring (`scorer.py`)

- Model: `gemini-2.5-flash` (override with `GEMINI_MODEL`)  
- Resume: `RESUME_CONTENT` env (CI) **or** local `resume.md`  
- Missing key/resume → all `UNSCORED`; email still sends  
- Prompt assumes ~**3–4 yrs** AI/ML, DS/AI Engineer IC, **H1B**, US/remote OK; don’t punish sponsorship-needed US roles  
- Buckets: HIGH ≥8, MED ≥5, LOW &lt;5  
- Sort: HIGH → MED → LOW → UNSCORED  

Tune fit by editing `_PROMPT_TEMPLATE` in `scorer.py`.

---

## Companies (`companies.json`)

- **113** entries today: Workday ~47, Greenhouse ~41, Ashby ~21, Lever ~4  
- Lever / Greenhouse / Ashby → need **`slug`**  
- Workday → need full **`careers_url`** like  
  `https://{tenant}.wd{N}.myworkdayjobs.com/{board}`  
  Do **not** put `/en-US/` as the board segment; the CXS client breaks.  
- Many large employers use Phenom/custom portals — **unsupported**; don’t add them.  
- Before adding: run `python verify_companies.py` (or equivalent probe). Only commit boards that return jobs.

---

## Secrets & deploy

| Name | Where | Purpose |
|------|--------|---------|
| `GMAIL_USER` | `.env` / Actions secret | SMTP sender |
| `GMAIL_APP_PASS` | `.env` / Actions secret | Gmail App Password (2FA) |
| `ALERT_TO` | optional | Override recipient (default = GMAIL_USER) |
| `GEMINI_API_KEY` | `.env` / Actions secret | AI Studio key (not Vertex) |
| `RESUME_CONTENT` | Actions secret | Full resume text in CI |

Repo is **public** so standard Actions minutes stay free. Secrets stay in GitHub Secrets; never paste them into tracked files.

Workflow job env wires those secrets; on failure it may call `alerts.send_failure`.

---

## Local commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill secrets
cp resume.example.md resume.md

python job_monitor.py --dry-run
python -m pytest tests/
```

Trigger CI manually:

```bash
gh workflow run job_monitor.yml -f mode=normal
# modes: normal | dry-run | seed
```

---

## What agents should / shouldn’t change

**Safe / common**

- `filters.py` title/location lists  
- `MAX_AGE_DAYS` in `job_monitor.py` (freshness vs volume tradeoff)  
- `scorer.py` prompt for experience-band / sponsorship preferences  
- `companies.json` only after verifying ATS URLs/slugs  
- Workflow action versions / cron frequency  

**Be careful**

- Dedup hash format (changing it re-emails everything)  
- Greenhouse date preference (`first_published` first)  
- Workday URL shape  
- Seeding in production (marks everything seen; silences email until new posts)  

**Never**

- Commit `.env`, `resume.md`, real API keys, App Passwords  
- Add exploit/malware tooling  
- Force-push `main` or skip hooks unless the human asks  

---

## Design tradeoffs (owner preferences)

| Lever | Current choice | Effect |
|-------|----------------|--------|
| Age window | **6 hours** | Fewer emails; earlier in applicant wave |
| Titles | Broader ML/AI phrases | More apps/day without pure SWE spam |
| Seniority | Block staff/lead/manager; allow new-grad | Mid IC focus |
| Public repo | Yes | Free frequent Actions |
| Cron | Every 30 min | Near-real-time; runs ~20–25 min each |

Volume is low on quiet days because health/AI boards don’t always post matching titles inside 6h — that is expected, not a silent failure. Check Actions logs for `filter-matched`, `after dedup`, `dropped … old`, `sent email`.

---

## Quick debug checklist

1. Actions run green? (Billing/spending limit can block jobs entirely.)  
2. Log: `fetched N` → `filter-matched` → `after dedup` → `dropped … old` → `sent email`?  
3. Zero email + `after dedup: 0` → nothing new since last seen.  
4. Zero email + many dropped old → age window working; wait for fresher posts or widen `MAX_AGE_DAYS`.  
5. Stale dates in UI (Jobright) vs digest → confirm Greenhouse uses `first_published`.  
6. Gmail 535 → App Password / `GMAIL_*` secrets.

---

## Related human docs

- `README.md` — setup narrative (may lag slightly; **trust this AGENTS.md for current numbers**: 6h window, 113 companies, public repo).  
- `resume.example.md` — resume shape for local scoring.
