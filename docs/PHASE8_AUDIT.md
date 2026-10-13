# Phase 8 audit: final candidate review

Scope: the Phase 7 project (`hrone-employee-attendance-api-main2.zip`) audited against `openapi.yaml`, `DATA_MODEL.md`, `README.md`,
`REVIEW.md` and `DECISIONS.md`. Nothing was committed or pushed. No MongoDB server was available where this audit ran
(Linux sandbox, Python 3.13, no `mongod`, no route to MongoDB downloads), so everything that needs a real server is listed as
**not verified** and has a command in section 10.

Evidence labels: **tested** = run and observed passing here; **inspected** = read, not executed; **not verified** = needs your PC.

## 1. Verdict

No blocker and no confirmed code defect was found. `app/main.py` was **not changed**. The candidate is not yet "final":
the real-MongoDB tests, the explain-plan tests, and the live server plus HTTP smoke test have not been run by anyone in this
review. Two files changed: `README.md` (Windows activation line, MongoDB version floor, `TEST_MONGO_URI` and smoke-test
instructions) and one added file, `scripts/smoke_test.py`. This report is added as `docs/PHASE8_AUDIT.md`.

Not available to this audit, and therefore not invented: the original problem statement document (it is not in the zip), and
the Git metadata (the zip has no `.git`, so `git status`, branch, `origin/main` parity and tracked-file checks must be run by you, section 10).

## 2. Phase 1-7 completion matrix

| Phase | Content | Evidence in this review |
|---|---|---|
| 1 | Helpers, validation, R1-R5 maths | tested (71 tests; plus independent spot checks, section 8) |
| 2 | Employees, punch-in, serialisation | tested with fakes; real-MongoDB class skipped (8) |
| 3 | Punch-out, attendance list | tested with fakes; real-MongoDB skipped (10) |
| 4 | Regularization / history | tested with fakes; real-MongoDB skipped (10) |
| 5.1-5.4 | Monthly, summary, leaderboard, trend | tested with in-Python emulators; real-MongoDB skipped (10/13/11/11) |
| 5.5 | `/admin/explain` | request validation tested; real explain skipped (1) |
| 6 | 503 handler, punch-out 422 fix | tested (36) |
| 7 | Repo hygiene | tested (4) |

## 3. Requirements compliance matrix

Source: `openapi.yaml` (R1-R10 and endpoint text) and `DATA_MODEL.md`. "Fakes" means the repo's in-memory test doubles, not MongoDB.

| Requirement | Where | Status | Gap / action |
|---|---|---|---|
| Routes, methods, params match contract (11 paths) | all | VERIFIED BY LIVE LOCAL TEST (server started, `/openapi.json` listed) | none |
| `/health` 200 only if ping works, else 503 | `health` | VERIFIED BY LIVE LOCAL TEST (503 with MongoDB unreachable); 200 path NOT VERIFIED | run section 10 |
| Indexes at startup, idempotent | `INDEXES`, `ensure_indexes`, `lifespan` | INSPECTED, NOT EXECUTED against a server | startup log on your PC |
| `MONGO_URI`/`MONGO_DB` from env, real env wins over `.env` | top of `main.py` | INSPECTED; unreachable-URI behaviour VERIFIED BY LIVE LOCAL TEST | none |
| Employee create, unique `emp_code`, 409 | `create_employee` | VERIFIED BY TEST (fakes); race safety rests on the unique index: NOT VERIFIED on real MongoDB here | real-MongoDB tests |
| Employee list, filter, sort, `total` | `list_employees` | VERIFIED BY TEST (fakes) | real-MongoDB tests |
| Punch-in: 404/409/422, IST date, overnight, late | `punch_in`, helpers | VERIFIED BY TEST; R1/R2 examples re-checked independently | none |
| Epoch-ms strictness (seconds, float, str, bool, range) | `validate_epoch_ms` | VERIFIED BY TEST + spot check; HTTP 422 for seconds-sized value VERIFIED against a mock-backed server | none |
| R3 overtime, R4 half-up hours, R5 half day | helpers | VERIFIED BY TEST + spot check (incl. DATA_MODEL samples 1, 2, 7) | none |
| Punch-out 404/409/422, 24 h window | `punch_out` | VERIFIED BY TEST (fakes); simultaneous-close race NOT VERIFIED on real MongoDB here | real-MongoDB tests |
| Attendance list, sort, `date_from > date_to` 422 | `list_attendance` | VERIFIED BY TEST (fakes) | real-MongoDB tests |
| PATCH rules, recompute, one history entry, only changed fields | `plan_regularization` | VERIFIED BY TEST; behaviour re-run over HTTP on a mock-backed server (see section 8) | real-MongoDB tests for the concurrency filter |
| Monthly analytics (R5-R8) | `employee_monthly_pipeline` | VERIFIED BY TEST (emulator); real aggregation NOT VERIFIED here | real-MongoDB tests |
| Department summary (R9, zero-log staff count, avg hours) | `department_summary_pipeline` | VERIFIED BY TEST (emulator; my hand-computed scenario also matches it); real aggregation NOT VERIFIED | real-MongoDB tests |
| Leaderboard: competition ranking, limit after ranking, tie order | `late_leaderboard_pipeline` | VERIFIED BY TEST (emulator); real `$setWindowFields` NOT VERIFIED | real-MongoDB tests |
| Trend: one row per day, headcount R9, rate rules, 7-day window, 404/422 | `department_trend_pipeline` | VERIFIED BY TEST (emulator); real `$dateAdd`/`$setWindowFields` NOT VERIFIED | real-MongoDB tests |
| Explain: IXSCAN and no COLLSCAN on ~100k records | `admin_explain` | NOT VERIFIED (needs MongoDB; the grader's dataset is larger than any local one) | section 10 |
| Everything lives in `app/main.py` | repo layout | VERIFIED BY INSPECTION (one source file) | none |

## 4. Actual defects

None confirmed. Each point below was inspected and found consistent with the contract rather than assumed:
R1 overnight date rule and its use for late/overtime, whole-second truncation, `$lte` punch-out lookup and its 404/409/422
ordering, PATCH conditional write (state pinned by status, both punch times and history size, 409 on mismatch),
422-before-404 ordering on analytics, orphan-log exclusion in the leaderboard, and `tz_aware=True` handling.

## 5. Missing verification (not defects)

| Item | Severity | Why it matters |
|---|---|---|
| Real-MongoDB suites (74 tests, skipped here) | HIGH until run | They are the only proof that the aggregations and race-safety behave on a server |
| `department_summary` explain plan: its first stage filters `employees` on `joined_on` plus a `$type`/`$ne` test on `department`; no index leads with `joined_on`, so whether the planner uses the `(department, emp_code)` index is a plan decision I could not observe | MEDIUM | The assignment states the grader rejects COLLSCAN. `REVIEW.md` says explain results were reported for the six trend scenarios only |
| Live server start with a real MongoDB, startup index log | MEDIUM | Not seen by me; only the unreachable-MongoDB start was |
| Git state, `origin/main` parity, tracked junk | MEDIUM | No `.git` in the zip |

## 6. Documentation issues

| Issue | Severity | Action |
|---|---|---|
| README did not say how to activate the venv on Windows | LOW | Fixed in README |
| README did not state the MongoDB minimum (the pipelines need 5.0+) | LOW | Fixed in README |
| README did not show how to enable the real-MongoDB tests | LOW | Fixed in README |
| `DATA_MODEL.md` says the repo ships no sample data, yet `sample_data/` exists and is used by `tests/test_phase5_2.py` | LOW | Left alone: `DATA_MODEL.md` is the assignment's file. Harmless |
| `REVIEW.md` quotes "608 tests" from an earlier run; today's count is 648 | NO ACTION | It is labelled as reported earlier and not re-run |
| `requirements.txt` uses `>=` with no upper bounds | LOW | Suite passed here on fastapi 0.143, pydantic 2.14, pymongo 4.18, uvicorn 0.54, Python 3.13. Run `pip freeze` on your PC and compare before submitting. No change made (no demonstrated incompatibility) |

`.gitignore` covers `.venv/`, `venv/`, `__pycache__/`, `.env`, `.env.*`, logs, `*.bak`, `*.backup`, `*.before-*`, caches, and has no BOM. No backup files are in the zip.

## 7. Environment and setup risks

- MongoDB must be 5.0+ (`$setWindowFields`, `$dateAdd`, `$lookup` with a sub-pipeline). Check with `mongod --version` or `mongosh --eval "db.version()"`.
- `MONGO_URI` unset means PyMongo's default `localhost:27017`; fine for the normal local URI. `MONGO_DB` unset means `attendance_db`, which is the normal application database: never aim the smoke test at it.
- A wrong URI is not a crash: the app starts and `/health` answers 503. Any non-connection index error (for example a unique index over existing duplicate rows) stops startup on purpose.
- Windows MongoDB service name on your PC is `MongoDB`: `Get-Service MongoDB`.

## 8. Test results (exact)

Environment: Linux, Python 3.13.16, fastapi 0.143.0, pydantic 2.14.1, pymongo 4.18.3, uvicorn 0.54.0, python-dotenv 1.2.4, httpx 0.28.1. `TEST_MONGO_URI` unset.

| Command (repo root) | Result |
|---|---|
| `python -m unittest tests.test_phase7` | Ran 4, OK |
| `python -m unittest tests.test_phase6` | Ran 36, OK |
| `python -m unittest discover -s tests -t .` | Ran 648, OK, skipped 74 (before and after the README/script edits) |

All 74 skips carry the reason `set TEST_MONGO_URI to run the real-MongoDB integration tests`. Per file: phase1 71, phase2 73 (8 skipped), phase3 83 (10), phase4 83 (10), phase5_1 69 (10), phase5_2 78 (13), phase5_3 62 (11), phase5_4 76 (11), phase5_5 13 (1), phase6 36, phase7 4. A skip is not a pass.

Other runs:
- Independent spot checks of the contract examples through the real helper functions, 0 failures: R2 (09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45, 09:40:00.900 -> 0), R3 (29m59s -> 0, 30m -> 30, sample 2 -> 40, overnight sample 7 -> 40), R4 (14850 s -> 4.13, samples 1 and 7), R5 (4.49 half, 4.50 not, 16182 s rounds to 4.50 so not half), R1 (overnight 05:59:59 -> previous day, 06:00:00 -> same day, UTC 19:00 -> next IST date), epoch-ms rejection (seconds, 4102444800001, float, string, bool), working days, attendance percentage.
- Live start of `uvicorn app.main:app --port 8000` with an unreachable MongoDB URI (no data written): started, logged "MongoDB unreachable at startup", `/health` 503 `{"detail":"database unavailable"}`, `/docs` 200, `/openapi.json` 200 listing exactly the 11 contract paths, `/employees` 503 with a fixed message. The server was stopped afterwards.
- The smoke script against a server whose database was replaced by `mongomock` (a mock, not MongoDB): the 36 checks that do not need server-side aggregation passed (health, create, list, duplicate, punch-in/out, half-up 9.33, PATCH with exact history entry, no-op 422, 404s, all validation cases). The analytics and explain steps failed there because the mock does not implement those operators; that says nothing about the real server. The expected analytics numbers in the script were hand-derived and matched the repo's own pipeline emulators (summary, leaderboard, trend); monthly was hand-derived only.

## 9. Recommended Phase 8 changes (made)

1. `README.md`: venv activation for PowerShell, MongoDB 5.0+ note, `TEST_MONGO_URI` example, smoke-test section.
2. `scripts/smoke_test.py`: stdlib-only HTTP smoke test, refuses to write without `--db-name` and `--yes`, unique employee codes and department, no deletion.
3. This report.

Optional and not done: pin dependency versions after you capture `pip freeze`; add an `.env.example` (`.gitignore` already allows one).

## 10. Local acceptance checklist (your Windows PC, PowerShell, repo root)

    git status -sb ; git log -1 --oneline ; git remote -v        # clean tree, main == origin/main, expected commit
    git ls-files | Select-String -Pattern "pyc|\.log|\.bak|\.env|backup"    # must print nothing
    Get-Service MongoDB ; mongod --version                       # Running, 5.0+
    python -c "import fastapi, pymongo, pydantic, dotenv, uvicorn; print('imports ok')"
    pip freeze | Select-String -Pattern "fastapi|pymongo|pydantic|dotenv|uvicorn|httpx"
    python -m unittest tests.test_phase7 -v
    $env:TEST_MONGO_URI = "mongodb://localhost:27017"
    python -m unittest discover -s tests -t . -v                 # expect 648 run, 0 failures, 0 or few skips
    Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue   # must be empty before starting
    $env:MONGO_DB = "attendance_smoke"
    uvicorn app.main:app --port 8000                              # keep this terminal visible
    # second terminal, same folder:
    curl.exe -s localhost:8000/health                             # {"status":"ok"}
    python scripts/smoke_test.py --db-name attendance_smoke --yes # expect 0 failures; explain lines are INFO

Pass criteria: no test failures, `/health` ok, smoke script exit code 0. Look at the `INFO` explain lines: any `COLLSCAN=True` for
`department_summary` is the item to investigate first (section 5).

## 11. Final GitHub publication checklist

- [ ] Section 10 passed on your PC, and you read the output yourself.
- [ ] `git diff --stat` shows only `README.md` plus the new `scripts/` and `docs/` files; `app/main.py` unchanged.
- [ ] `git status` shows no `.env`, `.venv`, caches or zips staged.
- [ ] Commit and push to the existing `main`, no history rewrite.
- [ ] After pushing, clone the repository into a fresh folder, install from `requirements.txt`, start the app, and open `/docs`.
