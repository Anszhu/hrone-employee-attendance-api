# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, attendance regularization, and analytics.

## Technology Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 7.0
- Pydantic

## Features

- Employee creation, listing, filtering, and pagination
- Attendance punch-in and punch-out
- Attendance listing with filters and pagination
- Attendance regularization with an audit history
- Employee monthly attendance summaries
- Department attendance summaries
- Late-arrival leaderboard with competition ranking
- Department attendance trends with seven-day moving averages
- Health checks and MongoDB execution-plan diagnostics
- MongoDB indexes and aggregation pipelines

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer; MongoDB 7.0 is recommended

## Setup

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Set these environment variables before starting the application:

- `MONGO_URI`: MongoDB connection URI
- `MONGO_DB`: MongoDB database name

Example for a local MongoDB instance in PowerShell:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance"
```

Use your own connection URI and database name as appropriate. Never commit credentials or secrets.

## Run the API

```bash
uvicorn app.main:app --port 8000
```

Open `/docs` on the running server for interactive API documentation.

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Application/database health |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize attendance |
| GET | `/analytics/employees/{emp_code}/monthly?month=YYYY-MM` | Employee monthly summary |
| GET | `/analytics/departments/summary?month=YYYY-MM` | Department summary |
| GET | `/analytics/leaderboard/late?month=YYYY-MM` | Late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend?from=YYYY-MM-DD&to=YYYY-MM-DD` | Department attendance trend |
| GET | `/admin/explain/{endpoint}` | Query execution-plan diagnostics |

Refer to `openapi.yaml` and `/docs` for request parameters, schemas, and response details.

## Testing

The implementation was tested locally against MongoDB 7.0.43. The reported full regression run completed with 595 tests and one skipped test.

## Repository Contents

- `app/main.py`: API implementation
- `requirements.txt`: Python dependencies
- `openapi.yaml`: API contract
- `DATA_MODEL.md`: data model reference
- `REVIEW.md`: implementation review
- `DECISIONS.md`: design decisions
- `.gitignore`: excludes local artifacts and secrets