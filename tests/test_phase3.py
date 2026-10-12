"""Phase 3 tests: /health, POST /attendance/punch-out, GET /attendance.

Layers:
  1. TestPunchOutPure / TestAttendanceFilterPure   stdlib only; run anywhere.
  2. *Fake classes     endpoint functions on an IN-MEMORY FAKE (tests/_fakes.py). MOCKED: they prove
                       branching, error mapping and query shape, NOT MongoDB behaviour.
  3. TestPhase3Http    status codes and bodies through TestClient, still on the fake.
  4. TestMongoPhase3   REAL MongoDB, opt-in via TEST_MONGO_URI; throwaway database
                       attendance_test_p3_<random>, dropped afterwards. Never touches MONGO_DB.
"""
import asyncio
import json
import os
import threading
import unittest
import uuid
import calendar
from datetime import datetime, timezone, timedelta

from tests._loader import load_main, have, ROOT

m, MODE = load_main()
FULL = MODE == "full"
HAVE_HTTP = FULL and have("httpx")
TEST_URI = os.environ.get("TEST_MONGO_URI")

UTC = timezone.utc
ATT_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
            "overtime_minutes", "half_day", "history"}
DAY = ("09:30", "18:30")
NIGHT = ("22:00", "06:00")


def utc_ms(y, mo, d, h=0, mi=0, s=0, ms=0):
    return calendar.timegm((y, mo, d, h, mi, s)) * 1000 + ms


def ist_ms(y, mo, d, h=0, mi=0, s=0, ms=0):
    return utc_ms(y, mo, d, h, mi, s, ms) - (5 * 3600 + 30 * 60) * 1000


def at(ms):
    return m.instant_from_epoch_ms(ms)


# ======================================================================== 1. pure
class TestPunchOutPure(unittest.TestCase):
    PI = at(ist_ms(2026, 7, 6, 9, 30, 0))
    D = "2026-07-06"

    def out(self, delta_seconds, pi=None, date=None, shift=DAY):
        pi = pi or self.PI
        po = pi + timedelta(seconds=delta_seconds)
        return m.build_punch_out_update(pi, date or self.D, shift[0], shift[1], po)

    def code_of(self, fn):
        with self.assertRaises(m.RuleError) as cm:
            fn()
        return cm.exception.status_code, cm.exception.detail

    # ---- window rules
    def test_equal_instants_are_422(self):
        self.assertEqual(self.code_of(lambda: self.out(0))[0], 422)

    def test_before_punch_in_is_422(self):
        self.assertEqual(self.code_of(lambda: self.out(-1))[0], 422)
        self.assertEqual(self.code_of(lambda: self.out(-3600))[0], 422)

    def test_one_second_after_is_accepted(self):
        r = self.out(1)
        self.assertEqual((r["work_hours"], r["half_day"], r["overtime_minutes"]), (0.0, True, 0))

    def test_exactly_24_hours_is_accepted(self):
        r = self.out(86400)
        self.assertEqual(r["work_hours"], 24.0)
        self.assertFalse(r["half_day"])

    def test_more_than_24_hours_is_422(self):
        self.assertEqual(self.code_of(lambda: self.out(86401))[0], 422)
        self.assertEqual(self.code_of(lambda: self.out(3 * 86400))[0], 422)

    def test_rule_error_details_are_plain_text(self):
        for delta in (0, 86401):
            code, detail = self.code_of(lambda: self.out(delta))
            self.assertIsInstance(detail, str)
            self.assertTrue(detail)

    def test_milliseconds_are_truncated_before_the_window_check(self):
        pi = self.PI.replace(microsecond=900_000)
        po = self.PI.replace(microsecond=100_000)      # both truncate to the same second -> equal -> 422
        self.assertEqual(self.code_of(lambda: m.build_punch_out_update(pi, self.D, *DAY, po))[0], 422)

    # ---- overtime (R3)
    def test_overtime_boundaries_day_shift(self):
        def ot(h, mi, s):
            po = at(ist_ms(2026, 7, 6, h, mi, s))
            return m.build_punch_out_update(self.PI, self.D, *DAY, po)["overtime_minutes"]
        self.assertEqual(ot(18, 29, 59), 0)     # just before shift end
        self.assertEqual(ot(18, 30, 0), 0)      # at shift end
        self.assertEqual(ot(18, 59, 59), 0)     # 29 min 59 s
        self.assertEqual(ot(19, 0, 0), 30)      # exactly 30 min
        self.assertEqual(ot(19, 0, 59), 30)     # 30 min 59 s -> floor
        self.assertEqual(ot(19, 1, 0), 31)
        self.assertEqual(ot(19, 10, 0), 40)

    def test_overtime_overnight_shift(self):
        pi = at(ist_ms(2026, 7, 6, 21, 55))

        def ot(d, h, mi, s=0):
            return m.build_punch_out_update(pi, "2026-07-06", *NIGHT, at(ist_ms(2026, 7, d, h, mi, s)))["overtime_minutes"]
        self.assertEqual(ot(6, 23, 30), 0)       # before midnight: far from the 06:00 end
        self.assertEqual(ot(7, 5, 59, 59), 0)    # just before shift end
        self.assertEqual(ot(7, 6, 0), 0)         # at shift end
        self.assertEqual(ot(7, 6, 29, 59), 0)
        self.assertEqual(ot(7, 6, 30), 30)
        self.assertEqual(ot(7, 6, 40), 40)       # the audit's example (was 1480)

    def test_overnight_work_hours(self):
        pi = at(ist_ms(2026, 7, 6, 21, 55))
        r = m.build_punch_out_update(pi, "2026-07-06", *NIGHT, at(ist_ms(2026, 7, 7, 6, 40)))
        self.assertEqual((r["work_hours"], r["overtime_minutes"], r["half_day"]), (8.75, 40, False))

    # ---- work hours / half day (R4, R5)
    def test_half_up_14850_seconds(self):
        r = self.out(14850)
        self.assertEqual(r["work_hours"], 4.13)
        self.assertTrue(r["half_day"])

    def test_half_day_boundary_uses_rounded_hours(self):
        self.assertTrue(self.out(16181)["half_day"])      # 4.4947 h -> 4.49
        self.assertFalse(self.out(16182)["half_day"])     # 4.495 h -> 4.50 (tie rounds up)
        self.assertEqual(self.out(16182)["work_hours"], 4.5)
        self.assertFalse(self.out(16200)["half_day"])

    def test_sample_document_values(self):
        pi = at(utc_ms(2026, 7, 6, 3, 58))
        r = m.build_punch_out_update(pi, "2026-07-06", *DAY, at(utc_ms(2026, 7, 6, 13, 5)))
        self.assertEqual((r["work_hours"], r["overtime_minutes"], r["half_day"]), (9.12, 0, False))

    # ---- datatypes
    def test_stored_values_are_aware_utc_and_truncated(self):
        r = self.out(100)
        self.assertEqual(r["punch_out"].utcoffset(), timedelta(0))
        self.assertEqual(r["punch_out"].microsecond, 0)
        self.assertIsInstance(r["work_hours"], float)
        self.assertIsInstance(r["overtime_minutes"], int)
        self.assertEqual(set(r), {"punch_out", "work_hours", "overtime_minutes", "half_day"})

    def test_naive_stored_datetimes_are_treated_as_utc(self):
        naive_pi = datetime(2026, 7, 6, 4, 0, 0)                 # == 09:30 IST
        r = m.build_punch_out_update(naive_pi, self.D, *DAY, datetime(2026, 7, 6, 13, 40, 0))  # naive: 19:10 IST
        self.assertEqual((r["work_hours"], r["overtime_minutes"]), (9.67, 40))


class TestAttendanceFilterPure(unittest.TestCase):
    def test_filter_combinations(self):
        f = m.attendance_list_filter
        self.assertEqual(f(None, None, None, None), {})
        self.assertEqual(f("EMP0001", None, None, None), {"emp_code": "EMP0001"})
        self.assertEqual(f(None, "2026-07-01", None, None), {"date": {"$gte": "2026-07-01"}})
        self.assertEqual(f(None, None, "2026-07-31", None), {"date": {"$lte": "2026-07-31"}})
        self.assertEqual(f(None, "2026-07-01", "2026-07-31", "WFH"),
                         {"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "status": "WFH"})
        self.assertEqual(f("EMP0001", "2026-07-01", "2026-07-01", "PRESENT"),
                         {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-01"},
                          "status": "PRESENT"})

    def test_filter_is_exact_not_case_folded(self):
        self.assertEqual(m.attendance_list_filter("emp0001", None, None, None), {"emp_code": "emp0001"})

    def test_date_range_validation(self):
        m.validate_date_range(None, None)
        m.validate_date_range("2026-07-01", None)
        m.validate_date_range(None, "2026-07-01")
        m.validate_date_range("2026-07-01", "2026-07-01")      # equal is fine
        m.validate_date_range("2026-07-01", "2026-07-02")
        m.validate_date_range("2026-12-31", "2027-01-01")
        for a, b in (("2026-07-02", "2026-07-01"), ("2027-01-01", "2026-12-31")):
            with self.assertRaises(ValueError):
                m.validate_date_range(a, b)


# ======================================================================== 2. fake-DB endpoint tests
if FULL:
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from pymongo.errors import ConnectionFailure, OperationFailure, ServerSelectionTimeoutError
    from tests._fakes import FakeDB


@unittest.skipUnless(FULL, "needs fastapi/pydantic/pymongo installed: NOT EXECUTED")
class FakeDbCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDB()
        self._orig_db, self._orig_ready = m.db, m._indexes_ready
        m.db = self.fake

    def tearDown(self):
        m.db, m._indexes_ready = self._orig_db, self._orig_ready

    def add_emp(self, code="EMP0001", start="09:30", end="18:30", dept="Engineering"):
        self.fake.employees.docs.append({
            "_id": "e-" + code, "emp_code": code, "name": "N", "email": "n@x.io", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": "2026-01-05",
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)})

    def seed(self, code, date, pi_ms=None, po_ms=None, status="PRESENT", **extra):
        doc = {"_id": f"a-{code}-{date}", "emp_code": code, "date": date, "status": status,
               "punch_in": None if pi_ms is None else at(pi_ms),
               "punch_out": None if po_ms is None else at(po_ms),
               "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
        doc.update(extra)
        self.fake.attendance_logs.docs.append(doc)
        return doc

    def stored(self, code, date):
        return next(d for d in self.fake.attendance_logs.docs if d["emp_code"] == code and d["date"] == date)

    def status_of(self, fn, *a, **kw):
        try:
            return 200, fn(*a, **kw)
        except HTTPException as e:
            return e.status_code, e.detail


class TestHealthFake(FakeDbCase):
    def test_ok_when_ping_succeeds(self):
        m._indexes_ready = True
        self.assertEqual(m.health(), {"status": "ok"})
        self.assertEqual(self.fake.pings, 1)

    def test_pings_on_every_request(self):
        m._indexes_ready = True
        m.health()
        m.health()
        self.assertEqual(self.fake.pings, 2)

    def test_503_when_mongodb_unavailable_and_no_secret_leak(self):
        self.fake.ping_error = ServerSelectionTimeoutError("user:s3cret@db.internal:27017 timed out")
        with self.assertLogs("attendance", level="WARNING") as logs:
            code, detail = self.status_of(m.health)
        self.assertEqual((code, detail), (503, "database unavailable"))
        self.assertNotIn("s3cret", detail)
        joined = " ".join(logs.output)
        self.assertNotIn("s3cret", joined)                    # message never logged
        self.assertIn("ServerSelectionTimeoutError", joined)  # only the class name

    def test_every_pymongo_failure_is_503(self):
        for err in (ConnectionFailure("x"), OperationFailure("auth failed s3cret"), ServerSelectionTimeoutError("t")):
            with self.subTest(err=type(err).__name__):
                self.fake.ping_error = err
                self.assertEqual(self.status_of(m.health)[0], 503)

    def test_unexpected_non_mongo_error_is_not_masked(self):
        self.fake.ping_error = RuntimeError("bug")
        with self.assertRaises(RuntimeError):
            m.health()

    def test_health_creates_indexes_once_mongo_is_back(self):
        m._indexes_ready = False
        self.assertEqual(m.health(), {"status": "ok"})
        self.assertTrue(m._indexes_ready)
        self.assertEqual(len(self.fake.employees.created_indexes), 2)
        self.assertEqual(len(self.fake.attendance_logs.created_indexes), 4)
        m.health()  # already ready: no further index calls
        self.assertEqual(len(self.fake.employees.created_indexes), 2)

    def test_503_if_index_creation_fails_in_health(self):
        m._indexes_ready = False
        self.fake.employees.index_error = OperationFailure("index build failed")
        self.assertEqual(self.status_of(m.health)[0], 503)
        self.assertFalse(m._indexes_ready)


class TestLifespanFake(FakeDbCase):
    def run_lifespan(self):
        async def go():
            async with m.lifespan(m.app):
                pass
        asyncio.run(go())

    def test_normal_startup_ensures_indexes(self):
        m._indexes_ready = False
        self.run_lifespan()
        self.assertTrue(m._indexes_ready)
        self.assertEqual(len(self.fake.attendance_logs.created_indexes), 4)

    def test_startup_with_mongodb_down_still_starts_and_stays_not_ready(self):
        m._indexes_ready = False
        self.fake.employees.index_error = ServerSelectionTimeoutError("down")
        self.run_lifespan()                       # must NOT raise
        self.assertFalse(m._indexes_ready)

    def test_other_index_failures_still_fail_startup(self):
        self.fake.employees.index_error = OperationFailure("E11000 duplicate key building unique index")
        with self.assertRaises(OperationFailure):
            self.run_lifespan()

    def test_restart_is_idempotent(self):
        self.run_lifespan()
        self.run_lifespan()
        self.assertTrue(m._indexes_ready)


class TestPunchOutFake(FakeDbCase):
    def setUp(self):
        super().setUp()
        self.add_emp("EMP0001")

    def out(self, ms=None, emp="EMP0001"):
        kw = {} if ms is None else {"punched_at": ms}      # omit the field: explicit null is a 422
        return m.punch_out(m.PunchOutIn(emp_code=emp, **kw))

    def test_success_response_and_stored_record(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 28), late_minutes=0)
        ms = ist_ms(2026, 7, 6, 18, 35)
        r = self.out(ms)
        self.assertEqual(set(r), ATT_KEYS)
        self.assertEqual((r["punch_out"], r["work_hours"], r["overtime_minutes"], r["half_day"]),
                         (ms, 9.12, 0, False))
        self.assertEqual(r["punch_in"], ist_ms(2026, 7, 6, 9, 28))
        self.assertNotIn("_id", r)
        self.assertNotIn("id", r)
        s = self.stored("EMP0001", "2026-07-06")
        self.assertEqual(s["punch_out"].utcoffset(), timedelta(0))      # stored as an aware datetime
        self.assertIsInstance(s["punch_out"], datetime)

    def test_overtime_and_preserved_fields(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 41, 1), late_minutes=11, status="WFH")
        r = self.out(ist_ms(2026, 7, 6, 19, 10))
        self.assertEqual((r["overtime_minutes"], r["late_minutes"], r["status"], r["history"]), (40, 11, "WFH", []))

    def test_overnight_punch_out_after_midnight(self):
        self.add_emp("EMP0005", "22:00", "06:00")
        self.seed("EMP0005", "2026-07-06", ist_ms(2026, 7, 6, 21, 55))
        r = self.out(ist_ms(2026, 7, 7, 6, 40), "EMP0005")
        self.assertEqual((r["date"], r["work_hours"], r["overtime_minutes"]), ("2026-07-06", 8.75, 40))

    def test_default_punched_at_is_now_truncated(self):
        now = m.truncate_seconds(m.utc_now())
        pi = now - timedelta(hours=1)
        self.seed("EMP0001", m.ist_date_str(pi), int(pi.timestamp() * 1000))
        r = self.out(None)
        self.assertEqual(r["punch_out"] % 1000, 0)
        self.assertTrue(1.0 <= r["work_hours"] <= 1.01)

    def test_unknown_employee_is_404_without_touching_attendance(self):
        self.assertEqual(self.status_of(self.out, ist_ms(2026, 7, 6, 18, 0), "EMP9999"), (404, "employee not found"))
        self.assertEqual(self.fake.attendance_logs.calls, [])

    def test_employee_without_any_record_is_404(self):
        self.assertEqual(self.status_of(self.out, ist_ms(2026, 7, 6, 18, 0)), (404, "no punch-in found"))

    def test_punch_out_before_the_only_punch_in_is_422_by_contract(self):
        # Contract: punch_out must be strictly after punch_in.
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        self.assertEqual(self.status_of(self.out, ist_ms(2026, 7, 6, 9, 29, 59)), (422, "punched_at must be after punch_in"))
        self.assertIsNone(self.stored("EMP0001", "2026-07-06")["punch_out"])

    def test_equal_instant_is_422_and_changes_nothing(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        code, _ = self.status_of(self.out, ist_ms(2026, 7, 6, 9, 30))
        self.assertEqual(code, 422)
        self.assertIsNone(self.stored("EMP0001", "2026-07-06")["punch_out"])
        self.assertNotIn("find_one_and_update", self.fake.attendance_logs.calls)

    def test_one_second_after_exactly_24h_and_beyond(self):
        base = ist_ms(2026, 7, 6, 9, 30)
        cases = [(1, 200), (86400, 200), (86401, 422), (2 * 86400, 422)]
        for delta, expected in cases:
            with self.subTest(delta=delta):
                self.fake.attendance_logs.docs.clear()
                self.seed("EMP0001", "2026-07-06", base)
                code, _ = self.status_of(self.out, base + delta * 1000)
                self.assertEqual(code, expected)
                if expected == 422:
                    self.assertIsNone(self.stored("EMP0001", "2026-07-06")["punch_out"])

    def test_milliseconds_in_input_are_truncated(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        r = self.out(ist_ms(2026, 7, 6, 19, 0, 59, 999))
        self.assertEqual((r["punch_out"], r["overtime_minutes"]), (ist_ms(2026, 7, 6, 19, 0, 59), 30))

    def test_already_closed_is_409_and_record_unchanged(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30), ist_ms(2026, 7, 6, 18, 0), work_hours=8.5)
        code, detail = self.status_of(self.out, ist_ms(2026, 7, 6, 20, 0))
        self.assertEqual((code, detail), (409, "already punched out"))
        self.assertEqual(self.stored("EMP0001", "2026-07-06")["work_hours"], 8.5)
        self.assertNotIn("find_one_and_update", self.fake.attendance_logs.calls)

    def test_closed_latest_record_is_409_even_if_an_older_record_is_open(self):
        # Must NOT silently close the older open record.
        self.seed("EMP0001", "2026-07-05", ist_ms(2026, 7, 5, 9, 30))
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30), ist_ms(2026, 7, 6, 18, 0))
        self.assertEqual(self.status_of(self.out, ist_ms(2026, 7, 6, 19, 0))[0], 409)
        self.assertIsNone(self.stored("EMP0001", "2026-07-05")["punch_out"])

    def test_selects_latest_eligible_record_only(self):
        self.seed("EMP0001", "2026-07-05", ist_ms(2026, 7, 5, 9, 30))
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        self.seed("EMP0001", "2026-07-07", ist_ms(2026, 7, 7, 9, 30))     # punch_in after punched_at
        r = self.out(ist_ms(2026, 7, 6, 18, 35))
        self.assertEqual(r["date"], "2026-07-06")
        self.assertIsNone(self.stored("EMP0001", "2026-07-05")["punch_out"])
        self.assertIsNone(self.stored("EMP0001", "2026-07-07")["punch_out"])

    def test_other_employees_and_absent_leave_records_are_untouched(self):
        self.add_emp("EMP0002")
        self.seed("EMP0002", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        self.seed("EMP0001", "2026-07-05", None, None, status="ABSENT")   # no punch_in: never a candidate
        self.seed("EMP0001", "2026-07-07", None, None, status="LEAVE")
        r = self.out(ist_ms(2026, 7, 6, 18, 0))
        self.assertEqual((r["date"], r["status"]), ("2026-07-06", "PRESENT"))
        self.assertIsNone(self.stored("EMP0002", "2026-07-06")["punch_out"])
        for d in ("2026-07-05", "2026-07-07"):
            s = self.stored("EMP0001", d)
            self.assertEqual((s["punch_out"], s["work_hours"], s["punch_in"]), (None, None, None))

    def test_only_absent_or_leave_records_means_no_punch_in_found(self):
        self.seed("EMP0001", "2026-07-06", None, None, status="ABSENT")
        self.assertEqual(self.status_of(self.out, ist_ms(2026, 7, 6, 18, 0)), (404, "no punch-in found"))

    def test_update_is_conditional_on_the_record_still_being_open(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        self.out(ist_ms(2026, 7, 6, 18, 0))
        flt, upd = self.fake.attendance_logs.last_update
        self.assertIsNone(flt["punch_out"])
        self.assertIn("punch_out", flt)
        self.assertIn("_id", flt)
        self.assertIn("punch_in", flt)
        self.assertEqual(set(upd), {"$set"})
        self.assertEqual(set(upd["$set"]), {"punch_out", "work_hours", "overtime_minutes", "half_day"})

    def test_lost_race_is_409_and_does_not_overwrite_the_winner(self):
        rec = self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        winner = at(ist_ms(2026, 7, 6, 17, 0))

        def other_request_wins():
            rec["punch_out"], rec["work_hours"] = winner, 7.5
        self.fake.attendance_logs.before_update = other_request_wins
        code, _ = self.status_of(self.out, ist_ms(2026, 7, 6, 18, 0))
        self.assertEqual(code, 409)
        s = self.stored("EMP0001", "2026-07-06")
        self.assertEqual((s["punch_out"], s["work_hours"]), (winner, 7.5))

    def test_lost_race_when_punch_in_was_changed_meanwhile(self):
        rec = self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        self.fake.attendance_logs.before_update = lambda: rec.update(punch_in=at(ist_ms(2026, 7, 6, 10, 0)))
        self.assertEqual(self.status_of(self.out, ist_ms(2026, 7, 6, 18, 0))[0], 409)
        self.assertIsNone(rec["punch_out"])

    def test_unexpected_database_error_is_not_swallowed(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        self.fake.attendance_logs.force_error = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.out(ist_ms(2026, 7, 6, 18, 0))

    def test_legacy_record_missing_derived_fields(self):
        legacy = {"_id": "legacy", "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                  "punch_in": at(ist_ms(2026, 7, 6, 9, 30))}   # no punch_out/work_hours/late/overtime/half_day/history
        self.fake.attendance_logs.docs.append(legacy)
        r = self.out(ist_ms(2026, 7, 6, 19, 0))
        self.assertEqual(set(r), ATT_KEYS)
        self.assertEqual((r["late_minutes"], r["overtime_minutes"], r["half_day"], r["history"]), (0, 30, False, []))
        self.assertEqual(r["work_hours"], 9.5)

    def test_naive_stored_punch_in_is_treated_as_utc(self):
        self.fake.attendance_logs.docs.append({
            "_id": "naive", "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, 0)})   # naive == 09:30 IST
        r = self.out(ist_ms(2026, 7, 6, 19, 10))
        self.assertEqual((r["work_hours"], r["overtime_minutes"], r["punch_in"]), (9.67, 40, ist_ms(2026, 7, 6, 9, 30)))

    def test_closed_legacy_record_with_null_punch_out_is_open(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30), None)
        self.assertEqual(self.status_of(self.out, ist_ms(2026, 7, 6, 18, 0))[0], 200)


class TestListAttendanceFake(FakeDbCase):
    def setUp(self):
        super().setUp()
        rows = [("EMP0002", "2026-07-06", "PRESENT"), ("EMP0001", "2026-07-06", "WFH"),
                ("EMP0001", "2026-07-07", "PRESENT"), ("EMP0003", "2026-07-07", "ON_DUTY"),
                ("EMP0002", "2026-07-08", "ABSENT"), ("EMP0001", "2026-07-08", "LEAVE"),
                ("EMP0003", "2026-07-05", "PRESENT"), ("EMP0001", "2026-07-05", "PRESENT"),
                ("EMP0002", "2026-07-09", "PRESENT")]
        for code, date, st in rows:    # inserted in a scrambled order on purpose
            y, mo, d = map(int, date.split("-"))
            self.seed(code, date, ist_ms(y, mo, d, 9, 30) if st not in ("ABSENT", "LEAVE") else None, status=st)

    def ls(self, **kw):
        args = dict(emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20)
        args.update(kw)
        return m.list_attendance(**args)

    def keys(self, r):
        return [(i["date"], i["emp_code"]) for i in r["items"]]

    def test_sorted_date_desc_then_emp_code_asc(self):
        r = self.ls()
        self.assertEqual(self.keys(r), [("2026-07-09", "EMP0002"), ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"),
                                        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0003"), ("2026-07-06", "EMP0001"),
                                        ("2026-07-06", "EMP0002"), ("2026-07-05", "EMP0001"), ("2026-07-05", "EMP0003")])
        self.assertEqual((r["total"], r["page"], r["page_size"]), (9, 1, 20))

    def test_filter_sort_and_paging_are_pushed_to_the_database(self):
        self.ls(page=2, page_size=4)
        c = self.fake.attendance_logs.last_cursor
        self.assertEqual((c.sort_spec, c.skip_n, c.limit_n), ([("date", -1), ("emp_code", 1)], 4, 4))
        self.assertIn("count_documents", self.fake.attendance_logs.calls)

    def test_pagination_pages_and_end(self):
        p1, p2, p3 = (self.ls(page=i, page_size=4) for i in (1, 2, 3))
        self.assertEqual((len(p1["items"]), len(p2["items"]), len(p3["items"])), (4, 4, 1))
        self.assertEqual(self.keys(p1) + self.keys(p2) + self.keys(p3), self.keys(self.ls()))
        self.assertEqual(p3["total"], 9)

    def test_out_of_range_page_is_200_empty_with_true_total(self):
        r = self.ls(page=50, page_size=4)
        self.assertEqual((r["items"], r["total"], r["page"]), ([], 9, 50))
        r = self.ls(page=10 ** 30, page_size=100)
        self.assertEqual((r["items"], r["total"]), ([], 9))

    def test_emp_code_filter_exact_case_sensitive(self):
        r = self.ls(emp_code="EMP0001")
        self.assertEqual(r["total"], 4)
        self.assertEqual({d for d, _ in self.keys(r)}, {"2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08"})
        self.assertEqual(self.ls(emp_code="emp0001")["total"], 0)
        self.assertEqual(self.ls(emp_code="EMP000")["total"], 0)

    def test_date_range_inclusive_both_ends(self):
        r = self.ls(date_from="2026-07-06", date_to="2026-07-08")
        self.assertEqual({d for d, _ in self.keys(r)}, {"2026-07-06", "2026-07-07", "2026-07-08"})
        self.assertEqual(r["total"], 6)
        self.assertEqual(self.ls(date_from="2026-07-07", date_to="2026-07-07")["total"], 2)   # single day
        self.assertEqual(self.ls(date_from="2026-07-09")["total"], 1)
        self.assertEqual(self.ls(date_to="2026-07-05")["total"], 2)
        self.assertEqual(self.ls(date_from="2026-08-01")["total"], 0)

    def test_status_filter(self):
        self.assertEqual(self.ls(status="WFH")["total"], 1)
        self.assertEqual(self.ls(status="ABSENT")["total"], 1)
        self.assertEqual(self.ls(status="PRESENT")["total"], 5)

    def test_combined_filters_total_is_the_filtered_count(self):
        r = self.ls(emp_code="EMP0001", date_from="2026-07-06", date_to="2026-07-08", status="PRESENT", page_size=1)
        self.assertEqual((r["total"], len(r["items"])), (1, 1))
        self.assertEqual(self.fake.attendance_logs.count_filters[-1],
                         {"emp_code": "EMP0001", "date": {"$gte": "2026-07-06", "$lte": "2026-07-08"}, "status": "PRESENT"})

    def test_from_after_to_is_a_standard_422_error(self):
        with self.assertRaises(RequestValidationError) as cm:
            self.ls(date_from="2026-07-09", date_to="2026-07-01")
        self.assertEqual(cm.exception.errors()[0]["loc"], ("query", "date_from"))
        self.assertEqual(self.fake.attendance_logs.calls, [])           # rejected before any query

    def test_invalid_dates_are_422(self):
        for bad in ("2026-02-30", "2026-7-6", "20260706", "", "2026-07-06T00:00", "yesterday", "2026-13-01"):
            for param in ("date_from", "date_to"):
                with self.subTest(param=param, value=bad), self.assertRaises(RequestValidationError) as cm:
                    self.ls(**{param: bad})
                self.assertEqual(cm.exception.errors()[0]["loc"], ("query", param))

    def test_items_follow_the_contract_and_leak_nothing(self):
        for item in self.ls()["items"]:
            self.assertEqual(set(item), ATT_KEYS)
            self.assertNotIn("_id", item)
            self.assertNotIn("id", item)
        present = next(i for i in self.ls(emp_code="EMP0002", date_from="2026-07-06", date_to="2026-07-06")["items"])
        self.assertEqual(present["punch_in"], ist_ms(2026, 7, 6, 9, 30))
        self.assertIsInstance(present["punch_in"], int)
        absent = self.ls(status="ABSENT")["items"][0]
        self.assertEqual((absent["punch_in"], absent["punch_out"]), (None, None))

    def test_legacy_documents_serialize_with_defaults(self):
        self.fake.attendance_logs.docs.append({
            "_id": "legacy", "emp_code": "EMP0009", "date": "2026-07-10", "status": "PRESENT",
            "punch_in": at(ist_ms(2026, 7, 10, 9, 30)), "punch_out": at(ist_ms(2026, 7, 10, 18, 0)), "work_hours": 8.5})
        item = self.ls(emp_code="EMP0009")["items"][0]
        self.assertEqual(set(item), ATT_KEYS)
        self.assertEqual((item["half_day"], item["history"], item["late_minutes"], item["overtime_minutes"]),
                         (False, [], 0, 0))
        self.assertEqual(item["punch_out"], ist_ms(2026, 7, 10, 18, 0))

    def test_empty_result(self):
        r = self.ls(emp_code="NOBODY")
        self.assertEqual((r["items"], r["total"]), ([], 0))


# ======================================================================== 3. HTTP on the fake
@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestPhase3Http(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)       # not a context manager: lifespan does not run

    def test_health_200_and_exact_body(self):
        m._indexes_ready = True
        r = self.c.get("/health")
        self.assertEqual((r.status_code, r.json()), (200, {"status": "ok"}))

    def test_health_503_body_has_no_secrets(self):
        self.fake.ping_error = ServerSelectionTimeoutError("mongodb://admin:s3cret@10.0.0.5:27017 timeout")
        r = self.c.get("/health")
        self.assertEqual((r.status_code, r.json()), (503, {"detail": "database unavailable"}))
        self.assertNotIn("s3cret", r.text)
        self.assertNotIn("10.0.0.5", r.text)

    def test_docs_and_openapi_are_served(self):
        self.assertEqual(self.c.get("/docs").status_code, 200)
        paths = self.c.get("/openapi.json").json()["paths"]
        for p in ("/health", "/attendance/punch-out", "/attendance", "/attendance/punch-in", "/employees"):
            self.assertIn(p, paths)

    def test_punch_out_200_and_schema(self):
        self.add_emp("EMP0001")
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 28))
        ms = ist_ms(2026, 7, 6, 19, 10)
        r = self.c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms, "junk": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), ATT_KEYS)
        self.assertEqual((r.json()["punch_out"], r.json()["overtime_minutes"]), (ms, 40))

    def test_punch_out_error_statuses(self):
        self.add_emp("EMP0001")
        base = ist_ms(2026, 7, 6, 9, 30)
        self.seed("EMP0001", "2026-07-06", base)
        post = lambda body: self.c.post("/attendance/punch-out", json=body)
        self.assertEqual(post({"emp_code": "EMP4040", "punched_at": base + 1000}).status_code, 404)
        self.assertEqual(post({"emp_code": "EMP0001", "punched_at": base}).status_code, 422)
        self.assertEqual(post({"emp_code": "EMP0001", "punched_at": base + 86401000}).status_code, 422)
        self.assertEqual(post({"emp_code": "EMP0001", "punched_at": base + 3600000}).status_code, 200)
        r = post({"emp_code": "EMP0001", "punched_at": base + 7200000})
        self.assertEqual((r.status_code, r.json()), (409, {"detail": "already punched out"}))

    def test_punch_out_bad_timestamps_and_bodies_are_422(self):
        self.add_emp("EMP0001")
        for body in ({"emp_code": "EMP0001", "punched_at": 1783312500}, {"emp_code": "EMP0001", "punched_at": 1.5e12},
                     {"emp_code": "EMP0001", "punched_at": "1783312500000"}, {"emp_code": "EMP0001", "punched_at": True},
                     {"emp_code": "EMP0001", "punched_at": None}, {"emp_code": "EMP0001", "punched_at": 4102444800001},
                     {"punched_at": 1783312500000}, {}):
            with self.subTest(body=body):
                r = self.c.post("/attendance/punch-out", json=body)
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)

    def test_list_attendance_validation_422_bodies(self):
        for qs in ("status=SICK", "status=present", "page=0", "page=-1", "page_size=0", "page_size=101",
                   "date_from=2026-02-30", "date_to=nope", "date_from=2026-07-09&date_to=2026-07-01"):
            with self.subTest(qs=qs):
                r = self.c.get("/attendance?" + qs)
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)

    def test_list_attendance_200_shape_and_defaults(self):
        self.seed("EMP0001", "2026-07-06", ist_ms(2026, 7, 6, 9, 30))
        self.seed("EMP0002", "2026-07-07", ist_ms(2026, 7, 7, 9, 30), status="WFH")
        body = self.c.get("/attendance").json()
        self.assertEqual((body["total"], body["page"], body["page_size"]), (2, 1, 20))
        self.assertEqual([i["date"] for i in body["items"]], ["2026-07-07", "2026-07-06"])
        self.assertEqual(self.c.get("/attendance?status=WFH").json()["total"], 1)
        self.assertEqual(self.c.get("/attendance?page_size=100&page=3").json()["items"], [])

    def test_phase2_employee_list_still_exact_case(self):
        self.add_emp("EMP0001", dept="Sales")
        self.assertEqual(self.c.get("/employees?department=Sales").json()["total"], 1)
        self.assertEqual(self.c.get("/employees?department=sales").json()["total"], 0)


# ======================================================================== 4. REAL MongoDB (opt-in)
def _run_parallel(fn, n):
    barrier = threading.Barrier(n)
    results = [None] * n

    def work(i):
        barrier.wait()
        try:
            results[i] = ("ok", fn(i))
        except Exception as e:
            results[i] = ("err", e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    return results


def _code(result):
    kind, val = result
    if kind == "ok":
        return 200
    return val.status_code if isinstance(val, HTTPException) else repr(val)


@unittest.skipUnless(FULL and HAVE_HTTP and TEST_URI,
                     "set TEST_MONGO_URI to run the real-MongoDB integration tests: NOT EXECUTED")
class TestMongoPhase3(unittest.TestCase):
    """REAL MongoDB in a throwaway database (attendance_test_p3_<random>), dropped at the end."""
    PREFIX = "attendance_test_p3_"

    @classmethod
    def setUpClass(cls):
        from pymongo import MongoClient
        cls.client = MongoClient(TEST_URI, tz_aware=True, serverSelectionTimeoutMS=4000)
        cls.client.admin.command("ping")
        cls.dbname = cls.PREFIX + uuid.uuid4().hex[:8]
        cls.tdb = cls.client[cls.dbname]
        cls._orig_db, cls._orig_ready = m.db, m._indexes_ready
        m.db = cls.tdb
        m.ensure_indexes(cls.tdb)
        m._indexes_ready = True

    @classmethod
    def tearDownClass(cls):
        m.db, m._indexes_ready = cls._orig_db, cls._orig_ready
        if cls.dbname.startswith(cls.PREFIX):
            cls.client.drop_database(cls.dbname)
        cls.client.close()

    def setUp(self):
        self.tdb.employees.delete_many({})
        self.tdb.attendance_logs.delete_many({})

    def emp(self, code="EMP0001", start="09:30", end="18:30"):
        return m.create_employee(m.EmployeeIn(emp_code=code, name="N", email="n@x.io", department="D",
                                              shift_start=start, shift_end=end, joined_on="2026-01-05"))

    def pin(self, ms, code="EMP0001", **kw):
        return m.punch_in(m.PunchInIn(emp_code=code, punched_at=ms, **kw))

    def pout(self, ms, code="EMP0001"):
        return m.punch_out(m.PunchOutIn(emp_code=code, punched_at=ms))

    # ---- punch-out against real MongoDB
    def test_punch_in_then_out_stores_bson_dates_and_returns_epoch_ms(self):
        self.emp()
        pin_ms, pout_ms = ist_ms(2026, 7, 6, 9, 41, 1, 700), ist_ms(2026, 7, 6, 19, 10, 0, 400)
        a = self.pin(pin_ms)
        b = self.pout(pout_ms)
        self.assertEqual((a["punch_in"], b["punch_out"]), (pin_ms - 700, pout_ms - 400))
        for v in (b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"], b["overtime_minutes"]):
            self.assertNotIsInstance(v, datetime)
        raw = self.tdb.attendance_logs.find_one({"emp_code": "EMP0001"})
        self.assertIsInstance(raw["punch_in"], datetime)
        self.assertIsNotNone(raw["punch_out"].tzinfo)
        # server-side BSON types, not just what PyMongo decoded
        self.assertEqual(self.tdb.attendance_logs.count_documents(
            {"punch_in": {"$type": "date"}, "punch_out": {"$type": "date"}, "work_hours": {"$type": "double"}}), 1)
        self.assertEqual((b["work_hours"], b["overtime_minutes"], b["late_minutes"], b["half_day"]), (9.48, 40, 11, False))
        self.assertEqual(m.serialize_attendance(raw), b)

    def test_two_simultaneous_punch_outs_exactly_one_200(self):
        self.emp()
        self.pin(ist_ms(2026, 7, 6, 9, 30))
        base = ist_ms(2026, 7, 6, 18, 0)
        res = _run_parallel(lambda i: self.pout(base + i * 1000), 2)
        self.assertEqual(sorted(str(_code(r)) for r in res), ["200", "409"])

    def test_many_simultaneous_punch_outs_exactly_one_wins_and_its_values_persist(self):
        self.emp()
        self.pin(ist_ms(2026, 7, 6, 9, 30))
        base = ist_ms(2026, 7, 6, 18, 0)
        res = _run_parallel(lambda i: self.pout(base + i * 60000), 20)     # 20 different timestamps
        codes = sorted(str(_code(r)) for r in res)
        self.assertEqual(codes, ["200"] + ["409"] * 19, codes)
        winner = next(v for k, v in res if k == "ok")
        raw = self.tdb.attendance_logs.find_one({"emp_code": "EMP0001"})
        self.assertEqual(m.datetime_to_epoch_ms(raw["punch_out"]), winner["punch_out"])
        self.assertEqual(raw["work_hours"], winner["work_hours"])

    def test_sequential_second_punch_out_is_409_and_unrelated_records_untouched(self):
        self.emp()
        self.emp("EMP0002")
        self.pin(ist_ms(2026, 7, 6, 9, 30))
        self.pin(ist_ms(2026, 7, 6, 9, 30), "EMP0002")
        self.pin(ist_ms(2026, 7, 5, 9, 30))
        self.pout(ist_ms(2026, 7, 6, 18, 0))
        with self.assertRaises(HTTPException) as cm:
            self.pout(ist_ms(2026, 7, 6, 19, 0))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.tdb.attendance_logs.count_documents({"punch_out": None}), 2)

    def test_overnight_across_midnight_real_mongo(self):
        self.emp("EMP0005", "22:00", "06:00")
        a = self.pin(ist_ms(2026, 7, 6, 21, 55), "EMP0005")
        self.assertEqual(a["date"], "2026-07-06")
        b = self.pout(ist_ms(2026, 7, 7, 6, 40), "EMP0005")
        self.assertEqual((b["date"], b["work_hours"], b["overtime_minutes"]), ("2026-07-06", 8.75, 40))

    def test_punch_out_error_paths_real_mongo(self):
        self.emp()
        base = ist_ms(2026, 7, 6, 9, 30)
        def code(ms, emp="EMP0001"):
            try:
                self.pout(ms, emp)
                return 200
            except HTTPException as e:
                return e.status_code
        self.assertEqual(code(base, "EMP4040"), 404)             # unknown employee
        self.assertEqual(code(base), 404)                        # no records
        self.pin(base)
        self.assertEqual(code(base - 1000), 422)  # before the only punch-in                 # before the only punch-in (contract selection)
        self.assertEqual(code(base), 422)                        # equal
        self.assertEqual(code(base + 86401000), 422)             # > 24 h
        self.assertEqual(code(base + 86400000), 200)             # exactly 24 h

    def test_legacy_record_punch_out_and_listing_real_mongo(self):
        self.emp()
        self.tdb.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                                             "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=UTC)})
        b = self.pout(ist_ms(2026, 7, 6, 19, 10))
        self.assertEqual((b["late_minutes"], b["history"], b["overtime_minutes"]), (0, [], 40))
        listed = m.list_attendance(emp_code="EMP0001", date_from=None, date_to=None, status=None, page=1, page_size=20)
        self.assertEqual(listed["items"][0], b)

    # ---- attendance listing against real MongoDB
    def seed_many(self):
        import random
        rng = random.Random(11)
        statuses = ["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
        docs = []
        for e in range(1, 31):                       # 30 employees x 100 days = 3000 records
            for d in range(100):
                day = datetime(2026, 4, 1) + timedelta(days=d)
                st = rng.choice(statuses)
                docs.append({"emp_code": f"EMP{e:04d}", "date": day.strftime("%Y-%m-%d"), "status": st,
                             "punch_in": None if st in ("ABSENT", "LEAVE") else datetime(2026, 4, 1, 4, 0, tzinfo=UTC) + timedelta(days=d),
                             "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                             "half_day": False, "history": []})
        rng.shuffle(docs)
        self.tdb.attendance_logs.insert_many(docs)
        return docs

    def expected(self, docs, emp=None, df=None, dt=None, st=None):
        out = [d for d in docs if (emp is None or d["emp_code"] == emp) and (df is None or d["date"] >= df)
               and (dt is None or d["date"] <= dt) and (st is None or d["status"] == st)]
        return sorted(out, key=lambda d: (-int(d["date"].replace("-", "")), d["emp_code"]))

    def test_filters_sort_pagination_totals_match_a_python_oracle(self):
        docs = self.seed_many()
        combos = [dict(), dict(emp="EMP0007"), dict(df="2026-05-01", dt="2026-05-31"), dict(st="WFH"),
                  dict(df="2026-06-10"), dict(dt="2026-04-05"), dict(emp="EMP0003", df="2026-04-10", dt="2026-04-20", st="PRESENT"),
                  dict(df="2026-05-05", dt="2026-05-05")]
        for c in combos:
            exp = self.expected(docs, **c)
            kw = dict(emp_code=c.get("emp"), date_from=c.get("df"), date_to=c.get("dt"), status=c.get("st"))
            got, page = [], 1
            while True:
                r = m.list_attendance(page=page, page_size=100, **kw)
                self.assertEqual(r["total"], len(exp), c)
                got += [(i["date"], i["emp_code"]) for i in r["items"]]
                if len(r["items"]) < 100:
                    break
                page += 1
            with self.subTest(combo=c):
                self.assertEqual(got, [(d["date"], d["emp_code"]) for d in exp])
        far = m.list_attendance(emp_code=None, date_from=None, date_to=None, status=None, page=9999, page_size=20)
        self.assertEqual((far["items"], far["total"]), ([], 3000))

    def test_list_query_plans_use_indexes_per_explain(self):
        self.seed_many()
        combos = [dict(), dict(emp_code="EMP0007"), dict(date_from="2026-05-01", date_to="2026-05-31"), dict(status="WFH"),
                  dict(status="WFH", date_from="2026-05-01", date_to="2026-05-31"),
                  dict(emp_code="EMP0003", date_from="2026-04-10", date_to="2026-04-20")]
        for c in combos:
            q = m.attendance_list_filter(c.get("emp_code"), c.get("date_from"), c.get("date_to"), c.get("status"))
            plan = m.attendance_page_cursor(q, 20, 20).explain()["queryPlanner"]["winningPlan"]
            text = json.dumps(plan, default=str)
            with self.subTest(filter=c, plan=text):
                self.assertIn("IXSCAN", text)
                self.assertNotIn("COLLSCAN", text)
                self.assertNotIn('"stage": "SORT"', text)       # sort served by the index, no blocking sort

    # ---- health against real MongoDB
    def test_health_real_ping_and_real_failure(self):
        from fastapi.testclient import TestClient
        from pymongo import MongoClient
        with TestClient(m.app) as c:                             # runs the real lifespan
            self.assertEqual((c.get("/health").status_code, c.get("/health").json()), (200, {"status": "ok"}))
            self.assertEqual(c.get("/docs").status_code, 200)
        dead = MongoClient("mongodb://user:s3cret@127.0.0.1:1/", tz_aware=True, serverSelectionTimeoutMS=400)
        try:
            m.db, m._indexes_ready = dead["attendance_test_dead"], False
            with TestClient(m.app):          # startup with MongoDB unreachable must not crash
                pass
            r = TestClient(m.app).get("/health")
            self.assertEqual((r.status_code, r.json()), (503, {"detail": "database unavailable"}))
            self.assertNotIn("s3cret", r.text)
        finally:
            m.db, m._indexes_ready = self.tdb, True
            dead.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

