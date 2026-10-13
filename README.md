Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management,
attendance tracking, attendance regularization, and attendance
analytics.

Technology Stack

Python 3.11+

FastAPI

PyMongo

MongoDB 6.0+

Setup

Create a virtual environment, activate it, and install the application
dependencies:

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

For macOS/Linux, activate the environment with:

source .venv/bin/activate

MongoDB 6.0 or newer is required for this assignment. The application
has been tested locally against MongoDB 7.0.43.

Configuration is read from environment variables (a local .env file,
if you create one, is also read; a real environment variable always
wins). Never commit .env.

MONGO_URI: MongoDB connection string. The assignment only requires
that it is read from the environment and never hard-coded; it does
not say the variable is mandatory. If it is unset or empty, the app
does not fail: no URI is written in the source and PyMongo's own
default host (localhost:27017) applies, which suits local
development. Set it explicitly for any other deployment; a wrong or
unreachable URI shows up as /health 503.

MONGO_DB: database name. Defaults to attendance_db.

Start the application from the repository root:

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

Required indexes are created idempotently at startup. If MongoDB is
unreachable at startup, the app still starts and GET /health answers
503 until MongoDB responds and the indexes exist; any other
index-creation error stops startup.

Health and errors

GET /health returns 200 only when MongoDB answers a ping and the
startup indexes exist, otherwise 503.

422: invalid input. 404: unknown employee or no matching record.
409: duplicates and lost concurrent updates.

Any other PyMongo failure not handled by a route returns 503
{"detail": "database unavailable"}; driver messages (which can
contain hosts or credentials) are never returned. Programming errors
are not converted into 503.

POST /attendance/punch-out: 404 for an unknown employee or no
matching punch-in; 422 if punched_at is at or before the relevant
punch-in or more than 24 hours after it (exactly 24 hours is
allowed); 409 if the selected eligible record is already punched
out.

Tests

Install the development and test dependencies:

python -m pip install -r requirements-dev.txt

Run the complete test suite:

python -m unittest discover -s tests -t . -v

The test suite uses httpx for FastAPI's TestClient. pytest is also
included in requirements-dev.txt for developers who prefer pytest.

Tests that use a real MongoDB run only when TEST_MONGO_URI points at a
disposable MongoDB instance. They use their own attendance_test_*
databases, never the application database configured by MONGO_DB.

To run the real-MongoDB tests in PowerShell:

$env:TEST_MONGO_URI = "mongodb://localhost:27017"
python -m unittest discover -s tests -t . -v

Some platform-specific tests may be skipped when the required
operating-system functionality is unavailable. Review the test output to
distinguish skipped tests from passing tests.

Smoke test against a running server

scripts/smoke_test.py (standard library only) drives every endpoint
over HTTP. It writes data and never deletes it, so start the server on a
throw-away database first.

In the first PowerShell terminal:

$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_smoke"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

In a second terminal:

python scripts/smoke_test.py --base http://127.0.0.1:8000 --db-name attendance_smoke --yes

The smoke-test database is separate from your normal application
database. The script writes test data and does not automatically delete
it.

Implemented Features

Employee creation and paginated employee listing

Attendance punch-in and punch-out

Attendance filtering and pagination

Attendance regularization with audit history

Employee monthly attendance analytics

Department attendance summaries

Late-arrival leaderboard with competition ranking

Department trends with seven-day moving averages

Health checks and MongoDB execution-plan diagnostics

Verification

The project was verified locally from a fresh clone on Windows: 648
automated tests ran with 1 skipped and 0 failures; the smoke test
reported 46/46 checks passed. The skipped test requires time.tzset,
which is unavailable on Windows. These results describe that local
environment and do not guarantee identical results on every platform.
Index definitions and the reasons for them are documented in
DECISIONS.md.
