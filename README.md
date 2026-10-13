# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management,
attendance tracking, attendance regularization, and attendance analytics.

## Technology Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+

## Setup

Create a virtual environment (optional), activate it, and install the dependencies:

    python -m venv .venv
    .venv\Scripts\Activate.ps1        # Windows PowerShell   (macOS/Linux: source .venv/bin/activate)
    pip install -r requirements.txt

The aggregation endpoints use `$setWindowFields`, `$dateAdd` and a `$lookup` with a sub-pipeline, so MongoDB 5.0 or newer
is required (6.0+ is what this project was prepared for).

Configuration is read from environment variables (a local `.env` file, if you create one, is also read;
a real environment variable always wins). Never commit `.env`.

- `MONGO_URI`: MongoDB connection string. The assignment only requires that it is read from the environment and never
  hard-coded; it does not say the variable is mandatory. If it is unset (or empty) the app does not fail: no URI is
  written in the source and PyMongo's own default host (`localhost:27017`) applies, which suits local development. Set it
  explicitly for any other deployment; a wrong or unreachable URI shows up as `/health` 503.
- `MONGO_DB`: database name. Defaults to `attendance_db`.

Start the application from the repository root:

    uvicorn app.main:app --port 8000

Required indexes are created idempotently at startup. If MongoDB is unreachable at startup the app still starts and
`GET /health` answers 503 until MongoDB responds and the indexes exist; any other index-creation error stops startup.

## Health and errors

- `GET /health` returns 200 only when MongoDB answers a ping and the startup indexes exist, otherwise 503.
- 422: invalid input. 404: unknown employee or no matching record. 409: duplicates and lost concurrent updates.
- Any other PyMongo failure not handled by a route returns 503 `{"detail": "database unavailable"}`; driver messages
  (which can contain hosts or credentials) are never returned. Programming errors are not converted into 503.
- `POST /attendance/punch-out`: 404 for an unknown employee or no matching punch-in; 422 if `punched_at` is at or before
  the relevant punch-in or more than 24 hours after it (exactly 24 hours is allowed); 409 if the selected eligible record
  is already punched out.

## Tests

    python -m unittest discover -s tests -t . -v

`tests/test_phase6.py` needs no database (it uses in-memory fakes) and needs `httpx` for FastAPI's `TestClient`.
Tests that use a real MongoDB, where present, run only when `TEST_MONGO_URI` points at a disposable MongoDB and use their own
`attendance_test_*` databases, never `MONGO_DB`. Test results are not recorded in this repository.

To include the real-MongoDB tests (PowerShell; they otherwise show up as skipped, which is not a pass):

    $env:TEST_MONGO_URI = "mongodb://localhost:27017"
    python -m unittest discover -s tests -t . -v

## Smoke test against a running server

`scripts/smoke_test.py` (standard library only) drives every endpoint over HTTP. It writes data and never deletes it, so start
the server on a throw-away database first:

    $env:MONGO_DB = "attendance_smoke"
    uvicorn app.main:app --port 8000
    # in a second terminal
    python scripts/smoke_test.py --db-name attendance_smoke --yes

## Implemented Features

- Employee creation and paginated employee listing
- Attendance punch-in and punch-out
- Attendance filtering and pagination
- Attendance regularization with audit history
- Employee monthly attendance analytics
- Department attendance summaries
- Late-arrival leaderboard with competition ranking
- Department trends with seven-day moving averages
- Health checks and MongoDB execution-plan diagnostics

## Verification

No test or explain-plan results are claimed in this README; run the commands above to obtain them. Index definitions
and the reasons for them are in `DECISIONS.md`.
