# Audit — 2026-04-08

Performed before the full system rebuild. Every file is accounted for.

## Decision legend
- **KEEP** — directly useful in new system, left in place
- **ARCHIVE** → `archive/2026-04-08_<name>` — valuable history, not active
- **DELETE** — genuinely empty or pure duplicates (none in this repo)

---

## Root files

| File | Decision | Reason |
|------|----------|--------|
| `CLAUDE.md` | KEEP | Per standing rules — never deleted, will be replaced in Step 10 |
| `INITIAL_CONTEXT.md` | KEEP | Origin story and user intent — reference for future conversations |
| `.gitignore` | KEEP | Updated for new repo structure |
| `.env.example` | ARCHIVE | References Google Sheets / Telegram vars — new system uses different env |
| `requirements.txt` | ARCHIVE | Telegram, gspread, matplotlib etc — new stack is lighter |
| `SETUP.md` | ARCHIVE | Setup guide for the old Google Sheets + Telegram system |
| `Procfile` | ARCHIVE | Railway deployment config — new system runs locally + GitHub Actions only |
| `railway.toml` | ARCHIVE | Railway config — decommissioned |

## src/ (all archived — old Telegram + Google Sheets system)

| File | Decision | Reason |
|------|----------|--------|
| `run_coach.py` | ARCHIVE | 5986-line CLI entry point orchestrating Telegram + Sheets — fully replaced |
| `telegram_bot.py` | ARCHIVE | 24/7 Telegram bot — Telegram decommissioned |
| `telegram_utils.py` | ARCHIVE | Telegram send-only helper — no longer needed |
| `cascade_levels.py` | ARCHIVE | Close-day/weekly/monthly/annual cascade — replaced by simpler file-based approach |
| `cascade_state.py` | ARCHIVE | State machine for cascade — not needed in new system |
| `iteration_zero.py` | ARCHIVE | Telegram-based init interview — replaced by standalone prompt in Step 9 |
| `memory.py` | ARCHIVE | Google Sheets Coach Memory R/W (50 domains) — replaced by SQLite + JSON files |
| `sheet_sync.py` | ARCHIVE | Watermark-based delta sync against Sheets — not needed |
| `sheets.py` | ARCHIVE | Google Sheets reader + week tab parser — not needed |
| `writeback.py` | ARCHIVE | Applies program changes back to Google Sheet — not needed |
| `config.py` | ARCHIVE | Env vars for Sheets + Telegram — new system uses simpler config |
| `prompt.py` | ARCHIVE | System prompt for Telegram coach persona |
| `planner.py` | ARCHIVE | Strategic planning pass via LLM → Sheets |
| `processor.py` | ARCHIVE | Telegram message classifier |
| `program_agent.py` | ARCHIVE | LLM-driven program designer (Excel output) — Claude produces programs on demand, not via pipeline |
| `workout_agent.py` | ARCHIVE | Real-time workout adaptation via Telegram — not needed |
| `health_agent.py` | ARCHIVE | Health/recovery agent via Telegram — not needed |
| `gmail.py` | ARCHIVE | Gmail OAuth sender — not needed in file-based system |
| `garmin.py` | ARCHIVE | Working GarminClient — logic reused in new `scripts/garmin_sync.py` |
| `health_science.py` | ARCHIVE | Correlation engine operating on Sheets data model — logic may be reused later |
| `strength_tracker.py` | ARCHIVE | Multi-formula e1RM engine (Epley, Brzycki, Wathan, Mayhew) — logic reused in `scripts/estimate_strength.py` |
| `projections.py` | ARCHIVE | Linear regression projections — reusable logic, archived for reference |
| `training_data_store.py` | ARCHIVE | Data cleaning for Sheets model — replaced by SQLite |
| `charts.py` | ARCHIVE | Matplotlib PNG generation for Telegram — not needed |
| `cardio_zones.py` | ARCHIVE | Garmin cardio analytics for Telegram — may be ported to dashboard later |
| `build_strength_program.py` | ARCHIVE | Excel program builder — user reads Excel directly; Claude produces programs, doesn't parse them as input |

## tests/

| File | Decision | Reason |
|------|----------|--------|
| `test_core_logic.py` | ARCHIVE | 352 tests for old Telegram/Sheets architecture — new tests in `scripts/tests/` |
| `fixtures/` (10 files) | ARCHIVE | Test fixtures for cascade/Telegram system |
| `simulate/` (4 files) | ARCHIVE | Simulation engine for old pipeline |

## .github/workflows/

| File | Decision | Reason |
|------|----------|--------|
| `coach.yml` | ARCHIVE | Main cascade pipeline (cron, Telegram, Sheets) — replaced by garmin_sync.yml |
| `proactive.yml` | ARCHIVE | Check-in pipeline (cron, Telegram) — replaced by garmin_sync.yml |
| `test.yml` | ARCHIVE | Dummy test workflow — new tests run via scripts/tests/ |

## docs/

| File | Decision | Reason |
|------|----------|--------|
| `AUDIT_REPORT.md` | ARCHIVE | Architecture audit of old system (V18) — superseded by this file |

## .claude/

| File | Decision | Reason |
|------|----------|--------|
| `settings.local.json` | KEEP | Claude Code local settings — not touched |

---

## New structure built in this session

```
data/coach.db              — SQLite, single source of truth
sessions/template.md       — session log template
sessions/example_...md     — realistic filled example
scripts/init_db.py         — schema creation
scripts/import_session.py  — parses session .md → inserts into DB
scripts/query_lifts.py     — lift history and trends
scripts/query_health.py    — health data queries
scripts/estimate_strength.py — multi-formula e1RM estimation
scripts/garmin_sync.py     — pulls Garmin data into health_log
scripts/tests/test_estimate_strength.py — unit tests for estimation
dashboard/index.html       — single-file dashboard
dashboard/serve.py         — minimal JSON API server
system/state.json          — current system state (stub)
system/profile.json        — user profile (stub)
system/plans/longterm.md   — 3-5 year arc (to be filled in init)
system/plans/annual.md     — current year plan (to be filled in init)
system/plans/monthly.md    — current month (to be filled in init)
system/threads.json        — open decisions
system/reasoning_log.md    — append-only decisions log
.github/workflows/garmin_sync.yml — nightly GitHub Action
requirements.txt           — new lightweight dependencies
.env.example               — new env var template
CLAUDE.md                  — new instructions for every session
```
