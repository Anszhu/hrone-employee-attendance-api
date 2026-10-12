"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env, if present, is loaded for convenience; real environment
      variables take precedence)

Contract: openapi.yaml. Stored data: DATA_MODEL.md. Design decisions: DECISIONS.md. Review notes: REVIEW.md.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from bson import json_util
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()

# tz_aware=True: every datetime PyMongo returns is timezone-aware (UTC). Without it PyMongo
# returns naive datetimes, which are then easy to mistake for local time. Writes are always
# stored as UTC BSON dates.
# MONGO_URI comes from the environment (or .env); no connection string is hard-coded here. If it is
# unset, None makes PyMongo apply its own library default host.
# serverSelectionTimeoutMS: when MongoDB is down, an operation (and so /health) fails after 5 s
# instead of PyMongo's 30 s default.
client = MongoClient(os.getenv("MONGO_URI") or None, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

logger = logging.getLogger("attendance")


# --------------------------------------------------------------------------- #
# Pure helpers: standard library only, no I/O, never reads the machine's local
# time zone. (tests/_loader.py can load just this section when the third-party
# packages are not installed, so keep third-party imports out of it.)
# --------------------------------------------------------------------------- #
# === PURE-HELPERS-BEGIN ===
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)

# EpochMillis range from openapi.yaml. Anything below the minimum "looks like seconds".
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

GRACE_SECONDS = 10 * 60          # R2: late only if STRICTLY more than 10:00 after shift start
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_THRESHOLD = Decimal("4.50")  # R5: rounded work_hours < 4.50

# Patterns from openapi.yaml. [0-9] instead of \d: the contract's \d is ASCII-only (ECMA-262)
# but \d in several regex engines also matches other scripts' digits.
EMP_CODE_PATTERN = r"^EMP[0-9]{4,6}$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HHMM_RE = re.compile(HHMM_PATTERN)

# (keys, options) per collection. 1 / -1 are PyMongo's ASCENDING / DESCENDING.
# Default index names are used on purpose: they are deterministic, so re-running is a no-op.
INDEXES = {
    "employees": [
        # Uniqueness of emp_code is enforced HERE (concurrent creates), plus lookups by emp_code
        # and the emp_code ordering of GET /employees.
        ([("emp_code", 1)], {"unique": True}),
        # GET /employees?department=X sorted by emp_code; also the department -> employees lookup
        # the analytics will need.
        ([("department", 1), ("emp_code", 1)], {}),
    ],
    "attendance_logs": [
        # At most one record per (emp_code, date): makes concurrent punch-ins race-safe, addresses
        # a record for PATCH, serves per-employee date ranges and the emp_code-filtered list
        # (date descending is a backward scan of this index).
        ([("emp_code", 1), ("date", 1)], {"unique": True}),
        # GET /attendance default order (date desc, emp_code asc) with or without a date range.
        ([("date", -1), ("emp_code", 1)], {}),
        # GET /attendance?status=X with the same ordering.
        ([("status", 1), ("date", -1), ("emp_code", 1)], {}),
        # Punch-out: "most recent record of this employee whose punch_in <= punched_at".
        ([("emp_code", 1), ("punch_in", -1)], {}),
    ],
}


def ensure_indexes(database) -> list:
    """Create every index in INDEXES. Safe to run on every start: PyMongo's create_index is a
    no-op when an index with the same keys and options already exists. A failure (for example a
    unique index over pre-existing duplicate rows) is raised, not swallowed."""
    created = []
    for collection_name, specs in INDEXES.items():
        for keys, options in specs:
            created.append(database[collection_name].create_index(keys, **options))
    return created


# ---- strict validation primitives ----
def validate_epoch_ms(value):
    """Strict EpochMillis: a real int (not bool, float, str, None) inside the contract range."""
    if type(value) is not int:  # `type(...) is`, because bool is a subclass of int
        raise ValueError("must be an integer number of epoch milliseconds")
    if value < EPOCH_MS_MIN:
        raise ValueError(
            f"must be at least {EPOCH_MS_MIN} epoch milliseconds (smaller values look like seconds)"
        )
    if value > EPOCH_MS_MAX:
        raise ValueError(f"must be at most {EPOCH_MS_MAX} epoch milliseconds")
    return value


def parse_calendar_date(value) -> date:
    """Strict YYYY-MM-DD that is also a real calendar date (no 2026-02-30, no 20260706)."""
    if type(value) is not str or not _DATE_RE.fullmatch(value):
        raise ValueError("must be a calendar date in YYYY-MM-DD format")
    try:
        return date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise ValueError("is not a real calendar date") from None


def validate_calendar_date(value) -> str:
    parse_calendar_date(value)
    return value


# ---- instants: always timezone-aware, always explicit ----
def _as_utc(dt: datetime) -> datetime:
    """A naive datetime is interpreted as UTC (that is what MongoDB stores and what PyMongo
    returns without tz_aware) - never as machine-local time."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def truncate_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation. Returns aware UTC."""
    return _as_utc(dt).replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime (exact, integer arithmetic)."""
    if type(ms) is not int:
        raise TypeError("epoch milliseconds must be an int")
    return EPOCH_UTC + timedelta(milliseconds=ms)


def instant_from_epoch_ms(ms: int) -> datetime:
    """The value to store for a client-supplied instant: aware UTC, truncated to the second."""
    return truncate_seconds(epoch_ms_to_datetime(ms))


def datetime_to_epoch_ms(dt: datetime) -> int:
    """Aware (or naive-as-UTC) datetime -> epoch milliseconds, exactly. Never uses dt.timestamp(),
    which would consult the local time zone for naive values."""
    return (_as_utc(dt) - EPOCH_UTC) // timedelta(milliseconds=1)


def to_ist(dt: datetime) -> datetime:
    return _as_utc(dt).astimezone(IST)


def ist_date_str(dt: datetime) -> str:
    """The IST calendar date (YYYY-MM-DD) of an instant."""
    return to_ist(dt).date().isoformat()


# ---- shifts ----
def parse_hhmm(value: str) -> tuple:
    if type(value) is not str or not _HHMM_RE.fullmatch(value):
        raise ValueError("must be a 24-hour HH:MM time")
    return int(value[:2]), int(value[3:5])


def _minutes_of_day(hhmm: str) -> int:
    hours, minutes = parse_hhmm(hhmm)
    return hours * 60 + minutes


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift ends on the next calendar day."""
    return _minutes_of_day(shift_end) <= _minutes_of_day(shift_start)


def _ist_at(date_str: str, hhmm: str, day_offset: int = 0) -> datetime:
    d = parse_calendar_date(date_str)
    hours, minutes = parse_hhmm(hhmm)
    return datetime(d.year, d.month, d.day, hours, minutes, tzinfo=IST) + timedelta(days=day_offset)


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    """The instant the shift of attendance day `date_str` starts (IST wall clock)."""
    return _ist_at(date_str, shift_start)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The instant the shift of attendance day `date_str` ends (next day for overnight shifts)."""
    return _ist_at(date_str, shift_end, 1 if is_overnight_shift(shift_start, shift_end) else 0)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the (second-truncated) punch-in. Overnight shifts: a punch-in
    whose IST time of day is earlier than shift_end belongs to the shift that started yesterday."""
    t = to_ist(truncate_seconds(punch_in))
    day = t.date()
    if is_overnight_shift(shift_start, shift_end):
        seconds_into_day = t.hour * 3600 + t.minute * 60 + t.second
        if seconds_into_day < _minutes_of_day(shift_end) * 60:
            day -= timedelta(days=1)
    return day.isoformat()


# ---- derived fields (R2 - R5): integer arithmetic only ----
def _whole_seconds(delta: timedelta) -> int:
    # Callers pass differences of second-truncated instants, so microseconds are always 0.
    return delta.days * 86400 + delta.seconds


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2. `date_str` is the record's attendance date, so an overnight shift is measured from the
    shift_start of the day the shift began. 09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45."""
    start = shift_start_at(date_str, shift_start)
    late_seconds = _whole_seconds(truncate_seconds(punch_in) - start)
    return late_seconds // 60 if late_seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals HALF-UP, done on integers. hundredths =
    floor(seconds * 100 / 3600 + 1/2) = (seconds * 100 + 1800) // 3600. Python's round() and
    float division are never involved in the rounding decision."""
    seconds = _whole_seconds(truncate_seconds(punch_out) - truncate_seconds(punch_in))
    if seconds < 0:
        raise ValueError("punch_out is before punch_in")
    hundredths = (seconds * 100 + 1800) // 3600
    return hundredths / 100


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes (floor) after the shift end, counted only if >= 30, else 0. The shift end
    falls on the day after `date_str` for overnight shifts."""
    end = shift_end_at(date_str, shift_start, shift_end)
    over_seconds = _whole_seconds(truncate_seconds(punch_out) - end)
    minutes = over_seconds // 60 if over_seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: true when the ROUNDED work_hours (as produced by compute_work_hours) is < 4.50."""
    if work_hours is None:
        return False
    return Decimal(repr(work_hours)) < HALF_DAY_THRESHOLD


# ---- request / response mapping (Phase 2) ----
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")


def _ms_or_none(value):
    return None if value is None else datetime_to_epoch_ms(value)


def _present(doc: dict, key: str, default):
    """Legacy documents may omit a field (or hold null): read it as `default`."""
    value = doc.get(key)
    return default if value is None else value


def page_skip(page: int, page_size: int) -> int:
    """R10: page starts at 1."""
    return (page - 1) * page_size


def employee_list_filter(department: Optional[str]) -> dict:
    """Exact, case-sensitive department match. Only an omitted parameter means 'all'."""
    return {} if department is None else {"department": department}


def build_employee_doc(data: dict, now: datetime) -> dict:
    """The document to insert. created_at is server-generated and cut to whole milliseconds (what
    BSON stores), so the value returned equals the value stored."""
    doc = dict(data)
    doc["created_at"] = _as_utc(now).replace(microsecond=now.microsecond // 1000 * 1000)
    return doc


def serialize_employee(doc: dict) -> dict:
    """Contract `Employee`: only the documented fields; _id never; created_at in epoch ms."""
    out = {field: doc.get(field) for field in EMPLOYEE_FIELDS}
    out["created_at"] = _ms_or_none(doc.get("created_at"))
    return out


def build_attendance_doc(emp: dict, emp_code: str, status: str, ts: datetime) -> dict:
    """A new punch-in record (DATA_MODEL defaults). `ts` must already be second-truncated."""
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    return {
        "emp_code": emp_code,
        "date": day,
        "status": status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }


def _serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if isinstance(change, dict) and field in ("punch_in", "punch_out"):
            change = {"from": _ms_or_none(change.get("from")), "to": _ms_or_none(change.get("to"))}
        changes[field] = change
    return {
        "at": _ms_or_none(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    """Contract `AttendanceRecord`: exactly these keys (no _id, no id); instants in epoch ms;
    legacy documents read with the documented defaults (late/overtime 0, half_day false, history [])."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": _present(doc, "late_minutes", 0),
        "overtime_minutes": _present(doc, "overtime_minutes", 0),
        "half_day": _present(doc, "half_day", False),
        "history": [_serialize_history_entry(e) for e in _present(doc, "history", [])],
    }


# ---- punch-out and attendance listing (Phase 3) ----
MAX_WORK_SECONDS = 24 * 3600     # punch-out may be at most 24 hours after punch-in (exactly 24 h is allowed)


class RuleError(Exception):
    """A business-rule failure that carries the HTTP status the API must answer with."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def build_punch_out_update(punch_in: datetime, record_date: str, shift_start: str, shift_end: str,
                           punch_out: datetime) -> dict:
    """Validate the punch-out window and compute the derived fields (R3 - R5).
    422 if punch_out <= punch_in or more than 24 h after it. Instants are second-truncated first."""
    pi, po = truncate_seconds(punch_in), truncate_seconds(punch_out)
    seconds = _whole_seconds(po - pi)
    if seconds <= 0:
        raise RuleError(422, "punched_at must be after punch_in")
    if seconds > MAX_WORK_SECONDS:
        raise RuleError(422, "punched_at must be within 24 hours of punch_in")
    work_hours = compute_work_hours(pi, po)
    return {
        "punch_out": po,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(po, shift_start, shift_end, record_date),
        "half_day": compute_half_day(work_hours),
    }


def attendance_list_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                           status: Optional[str]) -> dict:
    """GET /attendance filter. Dates are YYYY-MM-DD strings, so string comparison is chronological;
    both bounds are inclusive."""
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None:
            bounds["$gte"] = date_from
        if date_to is not None:
            bounds["$lte"] = date_to
        q["date"] = bounds
    if status is not None:
        q["status"] = status
    return q


def validate_date_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("date_from must not be after date_to")
# ---- regularization / PATCH (Phase 4) ----
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ABSENCE_STATUSES = ("ABSENT", "LEAVE")
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def check_regularization_request(status, punch_in_given: bool, punch_out_given: bool) -> None:
    """ABSENT / LEAVE clear both punch times, so supplying either with them is a 422."""
    if status in ABSENCE_STATUSES and (punch_in_given or punch_out_given):
        raise RuleError(422, "punch_in and punch_out cannot be supplied with status ABSENT or LEAVE")


def _utc_or_none(value):
    return None if value is None else _as_utc(value)


def _truncate_millis(dt: datetime) -> datetime:
    """Cut to whole milliseconds, which is what a BSON date stores."""
    dt = _as_utc(dt)
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)


def plan_regularization(record: dict, shift_start: str, shift_end: str, status=None,
                        punch_in: Optional[datetime] = None, punch_out: Optional[datetime] = None) -> dict:
    """Apply a regularization to the stored `record` and return ONLY the fields that actually change, as
    {field: {"from": old, "to": new}} (instants as aware UTC datetimes, the BSON-side representation).

    Omitted status / punch_in / punch_out keep their stored values. ABSENT / LEAVE clear both punch times and
    zero the derived fields. A presence status needs a punch_in; a supplied punch_in must belong to the
    record's attendance date (R1); a punch_out must be after punch_in and at most 24 h later. Late, work
    hours, overtime and half day are always recomputed from the final times (R2 - R5). Raises RuleError(422)
    for every rule violation, and for a request that changes none of status / punch_in / punch_out."""
    check_regularization_request(status, punch_in is not None, punch_out is not None)
    old = {
        "status": record["status"],
        "punch_in": _utc_or_none(record.get("punch_in")),
        "punch_out": _utc_or_none(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": _present(record, "late_minutes", 0),
        "overtime_minutes": _present(record, "overtime_minutes", 0),
        "half_day": _present(record, "half_day", False),
    }
    target = status if status is not None else old["status"]
    if target in ABSENCE_STATUSES:
        if punch_in is not None or punch_out is not None:   # stored status ABSENT/LEAVE, status omitted
            raise RuleError(422, "punch_in and punch_out cannot be supplied with status ABSENT or LEAVE")
        new = {"status": target, "punch_in": None, "punch_out": None, "work_hours": None,
               "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        pi = truncate_seconds(punch_in) if punch_in is not None else old["punch_in"]
        if pi is None:
            raise RuleError(422, "a presence status requires a punch_in")
        if punch_in is not None and attendance_date(pi, shift_start, shift_end) != record["date"]:
            raise RuleError(422, "punch_in must fall on the record's attendance date")
        po = truncate_seconds(punch_out) if punch_out is not None else old["punch_out"]
        if po is not None:
            derived = build_punch_out_update(pi, record["date"], shift_start, shift_end, po)  # 422 on window
            work_hours, overtime, half_day = derived["work_hours"], derived["overtime_minutes"], derived["half_day"]
            po = derived["punch_out"]
        else:
            work_hours, overtime, half_day = None, 0, False
        new = {"status": target, "punch_in": pi, "punch_out": po, "work_hours": work_hours,
               "late_minutes": compute_late_minutes(pi, shift_start, record["date"]),
               "overtime_minutes": overtime, "half_day": half_day}
    if all(new[f] == old[f] for f in ("status", "punch_in", "punch_out")):
        raise RuleError(422, "the request changes nothing")
    return {f: {"from": old[f], "to": new[f]} for f in CHANGE_FIELDS if new[f] != old[f]}


def build_history_entry(now: datetime, by: str, reason: str, changes: dict) -> dict:
    """One audit entry. `at` is server-generated (never from the client), a UTC datetime cut to the
    millisecond BSON stores."""
    return {"at": _truncate_millis(now), "by": by, "reason": reason, "changes": changes}


def build_regularization_write(record: dict, changes: dict, entry: dict):
    """(filter, update) for ONE atomic conditional write. The filter pins the exact state the plan was
    computed from: status, both punch times and the history length. If anything - another PATCH, a
    punch-out - changed the record in between, nothing matches and the writer must answer 409 instead of
    overwriting. The history entry is appended in the same update as the changed fields."""
    history = record.get("history")
    flt = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),     # None also matches a missing field
        "punch_out": record.get("punch_out"),
        "history": {"$size": len(history)} if isinstance(history, list) else None,
    }
    sets = {field: change["to"] for field, change in changes.items()}
    if isinstance(history, list):
        return flt, {"$set": sets, "$push": {"history": entry}}
    sets["history"] = [entry]                   # legacy: history missing (or null) -> start the trail
    return flt, {"$set": sets}
# ---- employee monthly summary (Phase 5.1) ----
# openapi.yaml `Month`: ^\d{4}-(0[1-9]|1[0-2])$ ; [0-9] keeps it ASCII-only like the other patterns.
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
_MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")


def parse_month(value) -> tuple:
    """'YYYY-MM' -> (year, month). Rejects wrong shapes and year 0000 (the pattern allows it, but there
    is no calendar date in year 0)."""
    if type(value) is not str or not _MONTH_RE.fullmatch(value):
        raise ValueError("must be a calendar month in YYYY-MM format")
    year, month = int(value[:4]), int(value[5:7])
    if year < 1:
        raise ValueError("year must be at least 0001")
    return year, month


def month_bounds(year: int, month: int) -> tuple:
    """First and last calendar day of the month as 'YYYY-MM-DD' strings, the representation the
    attendance `date` field is stored in, so string comparison is chronological."""
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def weekend_dates(year: int, month: int) -> list:
    """Every Saturday and Sunday of the month as 'YYYY-MM-DD' (a date is a calendar day, no time zone)."""
    last = calendar.monthrange(year, month)[1]
    return [f"{year:04d}-{month:02d}-{d:02d}" for d in range(1, last + 1)
            if date(year, month, d).weekday() >= 5]


def count_working_days(joined_on: str, year: int, month: int) -> int:
    """R7: Monday-Friday, no holidays, from max(joined_on, first of month) to the end of the month,
    inclusive. 0 when joined_on is after the month ends."""
    joined = parse_calendar_date(joined_on)
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    start = max(first, joined)
    if start > last:
        return 0
    return sum(1 for offset in range((last - start).days + 1)
               if (start + timedelta(days=offset)).weekday() < 5)


def attendance_pct_half_up(present_halves: int, working_days: int):
    """present_days / working_days * 100 to 2 decimals, HALF-UP, in exact integer arithmetic.
    present_days = present_halves / 2, so the percentage is present_halves * 50 / working_days and the
    hundredths are floor(present_halves * 5000 / working_days + 1/2) = (present_halves * 10000 + wd) // (2 * wd).
    Neither round() nor MongoDB's half-to-even $round is involved. None when there are no working days."""
    if working_days == 0:
        return None
    return ((present_halves * 10000 + working_days) // (2 * working_days)) / 100


def employee_monthly_pipeline(emp_code: str, year: int, month: int) -> list:
    """The aggregation behind GET /analytics/employees/{emp_code}/monthly (reused as-is by the explain
    endpoint later). Stage 1 is the only access path: equality on emp_code plus a range on date, which the
    unique (emp_code, date) index serves. Stage 2 totals the month in one pass:
      present_halves  2 per presence record on Mon-Fri, 1 for a half day, 0 otherwise (integers, so no float)
      leave_days      every LEAVE record (weekends included)
      late_count / total_late_minutes / total_overtime_minutes   stored values, any status, any weekday
    Missing or null late_minutes / overtime_minutes / half_day read as 0 / 0 / false ($ifNull).
    No $round anywhere: percentage rounding happens in attendance_pct_half_up. An empty month yields NO
    output row ($group over zero documents), which build_employee_monthly turns into zeros."""
    first, last = month_bounds(year, month)
    late = {"$ifNull": ["$late_minutes", 0]}
    overtime = {"$ifNull": ["$overtime_minutes", 0]}
    weekday_presence = {"$and": [
        {"$in": ["$status", list(PRESENCE_STATUSES)]},
        {"$not": [{"$in": ["$date", weekend_dates(year, month)]}]},
    ]}
    half_day = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_halves": {"$sum": {"$cond": [weekday_presence, {"$cond": [half_day, 1, 2]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "total_overtime_minutes": {"$sum": overtime},
        }},
    ]


def build_employee_monthly(emp_code: str, month: str, working_days: int, row: Optional[dict]) -> dict:
    """Contract `EmployeeMonthly`. `row` is the single $group output, or None/{} for a month with no
    records (a valid all-zero summary, never a 404)."""
    row = row or {}
    halves = int(row.get("present_halves") or 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(row.get("leave_days") or 0),
        "late_count": int(row.get("late_count") or 0),
        "total_late_minutes": int(row.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes") or 0),
        "attendance_pct": attendance_pct_half_up(halves, working_days),
    }
# ---- department summary (Phase 5.2) ----
DEPARTMENT_SUMMARY_COLLECTION = "employees"     # the collection the aggregation runs on (explain reuses this)


def work_hours_hundredths(path: str) -> dict:
    """Expression: a stored work_hours (a BSON double with at most 2 decimals, R4) as INTEGER hundredths,
    floor(x * 100 + 0.5). x * 100 can land a hair off the integer (0.29 * 100 = 28.999999999999996,
    1.15 * 100 = 114.99999999999999), but never anywhere near .5 for a 2-decimal value, so the floor recovers
    the exact hundredths. No $round (which is half-to-even) is involved. Exact for every 2-decimal value
    (R4 stores 2); values with MORE decimals are outside the contract and are NOT guaranteed to follow the
    stored double's exact value (x * 100 itself rounds)."""
    return {"$toLong": {"$floor": {"$add": [{"$multiply": [path, 100]}, 0.5]}}}


def half_up_mean_hundredths(total_hundredths: int, count: int):
    """Mean of `count` values given as integer hundredths, rounded to 2 decimals HALF-UP, exactly:
    floor(T / n + 1/2) = (2T + n) // (2n). None when there are no values."""
    if count == 0:
        return None
    return ((2 * total_hundredths + count) // (2 * count)) / 100


def department_summary_pipeline(year: int, month: int, department: Optional[str] = None) -> list:
    """The aggregation behind GET /analytics/departments/summary (reused as-is by the explain endpoint later;
    it runs on DEPARTMENT_SUMMARY_COLLECTION). It starts from EMPLOYEES so that people with zero logs count.
      1 $match   joined_on <= last day of the month (R9); a valid non-empty string department; and, when asked,
                 an exact case-sensitive department equality (an explicit "" matches nothing, because
                 empty departments are excluded)
      2 $lookup  that employee's logs for the month, via the unique (emp_code, date) index; only the fields
                 needed are projected. Employees with no logs keep an empty array.
      3 $group   by department, WITHOUT $unwind (so headcount is one per employee). Each employee's logs array
                 is totalled with $map + the array form of $sum:
                   present_halves  2 per Mon-Fri presence record, 1 for a half day
                   work_n / work_hundredths   presence-status records with a numeric work_hours: their count and
                                  their integer-hundredths sum (the mean is finished in integers by the caller)
                   late_count / total_late_minutes   stored late_minutes > 0 only (negatives ignored)
                   leave_count / on_duty_count       by status, weekends included
      4 $sort    department ascending (MongoDB's ordinary string order)
      5 $project the item fields; _id is dropped.
    Missing or null late_minutes / half_day / work_hours read as 0 / false / not-a-value. No $round, no $unwind."""
    first, last = month_bounds(year, month)
    department_ok = {"$type": "string", "$ne": ""}
    if department is not None:
        department_ok["$eq"] = department
    late = {"$ifNull": ["$$l.late_minutes", 0]}
    is_presence = {"$in": ["$$l.status", list(PRESENCE_STATUSES)]}
    weekday_presence = {"$and": [is_presence, {"$not": [{"$in": ["$$l.date", weekend_dates(year, month)]}]}]}
    half_day = {"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}
    has_hours = {"$and": [is_presence, {"$isNumber": "$$l.work_hours"}]}

    def over_logs(expr):
        return {"$sum": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}}

    return [
        {"$match": {"joined_on": {"$lte": last}, "department": department_ok}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_halves": over_logs({"$cond": [weekday_presence, {"$cond": [half_day, 1, 2]}, 0]}),
            "work_n": over_logs({"$cond": [has_hours, 1, 0]}),
            "work_hundredths": over_logs({"$cond": [has_hours, work_hours_hundredths("$$l.work_hours"), 0]}),
            "late_count": over_logs({"$cond": [{"$gt": [late, 0]}, 1, 0]}),
            "total_late_minutes": over_logs({"$cond": [{"$gt": [late, 0]}, late, 0]}),
            "leave_count": over_logs({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
            "on_duty_count": over_logs({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
        }},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_halves": 1, "work_n": 1,
                      "work_hundredths": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1,
                      "on_duty_count": 1}},
    ]


def build_department_summary(month: str, rows: list) -> dict:
    """Contract `DepartmentSummary`: {"month", "items": [...]}. `rows` are the aggregation's per-department
    scalars (already sorted); only small integer arithmetic happens here."""
    items = []
    for row in rows:
        items.append({
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": int(row.get("present_halves") or 0) / 2,
            "avg_work_hours": half_up_mean_hundredths(int(row.get("work_hundredths") or 0),
                                                      int(row.get("work_n") or 0)),
            "late_count": int(row.get("late_count") or 0),
            "total_late_minutes": int(row.get("total_late_minutes") or 0),
            "leave_count": int(row.get("leave_count") or 0),
            "on_duty_count": int(row.get("on_duty_count") or 0),
        })
    return {"month": month, "items": items}
# ---- late-arrival leaderboard (Phase 5.3) ----
LATE_LEADERBOARD_COLLECTION = "attendance_logs"     # the collection the aggregation runs on (explain reuses this)
DEFAULT_LEADERBOARD_LIMIT = 10
MAX_LEADERBOARD_LIMIT = 50
LEADERBOARD_ITEM_KEYS = ("rank", "emp_code", "name", "department", "total_late_minutes", "late_count")


def late_leaderboard_pipeline(year: int, month: int, limit: int, department: Optional[str] = None) -> list:
    """The aggregation behind GET /analytics/leaderboard/late (reused as-is by GET /admin/explain/late_leaderboard;
    it runs on LATE_LEADERBOARD_COLLECTION).
      1 $match          the month's inclusive date range on attendance_logs.date (strings, so chronological)
      2 $group          by emp_code: total_late_minutes / late_count from the STORED late_minutes, positive values only
                        (missing / null / zero / negative contribute nothing); any status, any weekday
      3 $match          total_late_minutes > 0
      4 $lookup         employees by emp_code (unique index), projecting only name and department
      5 $unwind         drops logs whose employee does not exist (orphans)
      6 $match $expr    department is a BSON string (not an array containing one), and - when asked - exactly equal to `department` (case-sensitive;
                        an explicit "" is an exact empty-string match). This runs BEFORE ranking.
      7 $setWindowFields  $rank over total_late_minutes DESCENDING ONLY (emp_code is deliberately NOT in the sort,
                        so ties share a rank and the next rank is skipped: 1, 2, 2, 4)
      8 $match          rank <= limit (limit applies to ranks, so ties at the cutoff are all kept)
      9 $sort           total_late_minutes desc, then emp_code asc (emp_code is the group _id here)
     10 $project        the documented fields; no _id.
    Nothing is ranked or limited in Python."""
    first, last = month_bounds(year, month)
    late = {"$ifNull": ["$late_minutes", 0]}
    is_late = {"$gt": [late, 0]}
    # An AGGREGATION expression on purpose: the query form {"$type": "string"} (and a plain {"$eq": "X"}) also matches
    # an ARRAY field that merely contains a string element. $expr's $type sees the field's own BSON type, so only a
    # real string department passes; missing, null, number, array, object ... are all excluded.
    department_ok = {"$eq": [{"$type": "$emp.department"}, "string"]}
    if department is not None:
        department_ok = {"$and": [department_ok, {"$eq": ["$emp.department", department]}]}
    return [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$cond": [is_late, late, 0]}},
            "late_count": {"$sum": {"$cond": [is_late, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code",
            "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"$expr": department_ok}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]


def build_late_leaderboard(month: str, rows: list) -> dict:
    """Contract `Leaderboard`: {"month", "items": [...]} with the documented keys in the documented order."""
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r.get("name"), "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in rows]}
# ---- department trend (Phase 5.4) ----
TREND_COLLECTION = "employees"      # the collection the aggregation runs on (explain reuses this)
MAX_TREND_DAYS = 92                 # inclusive calendar dates; a 93-date range is rejected (see DECISIONS.md)
TREND_ITEM_KEYS = ("date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate",
                   "moving_avg_7d")
TREND_WINDOW = (-6, 0)              # the current row and up to six PRECEDING rows (calendar days, not working days)


def validate_trend_range(date_from: str, date_to: str) -> int:
    """Both arguments are already valid YYYY-MM-DD strings. Returns the number of inclusive calendar dates.
    ValueError (-> 422) when `to` is before `from` or the range holds more than MAX_TREND_DAYS dates."""
    first, last = parse_calendar_date(date_from), parse_calendar_date(date_to)
    if last < first:
        raise ValueError("to must not be before from")
    days = (last - first).days + 1
    if days > MAX_TREND_DAYS:
        raise ValueError(f"the range may contain at most {MAX_TREND_DAYS} calendar dates (got {days})")
    return days


def trend_department_filter(department: str) -> dict:
    """Exact, case-sensitive department equality that also requires the field to be a real BSON string. The plain
    equality keeps the (department, emp_code) index usable; the $expr then rejects an ARRAY department that merely
    contains the string (the query form of equality would match it), the same rule the 5.3 leaderboard applies."""
    return {"department": department, "$expr": {"$eq": [{"$type": "$department"}, "string"]}}


def department_trend_pipeline(date_from: str, date_to: str, department: str) -> list:
    """THE aggregation behind GET /analytics/departments/{department}/trend (the explain endpoint calls the same
    builder; it runs on TREND_COLLECTION). EMPLOYEES first, so people with no logs still count and the department
    filter uses the (department, emp_code) index; the attendance side is reached through the unique (emp_code, date)
    index, so only the department's own records in the range are ever read. No employee-day pair is materialised.
      1 $match    exact department (trend_department_filter) and joined_on <= `to`: an employee who joins after the
                  range can never be in any day's headcount, so they are left out (Phase 5.2 does the same)
      2 $project  emp_code, joined_on
      3 $lookup   that employee's logs in [from, to] via (emp_code, date); each log is reduced INSIDE the lookup to
                  halves (2 per presence status, 1 for a half day, 0 otherwise - integers, so no float error) and
                  late (1 when the stored late_minutes is a number > 0). Records dated before joined_on are kept
                  on purpose (DECISIONS.md 4). Orphan logs never appear: only existing employees are looked up.
      4 $facet    ONE document out, whatever the input (so a department whose staff all join later, or one with no
                  logs, still produces a result):
                    joins  headcount steps: employees grouped by max(joined_on, from) -> count
                    days   per-date totals of halves / late (the logs are $unwind-ed and $group-ed by date, so the
                           intermediate result is one row per record, never per employee-day)
      5 $project  the date spine, built in the database: $range(0, n) -> $dateAdd(from, i days) on a BSON UTC date ->
                  'YYYY-MM-DD', with is_working_day ($isoDayOfWeek <= 5), headcount = sum of the steps with
                  joined_on <= day, and halves / late for that day (0 when no record exists)
      6 $unwind + $replaceRoot   one document per calendar day
      7 $addFields  rate_t = the attendance rate in integer TEN-THOUSANDTHS, HALF-UP, only on a working day with
                  headcount > 0: floor((halves * 10000 + headcount) / (2 * headcount)); rate_n / rate_s = 1 / rate_t
                  for a non-null rate, 0 / 0 otherwise. No $round anywhere (it is half-to-even).
      8 $setWindowFields  sortBy date; windows documents [-6, 0] sum rate_s and rate_n: the current row and up to six
                  earlier rows of the requested range (shorter at the start; rows before `from` do not exist here)
      9 $sort + $project  by date; moving_avg_7d = floor((2 * sum + n) / (2 * n)) / 10000 (the mean of the already
                  rounded daily rates, half-up, in integers) or null when no row in the window has a rate."""
    days = validate_trend_range(date_from, date_to)
    first = parse_calendar_date(date_from)
    start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    late = {"$cond": [{"$and": [{"$isNumber": "$late_minutes"}, {"$gt": ["$late_minutes", 0]}]}, 1, 0]}
    halves = {"$cond": [{"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}
    has_rate = {"$isNumber": "$rate_t"}

    def step_sum(array, var, value, hit):
        """Sum of `value` over the elements of `array` for which `hit` holds (0 when none)."""
        return {"$sum": {"$map": {"input": array, "as": var, "in": {"$cond": [hit, value, 0]}}}}

    return [
        {"$match": {**trend_department_filter(department), "joined_on": {"$lte": date_to}}},
        {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                {"$project": {"_id": 0, "date": 1, "halves": halves, "late": late}},
            ],
            "as": "logs",
        }},
        {"$facet": {
            "joins": [
                {"$project": {"joined_on": 1}},
                {"$group": {"_id": {"$cond": [{"$lte": ["$joined_on", date_from]}, date_from, "$joined_on"]},
                            "n": {"$sum": 1}}},
            ],
            "days": [
                {"$unwind": "$logs"},
                {"$group": {"_id": "$logs.date", "halves": {"$sum": "$logs.halves"}, "late": {"$sum": "$logs.late"}}},
            ],
        }},
        {"$project": {"_id": 0, "rows": {"$map": {
            "input": {"$range": [0, days]}, "as": "i",
            "in": {"$let": {
                "vars": {"d": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}},
                "in": {"$let": {
                    "vars": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$d"}}},
                    "in": {
                        "date": "$$day",
                        "is_working_day": {"$lte": [{"$isoDayOfWeek": "$$d"}, 5]},
                        "headcount": step_sum("$joins", "j", "$$j.n", {"$lte": ["$$j._id", "$$day"]}),
                        "halves": step_sum("$days", "x", "$$x.halves", {"$eq": ["$$x._id", "$$day"]}),
                        "late": step_sum("$days", "x", "$$x.late", {"$eq": ["$$x._id", "$$day"]}),
                    },
                }},
            }},
        }}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$addFields": {"rate_t": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$halves", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$addFields": {"rate_n": {"$cond": [has_rate, 1, 0]}, "rate_s": {"$cond": [has_rate, "$rate_t", 0]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "win_s": {"$sum": "$rate_s", "window": {"documents": list(TREND_WINDOW)}},
            "win_n": {"$sum": "$rate_n", "window": {"documents": list(TREND_WINDOW)}},
        }}},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$halves", 2]},
            "late_count": "$late",
            "attendance_rate": {"$cond": [has_rate, {"$divide": ["$rate_t", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$win_n", 0]},
                                        {"$divide": [{"$floor": {"$divide": [
                                            {"$add": [{"$multiply": [2, "$win_s"]}, "$win_n"]},
                                            {"$multiply": [2, "$win_n"]}]}}, 10000]},
                                        None]},
        }},
    ]


def build_department_trend(department: str, rows: list) -> dict:
    """Contract `Trend`: {"department", "items": [...]}; each item has exactly TREND_ITEM_KEYS, in that order. The
    rows are the pipeline's final documents; only types are normalised here (no arithmetic, no rounding)."""
    items = []
    for r in rows:
        rate, avg = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if rate is None else float(rate),
            "moving_avg_7d": None if avg is None else float(avg),
        })
    return {"department": department, "items": items}
# === PURE-HELPERS-END ===


_indexes_ready = False


def _ensure_indexes_and_mark_ready() -> None:
    global _indexes_ready
    ensure_indexes(db)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        _ensure_indexes_and_mark_ready()
        logger.info("indexes ensured")
    except ConnectionFailure as exc:
        # MongoDB unreachable right now: still start, so /health can answer 503. The (idempotent)
        # index creation is retried by /health as soon as MongoDB answers. Any other failure - for
        # example a unique index that cannot be built - is NOT swallowed: startup fails loudly.
        logger.error("MongoDB unreachable at startup (%s); indexes will be created once /health can ping it",
                     type(exc).__name__)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_unavailable(_request, exc: PyMongoError):
    """Any PyMongo failure that no route handled itself (server unreachable, timeout, command failure)
    becomes a fixed 503. Routes keep handling their expected cases first (DuplicateKeyError -> 409, the
    /health 503, 404/422). Only the exception's class name is logged and nothing from its message is
    returned, because driver messages can contain hostnames or credentials. Non-PyMongo exceptions are
    deliberately not covered: a programming error stays a 500."""
    logger.error("database error: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
# Strict epoch milliseconds: the before-validator runs on the raw JSON value, so nothing is coerced
# first (a bool, float, numeric string or seconds-sized int never becomes a valid instant).
EpochMillis = Annotated[
    int,
    BeforeValidator(validate_epoch_ms),
    Field(ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX),
]


class EmployeeIn(BaseModel):
    # Unknown / derived fields a client sends (created_at, _id, ...) are ignored, per the contract.
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=EMP_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_PATTERN)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_real_date(cls, value: str) -> str:
        return validate_calendar_date(value)

    @model_validator(mode="after")
    def _shift_start_differs_from_shift_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    # Omitted -> None -> "now". An explicit null is rejected: EpochMillis is not nullable here.
    punched_at: EpochMillis = Field(default=None)
    # Only presence statuses may be created by punch-in.
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    # Omitted -> None -> "now". An explicit null is rejected, exactly as for punch-in.
    punched_at: EpochMillis = Field(default=None)


class RegularizeIn(BaseModel):
    """PATCH body. Only these fields can be changed; anything else a client sends (history, created_at,
    _id, late_minutes, work_hours, ...) is ignored, never stored."""
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # Omitted -> None -> unchanged. An explicit null is rejected, as for the other phases.
    status: AttendanceStatus = Field(default=None)
    punch_in: EpochMillis = Field(default=None)
    punch_out: EpochMillis = Field(default=None)

    @model_validator(mode="after")
    def _no_punch_times_with_absence(self):
        try:
            check_regularization_request(self.status, self.punch_in is not None, self.punch_out is not None)
        except RuleError as e:
            raise ValueError(e.detail) from None
        return self


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """200 only when MongoDB answers a ping, otherwise 503. The 503 body is fixed text: exception
    messages can contain hosts or credentials, so only the exception's class name is logged."""
    try:
        db.command("ping")
        if not _indexes_ready:
            _ensure_indexes_and_mark_ready()
    except PyMongoError as exc:
        logger.warning("health check failed: %s", type(exc).__name__)
        raise HTTPException(503, "database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = build_employee_doc(body.model_dump(), utc_now())
    try:
        # No find-then-insert: the unique emp_code index is the only arbiter, so concurrent creates
        # cannot both succeed.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return serialize_employee(doc)


def employee_page_cursor(query: dict, skip: int, limit: int):
    """The employees page query: filter, sort and pagination all happen in MongoDB. Served by the
    (department, emp_code) index when filtered, and by the unique emp_code index otherwise."""
    return db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(limit)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    q = employee_list_filter(department)
    total = db.employees.count_documents(q)
    skip = page_skip(page, page_size)
    # A page past the end is a normal 200 with no items (the contract defines no error for it).
    items = [serialize_employee(d) for d in employee_page_cursor(q, skip, page_size)] if skip < total else []
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1}
    )
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = (
        instant_from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_seconds(utc_now())
    )
    doc = build_attendance_doc(emp, body.emp_code, body.status, ts)
    try:
        # No find-then-insert: the unique (emp_code, date) index decides the race.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1}
    )
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = (
        instant_from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else truncate_seconds(utc_now())
    )
    # The employee's most recent record whose punch_in <= punched_at (index: emp_code, punch_in desc).
    # Records without a punch_in (ABSENT / LEAVE) never match a $lte comparison.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        # No record starts at or before punched_at. If any later punch-in exists, punched_at is before that
        # record's punch_in, which the contract classifies as 422 regardless of whether that record is still open.
        # This read runs only on the no-eligible-record path; the atomic close below is unchanged.
        later = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": ts}}, {"punch_out": 1}, sort=[("punch_in", 1)]
        )
        if later is not None:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:  # null or missing means still open
        raise HTTPException(409, "already punched out")
    try:
        changes = build_punch_out_update(
            rec["punch_in"], rec["date"], emp["shift_start"], emp["shift_end"], ts
        )
    except RuleError as e:
        raise HTTPException(e.status_code, e.detail) from None
    # Close it atomically: the update only matches while the record is STILL open (and its punch_in is
    # still the value the calculations used). Of two simultaneous punch-outs exactly one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out or modified concurrently")
    return serialize_attendance(updated)


def _checked_date_param(name: str, value: Optional[str]) -> Optional[str]:
    """Real calendar date in YYYY-MM-DD, or a standard FastAPI 422 body ({"detail": [...]})."""
    if value is None:
        return None
    try:
        validate_calendar_date(value)
    except ValueError as e:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", name), "msg": f"Value error, {e}", "input": value}]
        ) from None
    return value


def _checked_path_date(name: str, value: str) -> str:
    """Path parameter that must be a real YYYY-MM-DD date; otherwise a standard FastAPI 422 body."""
    try:
        validate_calendar_date(value)
    except ValueError as e:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("path", name), "msg": f"Value error, {e}", "input": value}]
        ) from None
    return value


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]      # GET /attendance order; the explain command uses the same list


def attendance_page_cursor(query: dict, skip: int, limit: int):
    """The attendance page query. Filter, sort and pagination all run in MongoDB. Sort = date desc,
    emp_code asc, which is the (date -1, emp_code 1) index; an emp_code filter is served by the unique
    (emp_code, date) index scanned backwards, a status filter by (status, date -1, emp_code 1)."""
    return (
        db.attendance_logs.find(query, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip(skip)
        .limit(limit)
    )


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    date_from = _checked_date_param("date_from", date_from)
    date_to = _checked_date_param("date_to", date_to)
    try:
        validate_date_range(date_from, date_to)
    except ValueError as e:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "date_from"), "msg": f"Value error, {e}",
              "input": date_from}]
        ) from None
    q = attendance_list_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = page_skip(page, page_size)
    # A page past the end is a normal 200 with no items (the contract defines no error for it).
    items = (
        [serialize_attendance(d) for d in attendance_page_cursor(q, skip, page_size)]
        if skip < total
        else []
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    _checked_path_date("date", date)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "attendance record not found")
    try:
        changes = plan_regularization(
            rec, emp["shift_start"], emp["shift_end"], body.status,
            instant_from_epoch_ms(body.punch_in) if body.punch_in is not None else None,
            instant_from_epoch_ms(body.punch_out) if body.punch_out is not None else None,
        )
    except RuleError as e:
        raise HTTPException(e.status_code, e.detail) from None
    entry = build_history_entry(utc_now(), body.regularized_by, body.reason, changes)
    flt, update = build_regularization_write(rec, changes, entry)
    # One conditional update: it applies the changes AND appends the history entry only if the record is
    # still exactly as it was read. A concurrent PATCH / punch-out makes it match nothing -> 409, and nothing
    # was written, so there is no lost update and no duplicate entry. No retry loop on purpose.
    updated = db.attendance_logs.find_one_and_update(flt, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance record was modified concurrently; retry")
    return serialize_attendance(updated)


def _checked_month(value: str) -> tuple:
    """(year, month) of a valid YYYY-MM, or a standard FastAPI 422 body. Runs before any lookup."""
    try:
        return parse_month(value)
    except ValueError as e:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "month"), "msg": f"Value error, {e}", "input": value}]
        ) from None


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    year, mon = _checked_month(month)           # 422 first: an invalid month beats an unknown employee
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    # The attendance totals come from ONE aggregation; no attendance documents are loaded into Python.
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, year, mon)))
    return build_employee_monthly(emp_code, month, count_working_days(emp["joined_on"], year, mon),
                                  rows[0] if rows else None)


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    year, mon = _checked_month(month)           # 422 for a missing / invalid month, like the monthly endpoint
    # One aggregation over employees (with a $lookup into attendance_logs); nothing is loaded into Python
    # except the per-department summary rows. An unknown department simply yields no items (200, never 404).
    rows = list(db.employees.aggregate(department_summary_pipeline(year, mon, department)))
    return build_department_summary(month, rows)


# ---- late-arrival leaderboard + explain (Phase 5.3) ----
def _leaderboard_pipeline_for(month: str, limit: int, department: Optional[str]) -> list:
    """Validate `month` (422 first), then build THE leaderboard pipeline. The endpoint and the explain endpoint both
    call this, so there is exactly one pipeline."""
    year, mon = _checked_month(month)
    return late_leaderboard_pipeline(year, mon, limit, department)


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(DEFAULT_LEADERBOARD_LIMIT, ge=1, le=MAX_LEADERBOARD_LIMIT),
    department: Optional[str] = None,
):
    pipeline = _leaderboard_pipeline_for(month, limit, department)
    # One aggregation; only the final ranked rows reach Python. An unknown department yields no items (200).
    return build_late_leaderboard(month, list(getattr(db, LATE_LEADERBOARD_COLLECTION).aggregate(pipeline)))


def _explain_late_leaderboard(month: Optional[str], limit: int, department: Optional[str]):
    if month is None:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", "month"), "msg": "Field required", "input": None}])
    return LATE_LEADERBOARD_COLLECTION, _leaderboard_pipeline_for(month, limit, department)


# ---- department trend + explain (Phase 5.4) ----
def _trend_pipeline_for(department: str, date_from: str, date_to: str) -> list:
    """Validate `from` / `to` (422 first - before any department lookup), then build THE trend pipeline. The endpoint and
    the explain endpoint both call this, so there is exactly one pipeline."""
    date_from = _checked_date_param("from", date_from)
    date_to = _checked_date_param("to", date_to)
    try:
        validate_trend_range(date_from, date_to)
    except ValueError as e:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "to"), "msg": f"Value error, {e}", "input": date_to}]
        ) from None
    return department_trend_pipeline(date_from, date_to, department)


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to")):
    pipeline = _trend_pipeline_for(department, date_from, date_to)      # 422 beats 404
    # 404 only when NO employee has this exact department (an existing department with no logs, or whose staff all join
    # after `to`, is a normal 200). One indexed point lookup; the analytics itself is the single aggregation below.
    if getattr(db, TREND_COLLECTION).find_one(trend_department_filter(department), {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    return build_department_trend(department, list(getattr(db, TREND_COLLECTION).aggregate(pipeline)))


def _explain_department_trend(department: Optional[str], date_from: Optional[str], date_to: Optional[str]):
    missing = [name for name, value in (("department", department), ("from", date_from), ("to", date_to)) if value is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing])
    return TREND_COLLECTION, _trend_pipeline_for(department, date_from, date_to)


# ---- explain for the Phase 1-5.2 operations (Phase 5.5) ----
def _required_query(**values):
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing])


def _explain_employee_monthly(emp_code: Optional[str], month: Optional[str]):
    _required_query(emp_code=emp_code, month=month)
    year, mon = _checked_month(month)
    return "attendance_logs", employee_monthly_pipeline(emp_code, year, mon)            # the endpoint's builder


def _explain_department_summary(month: Optional[str], department: Optional[str]):
    _required_query(month=month)
    year, mon = _checked_month(month)
    return DEPARTMENT_SUMMARY_COLLECTION, department_summary_pipeline(year, mon, department)   # the endpoint's builder


def _explain_attendance_list(emp_code, date_from, date_to, status, page, page_size):
    """The page query of GET /attendance, built from the same helpers the endpoint uses: attendance_list_filter,
    page_skip and ATTENDANCE_SORT. Returns (collection, the `find` command document)."""
    date_from = _checked_date_param("date_from", date_from)
    date_to = _checked_date_param("date_to", date_to)
    try:
        validate_date_range(date_from, date_to)
    except ValueError as e:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "date_from"), "msg": f"Value error, {e}", "input": date_from}]
        ) from None
    query = attendance_list_filter(emp_code, date_from, date_to, status)
    return "attendance_logs", {"find": "attendance_logs", "filter": query, "sort": dict(ATTENDANCE_SORT),
                               "projection": {"_id": 0}, "skip": page_skip(page, page_size), "limit": page_size}


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    limit: int = Query(DEFAULT_LEADERBOARD_LIMIT, ge=1, le=MAX_LEADERBOARD_LIMIT),
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
):
    """executionStats explain of the named endpoint's main query / pipeline, built by the endpoint's own builder."""
    if endpoint == "late_leaderboard":
        collection, body = _explain_late_leaderboard(month, limit, department)
    elif endpoint == "department_trend":
        collection, body = _explain_department_trend(department, trend_from, trend_to)
    elif endpoint == "employee_monthly":
        collection, body = _explain_employee_monthly(emp_code, month)
    elif endpoint == "department_summary":
        collection, body = _explain_department_summary(month, department)
    else:
        collection, body = _explain_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    inner = body if isinstance(body, dict) else {"aggregate": collection, "pipeline": body, "cursor": {}}
    out = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(out))}
