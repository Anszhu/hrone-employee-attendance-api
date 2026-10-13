# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, attendance regularization, and attendance analytics.

## Technology Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+

## Setup

Create a virtual environment, activate it, and install the application dependencies.

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

**macOS/Linux**

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

MongoDB 6.0 or newer is required for this assignment. The application has been tested locally against MongoDB 7.0.43.

### Configuration

Configuration is read from environment variables. A local `.env` file, if you create one, is also read; actual environment variables take precedence. Never commit `.env`.

- **`MONGO_URI`**: MongoDB connection string. It must not be hard-coded. If it is unset or empty, PyMongo uses its default host, `localhost:27017`. Set it explicitly for other deployments. An incorrect or unreachable URI results in a `503` response from `/health`.
- **`MONGO_DB`**: Database name. Defaults to `attendance_db`.

### Start the Application

Run the following command from the repository root:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Required indexes are created idempotently at startup. If MongoDB is unreachable at startup, the application can still start, and `GET /health` returns `503` until MongoDB responds and the required indexes exist. Other index-creation errors stop startup.

## Health and Errors

- **`GET /health`**: Returns `200` only when MongoDB responds to a ping and the startup indexes exist. Otherwise, it returns `503`.
- **`422`**: Invalid input.
- **`404`**: Unknown employee or no matching record.
- **`409`**: Duplicate records or lost concurrent updates.
- **`503`**: An unhandled database failure returns `{"detail": "database unavailable"}`. Driver messages that could expose hosts or credentials are not returned. Programming errors are not converted into `503` responses.

### Punch-Out Validation

`POST /attendance/punch-out` follows these rules:

- `404` if the employee is unknown or no matching punch-in exists.
- `422` if `punched_at` is at or before the relevant punch-in, or more than 24 hours after it. Exactly 24 hours is allowed.
- `409` if the selected eligible record has already been punched out.

## Tests

### Install Development Dependencies

Install the application and test dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

The development requirements include `httpx`, which is used by FastAPI's `TestClient`, and `pytest` for developers who prefer pytest.

### Run the Complete Test Suite

```powershell
python -m unittest discover -s tests -t . -v
```

### Run Tests Against MongoDB

Tests that require a real MongoDB instance run only when `TEST_MONGO_URI` points to a disposable MongoDB instance. They use their own `attendance_test_*` databases rather than the application database configured by `MONGO_DB`.

In PowerShell, set the test connection string and run the suite:

```powershell
$env:TEST_MONGO_URI = "mongodb://localhost:27017"
python -m unittest discover -s tests -t . -v
```

Some platform-specific tests may be skipped when the required operating-system functionality is unavailable. Review the test output to distinguish skipped tests from passing tests.

## Smoke Test Against a Running Server

`scripts/smoke_test.py` uses the Python standard library to exercise the API endpoints over HTTP.

**Important:** The smoke test writes data and does not automatically delete it. Use a disposable database.

### Terminal 1 — Start the Server

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_smoke"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Terminal 2 — Run the Smoke Test

```powershell
python scripts/smoke_test.py --base http://127.0.0.1:8000 --db-name attendance_smoke --yes
```

The smoke-test database is separate from your normal application database. The script writes test data and does not automatically delete it.

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

The project was verified locally from a fresh clone on Windows:

- **Automated tests:** 648 tests ran, with 1 skipped and 0 failures.
- **Smoke test:** 46/46 checks passed.
- **Skipped test:** Requires `time.tzset`, which is unavailable on Windows.

These results describe the local verification environment and do not guarantee identical results on every platform.

Index definitions and the reasons for them are documented in `DECISIONS.md`.