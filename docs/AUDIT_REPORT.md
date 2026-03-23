# AUDIT REPORT — Strength Coach Agent
**Generated:** 2026-03-23 | **Architecture version:** V18

---

## 1. Repository File Tree

```
strength-coach-agent/
├── .github/workflows/
│   ├── coach.yml          main cascade pipeline (cron-driven)
│   ├── proactive.yml      briefs, check-ins, post-session (cron-driven)
│   └── test.yml           dummy test workflow
├── config/
│   ├── credentials.json   Google OAuth (git-ignored)
│   ├── token.json         OAuth token (git-ignored)
│   └── token_b64.txt      base64 for CI/CD
├── docs/
│   └── AUDIT_REPORT.md    this file
├── src/
│   ├── run_coach.py            CLI entry point (~5986 lines, 30+ flags)
│   ├── cascade_state.py        state machine + snapshot/restore (~350 lines)
│   ├── cascade_levels.py       close_day/weekly/monthly/annual/longterm (~2000 lines)
│   ├── iteration_zero.py       initialization interview via Telegram (~500 lines)
│   ├── health_science.py       pure Python correlation engine, no LLM (~800 lines)
│   ├── health_agent.py         health/recovery specialist agent (~300 lines)
│   ├── telegram_bot.py         24/7 Telegram bot (~2541 lines)
│   ├── sheet_sync.py           delta sync engine, watermark-based (~400 lines)
│   ├── sheets.py               Google Sheets reader + week tab parser (~500 lines)
│   ├── memory.py               Coach Memory sheet R/W (~1200 lines, 50 domains)
│   ├── config.py               env vars, week calc, model IDs
│   ├── prompt.py               stable SYSTEM_PROMPT definition (~300 lines)
│   ├── planner.py              strategic planning pass (~600 lines)
│   ├── processor.py            Telegram message classifier + fact extractor (~700 lines)
│   ├── garmin.py               Garmin Connect API wrapper (~300 lines)
│   ├── gmail.py                Gmail sender with OAuth2 (~200 lines)
│   ├── writeback.py            apply confirmed program changes to sheet (~700 lines)
│   ├── program_agent.py        program designer (extended thinking, REJECT/MODIFY/CREATE) (~760 lines)
│   ├── workout_agent.py        real-time workout adaptation (~135 lines)
│   ├── projections.py          pure Python 1RM/BW projection math (~600 lines)
│   ├── strength_tracker.py     weekly strength analytics (e1RM, stall, push:pull) (~1200 lines)
│   ├── training_data_store.py  data cleaning + normalization (~500 lines)
│   ├── charts.py               matplotlib PNG generation, BytesIO, no disk (~600 lines)
│   ├── cardio_zones.py         Garmin cardio analytics, 5-zone HR model (~500 lines)
│   ├── telegram_utils.py       lightweight send-only Telegram helper (~60 lines)
│   └── build_strength_program.py  Excel program builder, 8 progression methods (~2000+ lines)
├── tests/
│   └── test_core_logic.py      352+ pure logic tests (no API/sheet mocking)
├── requirements.txt
├── CLAUDE.md
├── INITIAL_CONTEXT.md
└── SETUP.md
```

---

## 2. GitHub Actions Workflows

### coach.yml — Main Coaching Pipeline

| Time (UTC) | Days | Flags | Purpose |
|---|---|---|---|
| 18:00 | Mon-Sat | `--evening-protocol` | Session proposal + open daily_planning |
| 18:00 | Sun | `--weekly` + `--evening-protocol` | Weekly email + evening protocol |
| 20:00 | Sun only | `--think` + `--export` | Strategic pass + memory backup |
| 22:00 | Mon-Sat | `--close-day` | Daily closing summary |
| 22:00 | Sun | `--close-day` + `--weekly-eval` | Daily close + weekly evaluation |

### proactive.yml — Check-ins

| Time (UTC) | Days | Flags | Purpose |
|---|---|---|---|
| 06:00 | Mon-Sat | `--brief` | Morning session prep |
| 06:00 | Sun | `--weekly-schedule` | Schedule discovery |
| 08:00 | Daily | `--proactive` | Morning check-in |
| 13:00 | Daily | `--proactive` + `--post-session` | Post-session acknowledgment |
| 14:00 | Daily | `--proactive` | Afternoon check-in |

**Note:** `--monthly-eval`, `--annual-eval`, `--longterm-eval` are NOT directly scheduled. They are triggered by internal logic inside `--close-day`, `--weekly-eval`, and `--think` based on date/escalation conditions.

---

## 3. Source Files — What Each Owns

### Entry Points

**run_coach.py** (~5986 lines)
- Owns: CLI argument parsing, all pipeline orchestration
- Reads: Program Sheet, Coach Memory (all tabs), Telegram log
- Writes: Coach State domains, Telegram messages
- 30+ flags, each mapped to a function (run_evening_protocol, run_brief, run_close_day, etc.)

**telegram_bot.py** (~2541 lines)
- Owns: 24/7 Telegram bot, message routing, planning conversations
- Reads: Coach State, Coach Focus, lift history, Telegram Log
- Writes: Telegram Log, Coach State (via agents)
- CURRENT_FLOW state machine: 8 intercept types
- Intent classifier: Haiku → 6 categories (ENDSESSION/WORKOUT/HEALTH/PROGRAM/META/GENERAL)

### Cascade Architecture

**cascade_state.py** (~350 lines)
- Owns: State machine per level (LONGTERM→ANNUAL→MONTHLY→WEEKLY→DAILY)
- States: IDLE, GATHERING, REASONING, AWAITING_USER, COMMITTING, LOCKED
- Writes: `CASCADE_STATE`, `SNAPSHOT_LOG` domains
- Snapshot debounce: 15 minutes

**cascade_levels.py** (~2000 lines)
- Owns: bottom-up closing operations
- `close_day()` → `DAILY_SUMMARIES` (max_keep=10) — Haiku LLM
- `weekly_eval()` → `WEEKLY_SUMMARIES` (max_keep=52) — Haiku LLM
- `monthly_eval()` → `MONTHLY_SUMMARIES` (max_keep=24) — Sonnet LLM
- `annual_eval()` → `ANNUAL_SUMMARY` — Sonnet LLM
- `longterm_eval()` → `LONGTERM_PLAN` — Sonnet LLM
- Python-side escalation check (deterministic, no LLM): injury keywords → ANNUAL, goal_change → LONGTERM, 3+ skips → MONTHLY

### Memory & Storage

**memory.py** (~1200 lines)
- Owns: all Coach Memory Google Sheet R/W operations
- 50+ domain keys stored as JSON blobs in `Coach State` tab
- Key read functions: `read_coach_state()`, `read_lift_history()`, `read_health_log()`, `read_telegram_log()`
- Key write functions: `upsert_coach_state()`, `append_summary()`, `write_single_summary()`

**sheet_sync.py** (~400 lines)
- Owns: delta detection between sheet state and stored watermark
- Writes: `SHEET_SYNC` watermark domain
- Non-fatal (wrapped in try/except), runs at start of every pipeline call

### Specialized Agents

**program_agent.py** (~760 lines)
- Decision: REJECT / MODIFY_CURRENT / CREATE_NEW
- Uses extended thinking (Opus, 16K token budget)
- Guards: final 3 weeks of block → REJECT; tonnage limit check

**workout_agent.py** (~135 lines)
- Real-time workout adaptation (Sonnet)
- Triggered by: workout/session/tired/substitute keywords

**health_agent.py** (~300 lines)
- Health/recovery/nutrition specialist (Haiku or Sonnet)
- Read-only (no state writes)

### Analytics (Pure Python, No LLM)

**health_science.py** (~800 lines)
- `compute_daily_readiness()` → `HEALTH_READINESS` domain
- `compute_weekly_correlations()` → `HEALTH_INSIGHTS` domain
- Min N=20 observations before surfacing correlations, R²≥5% threshold

**strength_tracker.py** (~1200 lines)
- e1RM (multi-formula blend: Epley, Brzycki, Wathan, Mayhew)
- Stall detection, rep bucket volume, push:pull balance
- Writes: `STRENGTH_PROJECTIONS` domain

**projections.py** (~600 lines)
- Linear regression on 1RM trends, forward projection to end date
- Read-only (no Coach State writes)

---

## 4. Memory Domains (Complete List)

All domains stored as `[Domain, Summary (JSON), Confidence, Last Updated]` in Coach Memory Sheet.

### Summary-list domains (append, max_keep)
| Domain | max_keep | Writer |
|---|---|---|
| `DAILY_SUMMARIES` | 10 | cascade_levels.close_day() |
| `WEEKLY_SUMMARIES` | 52 | cascade_levels.weekly_eval() |
| `MONTHLY_SUMMARIES` | 24 | cascade_levels.monthly_eval() |
| `PENDING_FLAGS` | 20 | cascade_levels.close_day() harvest |

### Scalar domains (write_single_summary)
`ANNUAL_SUMMARY` | `LONGTERM_PLAN` | `GOLDEN_RULES` | `ANNUAL_ARC`

### Planning conversation threads
`DAILY_PLAN_THREAD` | `WEEKLY_PLAN_THREAD` | `MONTHLY_PLAN_THREAD` | `ANNUAL_PLAN_THREAD`

### Intent/focus
`WEEKLY_INTENT` | `MONTHLY_INTENT` | `DAILY_FOCUS` | `WEEKLY_SCHEDULE`

### Health & analytics
`HEALTH_READINESS` | `HEALTH_INSIGHTS` | `GARMIN_SUMMARY` | `STRENGTH_PROJECTIONS` | `CARDIO_ZONES`

### State machine
`CASCADE_STATE` | `SNAPSHOT_LOG` | `ACTIVE_THREADS` | `CURRENT_FLOW`

### Athlete model
`ATHLETE_MODEL` | `BEHAVIOR_PATTERNS` | `ATHLETE_DREAMS` | `COACHING_REASON` | `ITERATION_ZERO`

### Timestamp guards (string, not JSON)
`LAST_BRIEF` | `LAST_BRIEF_CONTENT` | `LAST_EMAIL` | `LAST_EVENING_PROTOCOL` | `LAST_POST_SESSION` | `LAST_PROACTIVE` | `LAST_HEALTH_PROACTIVE` | `LAST_SCHEDULE_DISCOVERY` | `LAST_CHALLENGE` | `LAST_NUDGE` | `LAST_STEER_CO`

### Program
`PROGRAM` | `PROGRAM_TERMINAL` | `PROGRAM_TERMINAL_WRITTEN` | `ACTIVE_PROGRAM_SHEET_ID` | `LAST_PROGRAM_SNAPSHOT`

### Other
`STEER_CO_DRAFT` | `TELEGRAM_HISTORY` | `GARMIN_SUMMARY` | `SHEET_SYNC` | `LAST_STEER_CO`

---

## 5. Telegram Message Routing (Exact Chain)

```
handle_message()
  ├── Authorization check (_is_authorized)
  ├── Log IN to Telegram Log
  ├── Spawn _process_incoming_message_background() [non-blocking]
  ├── SKIP_UNTIL check (pause/resume emails)
  ├── _handle_confirmation() — yes/no for pending proposals
  ├── Typing indicator
  ├── Iteration Zero intercept (if ITERATION_ZERO.status = IN_PROGRESS/COVERAGE_TESTING)
  │
  ├── [BUG-01 FIX] coach_state = read_coach_state()  ← ADDED HERE
  ├── [BUG-01 FIX] _cf_raw = coach_state.get("CURRENT_FLOW", ...)  ← ADDED HERE
  │
  ├── CURRENT_FLOW intercepts (ordered):
  │   ├── weekly_planning → multi-turn Sonnet weekly planning
  │   ├── daily_planning → multi-turn Sonnet daily planning (deload check at line 1754)
  │   ├── monthly_planning → multi-turn Sonnet monthly planning
  │   ├── annual_planning → multi-turn Sonnet annual planning
  │   ├── endsession → RPE collection thread
  │   └── weekly_confirm → legacy confirm handler
  │
  ├── On-demand planning triggers ("plan week", "lets plan", ...)
  │
  ├── Haiku intent classifier → ENDSESSION | WORKOUT | HEALTH | PROGRAM | META | GENERAL
  │
  └── Route to:
      ├── ENDSESSION → run_endsession_protocol()
      ├── WORKOUT → WorkoutAdvisorAgent (Sonnet + sheet load)
      ├── HEALTH → health_agent (Haiku or Sonnet)
      ├── PROGRAM → program_agent (extended thinking)
      ├── META → run_meta_improvement()
      └── GENERAL → _generate_response_with_tools() (Sonnet)
```

---

## 6. What Is Currently Working

| Feature | Status | Location |
|---|---|---|
| Evening protocol → daily_planning Telegram flow | ✅ Live | run_coach.py + telegram_bot.py |
| Weekly/monthly/annual planning conversations | ✅ Live | telegram_bot.py |
| close_day() + weekly_eval() + monthly_eval() + annual_eval() | ✅ Live | cascade_levels.py |
| Cascade state machine + snapshot/restore | ✅ Live | cascade_state.py |
| PENDING_FLAGS harvest from DAILY_FOCUS | ✅ Live | cascade_levels.py |
| Deload coherence check in daily_planning | ✅ Live | telegram_bot.py:1754 |
| Morning brief reads DAILY_FOCUS | ✅ Live | run_coach.py |
| Health science correlation engine (pure Python) | ✅ Live | health_science.py |
| Sheet delta sync (watermark-based) | ✅ Live | sheet_sync.py |
| 352+ unit tests passing | ✅ Live | tests/test_core_logic.py |
| Garmin integration | ⚠️ Partial | garmin.py exists; run_sync_garmin() not wired in pipeline |
| _check_golden_rules() | ❌ Planned | cascade_levels.py — stub only |
| CONDITIONAL_CHECKINS carry-forward | ❌ Not built | — |
| "2x confirm before folding" mechanism | ❌ Not built | — |

---

## 7. Bug Log

### BUG-01 [CRITICAL — FIXED 2026-03-23]
**File:** `src/telegram_bot.py`
**Lines affected:** 1612–1946 (before fix)
**Description:** `coach_state` and `_cf_raw` were used starting at line 1614 (CURRENT_FLOW intercept checks) but initialized at line 1946 (`_cf_raw = coach_state.get("CURRENT_FLOW", ...)`). Any message received while any CURRENT_FLOW state was active caused an immediate `NameError`, silently dropping the athlete's message.
**Fix:** Added initialization of `coach_state` and `_cf_raw` immediately after the iteration_zero block (around line 1611), before any CURRENT_FLOW checks.
**Regression test:** `tests/test_core_logic.py::TestBug01CoachStateInit` (2 tests)

### BUG-02 [MEDIUM — Phase 2]
**File:** `src/telegram_bot.py`
**Description:** No `DAILY_FOCUS` guard before intraday routing. If `CURRENT_FLOW` is empty and no daily planning conversation has occurred, the system routes workout questions to `WorkoutAdvisorAgent` without checking whether a day plan was confirmed for today.
**Risk:** Confusing UX — athlete gets workout advice before agreeing on today's plan.
**Fix:** Phase 2 — add DAILY_FOCUS existence check in WORKOUT intent routing.

### BUG-03 [LOW — Phase 3]
**File:** `src/telegram_bot.py`
**Description:** Haiku intent classifier has 6 categories but no `QUESTION` category. Simple data queries ("how much did I squat last week?") route to `WORKOUT` → `WorkoutAdvisorAgent` (Sonnet + sheet load) instead of a cheap targeted data lookup.
**Risk:** Efficiency waste, minor UX confusion (training advice framing for data queries).
**Fix:** Phase 3 — add QUESTION category to Haiku classifier.

---

## 8. Architecture Validation Checklist

### State Integrity
| Check | Status |
|---|---|
| system_state updated atomically | ✅ `upsert_coach_state` is single-row write |
| DAILY_FOCUS exists before intraday mode | ❌ No guard — intraday routes regardless |
| pending_resolutions never silently removed | ✅ Commands tab with Applied=Y/DECLINED |
| e1RM never calculated from incomplete set data | ✅ `projections.py` guards None values |
| thursday_skip_count persists across weeks | ⚠️ LLM-only pattern detection, no code counter |

### Escalation Guards
| Check | Status |
|---|---|
| No plan modified before user confirms | ✅ AWAITING_USER + LOCKED chain in cascade_state.py |
| Escalation never skips levels | ✅ `initiate_escalation()` router table |
| No escalation for simple queries | ⚠️ No QUESTION category — simple queries route to WORKOUT |
| Agent pushes back on golden rule conflicts | ❌ `_check_golden_rules()` NOT BUILT |
| Agent requires 2+ confirms before accepting override | ❌ No 2x-confirm mechanism |

### Context Isolation
| Check | Status |
|---|---|
| morning_brief does NOT include pending_resolutions | ✅ `run_brief()` reads DAILY_FOCUS only |
| close_session prompt does NOT include next-day plan | ✅ `close_day()` prompt is backward-looking |
| Classification is separate LLM call | ✅ `_classify_intent()` is separate Haiku call |
| LLM never receives raw history directly | ✅ `cascade_levels` reads summaries only |

### Daily Flow Completeness
| Check | Status |
|---|---|
| No intraday processing without confirmed day_plan | ❌ No gate — BUG-02 |
| close_session only after day_plan_confirmed | ⚠️ `run_endsession_protocol()` doesn't check DAILY_FOCUS |
| close_day always runs (rest days, skipped days) | ✅ `close_day()` handles all cases |
| Missing morning check-in detected + handled | ❌ No detection logic |

### Pattern Detection
| Check | Status |
|---|---|
| Thursday skip pattern persists across weeks | ⚠️ LLM-only, may miss after >8 DAILY_SUMMARIES |
| Elbow flags accumulate to monthly escalation | ⚠️ Only if weekly_eval writes to `escalations` field |
| Annual layer notified of recurring patterns | ✅ `to_annual` field exists in monthly schema |

---

## 9. Migration Plan

| Component | Exists? | Action | Risk | Phase |
|---|---|---|---|---|
| Fix BUG-01: `coach_state/_cf_raw` init | ✅ **FIXED** | Done | — | 0 |
| `/docs/AUDIT_REPORT.md` | ✅ **DONE** | Done | — | 0 |
| `/tests/simulate/` framework | ❌ | Create engine, mock_memory, mock_llm, runner | NONE | 1 |
| `/tests/fixtures/` (10 fixtures) | ❌ | Create all 10 JSONs | NONE | 1 |
| `_check_golden_rules()` | ❌ Stub | Build constitutional check before cascade commits | LOW | 2 |
| DAILY_FOCUS guard for intraday routing | ❌ | Add in telegram_bot.py WORKOUT branch | LOW | 2 |
| Catch-up flow for missed morning check-in | ❌ | Add branch in handle_message() | LOW | 2 |
| "2x confirm before override" mechanism | ❌ | Add GOLDEN_RULES.override_attempts counter | MEDIUM | 2 |
| thursday_skip_count code counter | ❌ | Add deterministic counter in PENDING_FLAGS | LOW | 3 |
| CONDITIONAL_CHECKINS carry-forward | ❌ | Add field in DAILY_FOCUS; read in brief | LOW | 3 |
| QUESTION intent category | ❌ | Add to Haiku classifier | LOW | 3 |
| Accumulated load index since last deload | ❌ | Computed field in WEEKLY_INTENT | LOW | 4 |
| Garmin wiring | ⚠️ Partial | Wire run_sync_garmin() into schedule | LOW | 4 |
| RPE write-back via /endsession | ⚠️ Partial | Wire _apply_rpe_log() after RPE collection | LOW | 4 |
| Bootstrap trap (max_keep=52) | ✅ Fixed | Verify only | NONE | verify |

### Expected pytest results after Phase 1 simulation framework:
```
fixture_01_normal_day        → PASS
fixture_02_session_skip      → FAIL  (pre-confirm guard missing)
fixture_03_elbow_pain        → FAIL  (escalation chain gaps)
fixture_04_program_change    → FAIL  (cardio proposal leaks to brief)
fixture_05_escalation_block  → FAIL  (_check_golden_rules not built)
fixture_06_false_escalation  → PASS  (no correctness failure)
fixture_07_weekly_close      → PASS  (LLM mock carries pattern)
fixture_08_monthly_close     → FAIL  (cross-week elbow pattern propagation)
fixture_09_annual_arc        → PASS  (annual_eval doesn't call writeback)
fixture_10_no_plan_guard     → FAIL  (BUG-02 guard missing)
```
