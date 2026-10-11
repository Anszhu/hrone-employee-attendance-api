# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management,
attendance tracking, attendance regularization, and attendance analytics.

## Technology Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+

## Setup

Install the dependencies:

    pip install -r requirements.txt

Configure the MongoDB connection using environment variables:

- MONGO_URI
- MONGO_DB

Start the application from the repository root:

    uvicorn app.main:app --port 8000

MongoDB indexes are created at application startup.

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

The development regression run completed with 608 tests passing
and one skipped. Explain-plan diagnostics on MongoDB 7.0.43
verified index scans without collection scans in the reported
scenarios. These results do not imply that every query shape
was benchmarked at scale.