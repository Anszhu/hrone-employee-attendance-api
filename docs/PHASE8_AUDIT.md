# Phase 8 audit: final candidate review

Scope: the Phase 7 project (`hrone-employee-attendance-api-main2.zip`) was initially audited against `openapi.yaml`, `DATA_MODEL.md`, `README.md`, `REVIEW.md` and `DECISIONS.md`. The initial audit ran in a Linux sandbox without a MongoDB server. Subsequent local verification was performed on Windows against MongoDB 7.0.43. The original sandbox findings and the later Windows results are distinguished throughout this report.

Evidence labels: **tested** = run and observed passing in the named environment; **inspected** = read, not executed; **not verified** = not covered by the recorded evidence.

## 1. Verdict

No blocker or confirmed code defect was found in the reviewed implementation. `app/main.py` was **not changed** during Phase 8. The initial Linux review could not run real-MongoDB tests or explain plans; subsequent Windows verification ran the full test suite against local MongoDB 7.0.43 and the HTTP smoke test, with results recorded in section 12. The Phase 8 changes are the README updates, `scripts/smoke_test.py`, and this report. Git publication and fresh-clone checks remain to be completed.

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
| `/health` 200 only if ping works, else 503 | `health` | Unreachable-MongoDB 503 verified in the initial sandbox; local smoke test passed health checks against MongoDB | Covered by local smoke test |
| Indexes at startup, idempotent | `INDEXES`, `ensure_indexes`, `lifespan` | Real-MongoDB test suite and live smoke test passed locally; index creation was exercised in the local environment | Startup behavior was exercised by the local run; no claim about every failure mode |
| `MONGO_URI`/`MONGO_DB` from env, real env wins over `.env` | top of `main.py` | INSPECTED; unreachable-URI behaviour VERIFIED BY LIVE LOCAL TEST | none |
| Employee create, unique `emp_code`, 409 | `create_employee` | Tests with fakes and real-MongoDB suite passed; smoke test passed create and duplicate checks | No known gap from the recorded tests |
| Employee list, filter, sort, `total` | `list_employees` | Fake-backed tests, real-MongoDB suite, and HTTP smoke test passed | No known gap from the recorded tests |
| Punch-in: 404/409/422, IST date, overnight, late | `punch_in`, helpers | VERIFIED BY TEST; R1/R2 examples re-checked independently | none |
| Epoch-ms strictness (seconds, float, str, bool, range) | `validate_epoch_ms` | VERIFIED BY TEST + spot check; HTTP 422 for seconds-sized value VERIFIED against a mock-backed server | none |
| R3 overtime, R4 half-up hours, R5 half day | helpers | VERIFIED BY TEST + spot check (incl. DATA_MODEL samples 1, 2, 7) | none |
| Punch-out 404/409/422, 24 h window | `punch_out` | Fake-backed tests, real-MongoDB suite, and HTTP smoke test passed | No separate claim beyond the exercised test scenarios |
| Attendance list, sort, `date_from > date_to` 422 | `list_attendance` | Fake-backed tests, real-MongoDB suite, and HTTP smoke test passed | No known gap from the recorded tests |
| PATCH rules, recompute, one history entry, only changed fields | `plan_regularization` | Tests and HTTP smoke test passed; real-MongoDB suite passed | Results apply to the exercised scenarios |
| Monthly analytics (R5-R8) | `employee_monthly_pipeline` | Emulator tests and local real-MongoDB suite passed; HTTP smoke test passed | No performance claim beyond tested data and queries |
| Department summary (R9, zero-log staff count, avg hours) | `department_summary_pipeline` | Emulator and local real-MongoDB tests passed; tested explain plans showed IXSCAN and no COLLSCAN | Plan result applies to tested query shapes and datasets |
| Leaderboard: competition ranking, limit after ranking, tie order | `late_leaderboard_pipeline` | Emulator tests and local real-MongoDB suite passed; tested explain plan showed IXSCAN and no COLLSCAN | Plan result applies to tested query shapes and datasets |
| Trend: one row per day, headcount R9, rate rules, 7-day window, 404/422 | `department_trend_pipeline` | Emulator tests and local real-MongoDB suite passed; tested explain plans showed IXSCAN and no COLLSCAN | Plan result applies to tested query shapes and datasets |
| Explain: IXSCAN and no COLLSCAN on ~100k records | `admin_explain` | Tested locally; the listed query shapes showed IXSCAN and no COLLSCAN | Grader-scale behavior is not established by these local results |
| Everything lives in `app/main.py` | repo layout | VERIFIED BY INSPECTION (one source file) | none |

## 4. Actual defects

None confirmed. Each point below was inspected and found consistent with the contract rather than assumed:
R1 overnight date rule and its use for late/overtime, whole-second truncation, `$lte` punch-out lookup and its 404/409/422
ordering, PATCH conditional write (state pinned by status, both punch times and history size, 409 on mismatch),
422-before-404 ordering on analytics, orphan-log exclusion in the leaderboard, and `tz_aware=True` handling.

## 5. Missing verification (not defects)

| Item | Status after Windows verification | Remaining action |
|---|---|---|
| Real-MongoDB test suite | Run locally: 648 tests, 1 skipped, 0 failures | Retain test output if needed for submission evidence |
| Analytics explain plans | Tested query shapes reported IXSCAN with no COLLSCAN, including department summary and department trend | Results apply only to tested datasets and query shapes; do not infer grader-scale performance |
| Live server and HTTP smoke test | Smoke test passed 46/46 checks against local MongoDB | Keep using a disposable database for smoke tests |
| Git state, `origin/main` parity, tracked junk | Not established by the test results in this report | Complete section 11 before submission |
| Fresh-clone verification | Not established by the test results in this report | Complete after pushing |

## 6. Documentation issues

| Issue | Severity | Action |
|---|---|---|
| README did not say how to activate the venv on Windows | LOW | Fixed in README |
| README did not consistently state the assignment's MongoDB minimum | LOW | Fixed in README: MongoDB 6.0+ required; local test server was MongoDB 7.0.43 |
| README did not show how to enable the real-MongoDB tests | LOW | Fixed in README |
| `DATA_MODEL.md` says the repo ships no sample data, yet `sample_data/` exists and is used by `tests/test_phase5_2.py` | LOW | Left alone: `DATA_MODEL.md` is the assignment's file. Harmless |
| `REVIEW.md` quotes "608 tests" from an earlier run; today's count is 648 | NO ACTION | It is labelled as reported earlier and not re-run |
| `requirements.txt` uses `>=` with no upper bounds | LOW | Suite passed here on fastapi 0.143, pydantic 2.14, pymongo 4.18, uvicorn 0.54, Python 3.13. Run `pip freeze` on your PC and compare before submitting. No change made (no demonstrated incompatibility) |

`.gitignore` covers `.venv/`, `venv/`, `__pycache__/`, `.env`, `.env.*`, logs, `*.bak`, `*.backup`, `*.before-*`, caches, and has no BOM. No backup files are in the zip.

## 7. Environment and setup risks

- The README specifies MongoDB 6.0+ for the assignment. Local verification used MongoDB 7.0.43. Check the installed version with `mongod --version` or `mongosh --eval "db.version()"`.
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

1. `README.md`: venv activation for PowerShell, consistent MongoDB 6.0+ requirement, `TEST_MONGO_URI` example, smoke-test section.
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
    python -m unittest discover -s tests -t . -v                 # observed: 648 tests, 1 skipped, 0 failures
    # The live server and smoke test were also run locally; see section 12.

The test-suite, smoke-test, and explain-plan outcomes recorded in section 12 were observed on the local Windows environment.
The Git-state and fresh-clone checks in section 11 remain outstanding.

## 11. Final GitHub publication checklist

- [ ] Review `git diff --check`, `git diff --stat`, and the final diff.
- [ ] Confirm `app/main.py` is unchanged unless a separately reviewed code fix is necessary.
- [ ] Confirm `git status` shows no `.env`, `.venv`, caches, test output, or ZIP files staged.
- [ ] Confirm `main` and `origin/main` are in the expected state and inspect tracked files.
- [ ] Commit and push to the existing `main`, without rewriting history.
- [ ] After pushing, clone the repository into a fresh folder, install from `requirements.txt`, start the app, and open `/docs`.

## 12. Local Windows Verification - 2026-10-13

Environment: Windows PowerShell, Python virtual environment, local MongoDB 7.0.43.

### Test results

| Verification | Observed result |
|---|---|
| Full unittest suite with `TEST_MONGO_URI` configured | 648 tests ran; OK; 1 skipped |
| Windows-specific skip | `test_identical_results_in_every_zone` skipped because `time.tzset` is unavailable on Windows |
| HTTP smoke test | 46/46 checks passed |
| `department_summary` explain plan, unfiltered | IXSCAN present; COLLSCAN absent |
| `department_summary` explain plan, filtered | IXSCAN present; COLLSCAN absent |
| `department_trend` explain plan, wide range | IXSCAN present; COLLSCAN absent |
| `department_trend` explain plan, one-day range | IXSCAN present; COLLSCAN absent |
| Other tested analytics explain plans | IXSCAN present; COLLSCAN absent for the tested query shapes |

The explain-plan results apply to the tested datasets and query shapes. They do not establish performance or index selection for every possible query or for the grader's larger dataset.

### Smoke-test database

The HTTP smoke test used the disposable database `attendance_phase8_smoke`. The smoke script writes test data and does not delete it. Do not use the normal application database for smoke testing.

### Final interpretation

The Windows test suite, live HTTP smoke test, and listed explain-plan checks passed in the tested environment. Earlier Linux sandbox results remain useful as a record of that initial review, but they should not be mistaken for the later Windows test results. Git publication checks and fresh-clone verification still need to be completed before submission.
