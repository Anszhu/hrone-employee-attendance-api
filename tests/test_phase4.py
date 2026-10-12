"""Phase 4 tests: PATCH /attendance/{emp_code}/{date} (regularization).

Layers:
  1. TestPlan* / TestWrite*   pure functions (stdlib only); run anywhere.
  2. *Fake classes            endpoint function on an IN-MEMORY FAKE (tests/_fakes_p4.py). MOCKED: they prove
                              branching, error mapping, query/update SHAPE - not MongoDB's atomicity.
  3. TestPhase4Http           status codes and exact bodies through TestClient, still on the fake.
  4. TestMongoPhase4          REAL MongoDB, opt-in via TEST_MONGO_URI; throwaway database
                              attendance_test_p4_<random>, dropped afterwards. Never touches MONGO_DB.
"""
import copy
import os
import threading
import unittest
import uuid
import calendar
from datetime import datetime, timezone, timedelta

from tests._loader import load_main, have

m, MODE = load_main()
FULL = MODE == "full"
HAVE_HTTP = FULL and have("httpx")
TEST_URI = os.environ.get("TEST_MONGO_URI")

UTC = timezone.utc
DAY = ("09:30", "18:30")
NIGHT = ("22:00", "06:00")
ATT_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
            "overtime_minutes", "half_day", "history"}
ENTRY_KEYS = {"at", "by", "reason", "changes"}


def utc_ms(y, mo, d, h=0, mi=0, s=0, ms=0):
    return calendar.timegm((y, mo, d, h, mi, s)) * 1000 + ms


def ist_ms(y, mo, d, h=0, mi=0, s=0, ms=0):
    return utc_ms(y, mo, d, h, mi, s, ms) - (5 * 3600 + 30 * 60) * 1000


def at(ms):
    return m.instant_from_epoch_ms(ms)


def base_rec(**over):
    """A closed, late, overtime record on 2026-07-06 for a 09:30-18:30 employee."""
    rec = {"_id": "rec1", "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
           "punch_in": at(ist_ms(2026, 7, 6, 9, 41, 1)), "punch_out": at(ist_ms(2026, 7, 6, 19, 10, 0)),
           "work_hours": 9.48, "late_minutes": 11, "overtime_minutes": 40, "half_day": False, "history": []}
    rec.update(over)
    return rec


def open_rec(**over):
    """An open record punched in exactly at shift start: late 0, nothing derived yet."""
    return base_rec(punch_in=at(ist_ms(2026, 7, 6, 9, 30, 0)), punch_out=None, work_hours=None,
                    late_minutes=0, overtime_minutes=0, **over)


def plan(rec, status=None, pi=None, po=None, shift=DAY):
    return m.plan_regularization(rec, shift[0], shift[1], status,
                                 None if pi is None else at(pi), None if po is None else at(po))


def tos(changes):
    return {k: v["to"] for k, v in changes.items()}


# ======================================================================== 1. pure: the planner
class TestPlanRegularization(unittest.TestCase):
    def rule(self, fn):
        with self.assertRaises(m.RuleError) as cm:
            fn()
        self.assertEqual(cm.exception.status_code, 422)
        self.assertIsInstance(cm.exception.detail, str)
        return cm.exception.detail

    # ---- valid changes: only changed fields are reported, derived fields recomputed
    def test_status_only(self):
        c = plan(base_rec(), status="WFH")
        self.assertEqual(c, {"status": {"from": "PRESENT", "to": "WFH"}})

    def test_punch_in_change_recomputes_late_and_work_hours(self):
        c = plan(base_rec(), pi=ist_ms(2026, 7, 6, 9, 20, 0))
        self.assertEqual(set(c), {"punch_in", "work_hours", "late_minutes"})
        self.assertEqual((c["work_hours"], c["late_minutes"]),
                         ({"from": 9.48, "to": 9.83}, {"from": 11, "to": 0}))
        self.assertEqual(c["punch_in"]["from"], at(ist_ms(2026, 7, 6, 9, 41, 1)))

    def test_punch_out_change_recomputes_work_hours_and_overtime(self):
        c = plan(base_rec(), po=ist_ms(2026, 7, 6, 18, 35, 0))
        self.assertEqual(set(c), {"punch_out", "work_hours", "overtime_minutes"})
        self.assertEqual((tos(c)["work_hours"], tos(c)["overtime_minutes"]), (8.9, 0))

    def test_combined_changes_and_half_day(self):
        c = plan(base_rec(), pi=ist_ms(2026, 7, 6, 9, 0), po=ist_ms(2026, 7, 6, 13, 0))
        self.assertEqual(set(c), {"punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"})
        self.assertEqual((tos(c)["work_hours"], tos(c)["half_day"], tos(c)["late_minutes"], tos(c)["overtime_minutes"]),
                         (4.0, True, 0, 0))
        self.assertNotIn("status", c)

    def test_status_change_together_with_punch_change(self):
        c = plan(base_rec(), status="ON_DUTY", po=ist_ms(2026, 7, 6, 18, 0))
        self.assertEqual(tos(c)["status"], "ON_DUTY")
        self.assertIn("punch_out", c)

    def test_omitted_fields_are_preserved_not_reported(self):
        c = plan(base_rec(), status="WFH")
        self.assertNotIn("punch_in", c)
        self.assertNotIn("punch_out", c)

    # ---- ABSENT / LEAVE
    def test_absent_and_leave_clear_everything(self):
        for st in ("ABSENT", "LEAVE"):
            with self.subTest(status=st):
                c = plan(base_rec(), status=st)
                self.assertEqual(set(c), {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"})
                self.assertEqual(tos(c), {"status": st, "punch_in": None, "punch_out": None, "work_hours": None,
                                          "late_minutes": 0, "overtime_minutes": 0})
                self.assertEqual(c["punch_out"]["from"], at(ist_ms(2026, 7, 6, 19, 10)))

    def test_absent_resets_half_day(self):
        c = plan(base_rec(half_day=True), status="ABSENT")
        self.assertEqual(c["half_day"], {"from": True, "to": False})

    def test_punch_times_with_absent_or_leave_are_422(self):
        for st in ("ABSENT", "LEAVE"):
            for kw in (dict(pi=ist_ms(2026, 7, 6, 9, 0)), dict(po=ist_ms(2026, 7, 6, 18, 0)),
                       dict(pi=ist_ms(2026, 7, 6, 9, 0), po=ist_ms(2026, 7, 6, 18, 0))):
                with self.subTest(status=st, kw=sorted(kw)):
                    self.rule(lambda: plan(base_rec(), status=st, **kw))

    def test_punch_times_when_stored_status_is_absent_and_status_omitted_are_422(self):
        rec = base_rec(status="ABSENT", punch_in=None, punch_out=None, work_hours=None,
                       late_minutes=0, overtime_minutes=0)
        self.rule(lambda: plan(rec, pi=ist_ms(2026, 7, 6, 9, 0)))

    def test_request_check_helper(self):
        m.check_regularization_request("PRESENT", True, True)
        m.check_regularization_request(None, True, True)
        m.check_regularization_request("ABSENT", False, False)
        for st in ("ABSENT", "LEAVE"):
            with self.assertRaises(m.RuleError):
                m.check_regularization_request(st, True, False)

    # ---- presence needs a punch_in
    def test_presence_status_requires_a_punch_in(self):
        rec = base_rec(status="ABSENT", punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0)
        for st in ("PRESENT", "WFH", "ON_DUTY"):
            with self.subTest(status=st):
                self.assertIn("punch_in", self.rule(lambda: plan(rec, status=st)))

    def test_absent_to_present_with_punch_in(self):
        rec = base_rec(status="ABSENT", punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0)
        c = plan(rec, status="PRESENT", pi=ist_ms(2026, 7, 6, 9, 41, 1))
        self.assertEqual(tos(c), {"status": "PRESENT", "punch_in": at(ist_ms(2026, 7, 6, 9, 41, 1)), "late_minutes": 11})

    def test_absent_to_present_with_both_punches(self):
        rec = base_rec(status="LEAVE", punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0)
        c = plan(rec, status="WFH", pi=ist_ms(2026, 7, 6, 9, 30), po=ist_ms(2026, 7, 6, 19, 0))
        self.assertEqual((tos(c)["work_hours"], tos(c)["overtime_minutes"], tos(c)["status"]), (9.5, 30, "WFH"))

    # ---- punch_in must belong to the attendance date (R1)
    def test_punch_in_must_stay_on_the_attendance_date(self):
        for ms in (ist_ms(2026, 7, 7, 9, 0), ist_ms(2026, 7, 5, 23, 59, 59), ist_ms(2026, 7, 7, 0, 0, 0)):
            with self.subTest(ms=ms):
                self.assertIn("date", self.rule(lambda: plan(open_rec(), pi=ms)))

    def test_date_boundaries_are_ist_not_utc(self):
        # 2026-07-05T18:30:00Z is 2026-07-06 00:00:00 IST -> still the record's date
        c = plan(open_rec(), pi=utc_ms(2026, 7, 5, 18, 30, 0))
        self.assertIn("punch_in", c)
        self.rule(lambda: plan(open_rec(), pi=utc_ms(2026, 7, 5, 18, 29, 59)))     # 23:59:59 IST on the 5th
        plan(open_rec(), pi=utc_ms(2026, 7, 6, 18, 29, 59))                          # 23:59:59 IST on the 6th
        self.rule(lambda: plan(open_rec(), pi=utc_ms(2026, 7, 6, 18, 30, 0)))        # 00:00:00 IST on the 7th

    def test_overnight_shift_date_rule(self):
        rec = base_rec(punch_in=at(ist_ms(2026, 7, 6, 21, 55)), punch_out=at(ist_ms(2026, 7, 7, 6, 40)),
                       work_hours=8.75, late_minutes=0, overtime_minutes=40)
        c = plan(rec, pi=ist_ms(2026, 7, 7, 3, 0), shift=NIGHT)     # before 06:00 -> belongs to the 6th
        self.assertEqual((tos(c)["late_minutes"], tos(c)["work_hours"], tos(c)["half_day"]), (300, 3.67, True))
        plan(rec, pi=ist_ms(2026, 7, 7, 5, 59, 59), shift=NIGHT)
        self.rule(lambda: plan(rec, pi=ist_ms(2026, 7, 7, 6, 0, 0), shift=NIGHT))   # 06:00 -> the 7th's shift

    def test_unchanged_stored_punch_in_is_not_revalidated_against_the_date(self):
        # only a SUPPLIED punch_in is checked; a legacy record whose stored value is odd can still get a status fix
        rec = base_rec(punch_in=at(ist_ms(2026, 7, 7, 9, 0)), punch_out=None, work_hours=None,
                       late_minutes=0, overtime_minutes=0)
        self.assertEqual(tos(plan(rec, status="WFH"))["status"], "WFH")

    # ---- punch_out window
    def test_punch_out_equal_or_before_punch_in_is_422(self):
        rec = open_rec()
        pi = ist_ms(2026, 7, 6, 9, 30, 0)
        self.rule(lambda: plan(rec, po=pi))
        self.rule(lambda: plan(rec, po=pi - 1000))
        self.rule(lambda: plan(rec, po=pi - 3600_000))

    def test_new_punch_in_after_the_stored_punch_out_is_422(self):
        self.rule(lambda: plan(base_rec(), pi=ist_ms(2026, 7, 6, 19, 10, 0)))   # equal to stored punch_out
        self.rule(lambda: plan(base_rec(), pi=ist_ms(2026, 7, 6, 19, 10, 1)))   # after it

    def test_exactly_24_hours_ok_and_beyond_is_422(self):
        pi = ist_ms(2026, 7, 6, 9, 30, 0)
        c = plan(open_rec(), po=pi + 86400_000)
        self.assertEqual(tos(c)["work_hours"], 24.0)
        self.assertNotIn("half_day", c)                       # False -> False is not a change
        self.rule(lambda: plan(open_rec(), po=pi + 86401_000))
        self.rule(lambda: plan(open_rec(), po=pi + 3 * 86400_000))

    # ---- recalculation boundaries (R2 - R5)
    def test_late_boundaries(self):
        def late(h, mi, s):
            c = plan(open_rec(), pi=ist_ms(2026, 7, 6, h, mi, s))
            return c.get("late_minutes", {}).get("to", 0)
        self.assertEqual(late(9, 29, 59), 0)
        self.assertEqual(late(9, 40, 0), 0)
        self.assertEqual(late(9, 40, 1), 10)
        self.assertEqual(late(9, 40, 59), 10)
        self.assertEqual(late(9, 41, 0), 11)
        self.assertEqual(late(10, 15, 59), 45)

    def test_work_hours_half_up_and_half_day_boundary(self):
        pi = ist_ms(2026, 7, 6, 9, 30, 0)
        c = plan(open_rec(), po=pi + 14850_000)
        self.assertEqual((tos(c)["work_hours"], tos(c)["half_day"]), (4.13, True))      # 4.125 ties up
        c = plan(open_rec(), po=pi + 16182_000)
        self.assertEqual(tos(c)["work_hours"], 4.5)
        self.assertNotIn("half_day", c)                                                  # False -> False: unchanged
        c = plan(open_rec(), po=pi + 16181_000)
        self.assertEqual((tos(c)["work_hours"], tos(c)["half_day"]), (4.49, True))

    def test_overtime_boundaries(self):
        def ot(h, mi, s):
            c = plan(open_rec(), po=ist_ms(2026, 7, 6, h, mi, s))
            return c.get("overtime_minutes", {}).get("to", 0)
        self.assertEqual(ot(18, 29, 59), 0)
        self.assertEqual(ot(18, 30, 0), 0)
        self.assertEqual(ot(18, 59, 59), 0)
        self.assertEqual(ot(19, 0, 0), 30)
        self.assertEqual(ot(19, 0, 59), 30)
        self.assertEqual(ot(19, 1, 0), 31)

    def test_overnight_overtime(self):
        rec = base_rec(punch_in=at(ist_ms(2026, 7, 6, 21, 55)), punch_out=None, work_hours=None,
                       late_minutes=0, overtime_minutes=0)
        c = plan(rec, po=ist_ms(2026, 7, 7, 6, 40), shift=NIGHT)
        self.assertEqual((tos(c)["overtime_minutes"], tos(c)["work_hours"]), (40, 8.75))

    def test_milliseconds_truncated_before_everything(self):
        c = plan(open_rec(), po=ist_ms(2026, 7, 6, 19, 0, 59, 999))
        self.assertEqual(tos(c)["punch_out"], at(ist_ms(2026, 7, 6, 19, 0, 59)))
        self.assertEqual(tos(c)["punch_out"].microsecond, 0)
        self.assertEqual(tos(c)["overtime_minutes"], 30)

    # ---- no-op
    def test_noop_requests_are_422(self):
        rec = base_rec()
        self.rule(lambda: plan(rec))                                                     # nothing supplied
        self.rule(lambda: plan(rec, status="PRESENT"))                                   # same status
        self.rule(lambda: plan(rec, pi=ist_ms(2026, 7, 6, 9, 41, 1)))                    # same punch_in
        self.rule(lambda: plan(rec, po=ist_ms(2026, 7, 6, 19, 10, 0)))                   # same punch_out
        self.rule(lambda: plan(rec, status="PRESENT", pi=ist_ms(2026, 7, 6, 9, 41, 1), po=ist_ms(2026, 7, 6, 19, 10)))
        self.rule(lambda: plan(rec, po=ist_ms(2026, 7, 6, 19, 10, 0, 900)))              # same second after truncation

    def test_noop_absent_to_absent(self):
        rec = base_rec(status="ABSENT", punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0)
        self.rule(lambda: plan(rec, status="ABSENT"))

    def test_noop_even_if_stored_derived_values_are_inconsistent(self):
        # the request changes none of status / punch_in / punch_out, so nothing is silently "fixed"
        self.rule(lambda: plan(base_rec(late_minutes=99, work_hours=1.0), status="PRESENT"))

    # ---- legacy documents
    def test_legacy_record_without_optional_fields(self):
        legacy = {"_id": "l", "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                  "punch_in": at(ist_ms(2026, 7, 6, 9, 41, 1)), "punch_out": at(ist_ms(2026, 7, 6, 19, 10)),
                  "work_hours": 9.48}                      # no late / overtime / half_day / history
        c = plan(legacy, status="WFH")
        self.assertEqual(c["status"], {"from": "PRESENT", "to": "WFH"})
        # missing fields read as their defaults, so these are reported as real changes from 0:
        self.assertEqual(c["late_minutes"], {"from": 0, "to": 11})
        self.assertEqual(c["overtime_minutes"], {"from": 0, "to": 40})
        self.assertNotIn("half_day", c)
        self.assertNotIn("work_hours", c)

    def test_naive_stored_datetimes_are_utc(self):
        naive = base_rec(punch_in=datetime(2026, 7, 6, 4, 11, 1), punch_out=datetime(2026, 7, 6, 13, 40, 0))
        c = plan(naive, status="WFH")
        self.assertEqual(set(c), {"status"})
        self.rule(lambda: plan(naive, pi=ist_ms(2026, 7, 6, 9, 41, 1)))        # same instant as the naive UTC value
        c = plan(naive, po=ist_ms(2026, 7, 6, 18, 0))
        self.assertEqual(c["punch_out"]["from"], datetime(2026, 7, 6, 13, 40, tzinfo=UTC))   # reported as aware UTC


class TestWriteBuilders(unittest.TestCase):
    NOW = datetime(2026, 7, 8, 5, 0, 0, 123456, tzinfo=UTC)

    def test_history_entry_shape_and_server_time(self):
        changes = {"status": {"from": "PRESENT", "to": "WFH"}}
        e = m.build_history_entry(self.NOW, "hr.admin", "client forgot", changes)
        self.assertEqual(set(e), ENTRY_KEYS)
        self.assertEqual(e["at"], datetime(2026, 7, 8, 5, 0, 0, 123000, tzinfo=UTC))   # BSON ms precision
        self.assertEqual((e["by"], e["reason"], e["changes"]), ("hr.admin", "client forgot", changes))

    def test_write_for_a_record_with_history_pushes(self):
        rec = base_rec(history=[{"x": 1}, {"x": 2}])
        changes = {"status": {"from": "PRESENT", "to": "WFH"}}
        entry = m.build_history_entry(self.NOW, "b", "reason!", changes)
        flt, upd = m.build_regularization_write(rec, changes, entry)
        self.assertEqual(flt, {"_id": "rec1", "status": "PRESENT", "punch_in": rec["punch_in"],
                               "punch_out": rec["punch_out"], "history": {"$size": 2}})
        self.assertEqual(upd, {"$set": {"status": "WFH"}, "$push": {"history": entry}})

    def test_write_for_a_legacy_record_starts_the_trail(self):
        entry = {"at": self.NOW, "by": "b", "reason": "reason!", "changes": {}}
        for rec in (base_rec(history=None), {k: v for k, v in base_rec().items() if k != "history"}):
            flt, upd = m.build_regularization_write(rec, {"status": {"from": "PRESENT", "to": "WFH"}}, entry)
            self.assertIsNone(flt["history"])                      # matches missing or null
            self.assertEqual(upd, {"$set": {"status": "WFH", "history": [entry]}})
            self.assertNotIn("$push", upd)

    def test_filter_pins_an_open_record_with_none(self):
        flt, _ = m.build_regularization_write(open_rec(), {"punch_out": {"from": None, "to": at(1_800_000_000_000)}},
                                              {"at": self.NOW})
        self.assertIsNone(flt["punch_out"])
        self.assertIn("punch_in", flt)

    def test_set_contains_only_changed_fields(self):
        c = plan(base_rec(), po=ist_ms(2026, 7, 6, 18, 35))
        _, upd = m.build_regularization_write(base_rec(), c, {"at": self.NOW})
        self.assertEqual(set(upd["$set"]), {"punch_out", "work_hours", "overtime_minutes"})


# ======================================================================== 2. fake-DB endpoint tests
if FULL:
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from tests._fakes_p4 import FakeDBP4


@unittest.skipUnless(FULL, "needs fastapi/pydantic/pymongo installed: NOT EXECUTED")
class FakeDbCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDBP4()
        self._orig_db = m.db
        m.db = self.fake
        self.add_emp("EMP0001")

    def tearDown(self):
        m.db = self._orig_db

    def add_emp(self, code, start="09:30", end="18:30"):
        self.fake.employees.docs.append({
            "_id": "e-" + code, "emp_code": code, "name": "N", "email": "n@x.io", "department": "D",
            "shift_start": start, "shift_end": end, "joined_on": "2026-01-05",
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)})

    def seed(self, rec=None, **over):
        rec = rec or base_rec(**over)
        self.fake.attendance_logs.docs.append(copy.deepcopy(rec))
        return rec

    def stored(self, code="EMP0001", date="2026-07-06"):
        return next(d for d in self.fake.attendance_logs.docs if d["emp_code"] == code and d["date"] == date)

    def patch(self, code="EMP0001", date="2026-07-06", reason="fixing it", by="hr.admin", **fields):
        return m.regularize_attendance(code, date, m.RegularizeIn(reason=reason, regularized_by=by, **fields))

    def status_of(self, fn, *a, **kw):
        try:
            return 200, fn(*a, **kw)
        except HTTPException as e:
            return e.status_code, e.detail


class TestRegularizeFake(FakeDbCase):
    def test_status_only_response_schema_and_history(self):
        self.seed()
        before = utc_ms(*datetime.now(UTC).timetuple()[:6])
        r = self.patch(status="WFH")
        self.assertEqual(set(r), ATT_KEYS)
        self.assertEqual(r["status"], "WFH")
        self.assertNotIn("_id", r)
        self.assertNotIn("id", r)
        self.assertEqual(len(r["history"]), 1)
        e = r["history"][0]
        self.assertEqual(set(e), ENTRY_KEYS)
        self.assertEqual((e["by"], e["reason"]), ("hr.admin", "fixing it"))
        self.assertEqual(e["changes"], {"status": {"from": "PRESENT", "to": "WFH"}})
        self.assertIsInstance(e["at"], int)
        self.assertGreaterEqual(e["at"], before - 1000)
        self.assertEqual((r["punch_in"], r["punch_out"]), (ist_ms(2026, 7, 6, 9, 41, 1), ist_ms(2026, 7, 6, 19, 10)))

    def test_storage_keeps_bson_datetimes_response_has_epoch_ms(self):
        self.seed()
        r = self.patch(punch_in=ist_ms(2026, 7, 6, 9, 20), punch_out=ist_ms(2026, 7, 6, 13, 0))
        s = self.stored()
        for f in ("punch_in", "punch_out"):
            self.assertIsInstance(s[f], datetime)
            self.assertEqual(s[f].utcoffset(), timedelta(0))
        e = s["history"][0]
        self.assertIsInstance(e["at"], datetime)
        self.assertEqual(e["at"].utcoffset(), timedelta(0))
        self.assertIsInstance(e["changes"]["punch_in"]["from"], datetime)      # BSON dates inside the nested object
        self.assertIsInstance(e["changes"]["punch_out"]["to"], datetime)
        api = r["history"][0]
        self.assertEqual(api["changes"]["punch_in"], {"from": ist_ms(2026, 7, 6, 9, 41, 1), "to": ist_ms(2026, 7, 6, 9, 20)})
        self.assertIsInstance(api["at"], int)

    def test_changes_contain_only_fields_that_changed(self):
        self.seed()
        r = self.patch(punch_out=ist_ms(2026, 7, 6, 18, 35))
        self.assertEqual(set(r["history"][0]["changes"]), {"punch_out", "work_hours", "overtime_minutes"})
        self.assertEqual((r["work_hours"], r["overtime_minutes"], r["late_minutes"], r["half_day"]), (8.9, 0, 11, False))

    def test_combined_change_recalculates_all_derived_fields(self):
        self.seed()
        r = self.patch(status="ON_DUTY", punch_in=ist_ms(2026, 7, 6, 9, 0), punch_out=ist_ms(2026, 7, 6, 13, 0))
        self.assertEqual((r["status"], r["work_hours"], r["late_minutes"], r["overtime_minutes"], r["half_day"]),
                         ("ON_DUTY", 4.0, 0, 0, True))
        self.assertEqual(set(r["history"][0]["changes"]), {"status", "punch_in", "punch_out", "work_hours",
                                                           "late_minutes", "overtime_minutes", "half_day"})

    def test_absent_and_leave_clear_punches_and_reset_derived_fields(self):
        for st in ("ABSENT", "LEAVE"):
            with self.subTest(status=st):
                self.fake.attendance_logs.docs.clear()
                self.seed(half_day=True)
                r = self.patch(status=st)
                self.assertEqual((r["status"], r["punch_in"], r["punch_out"], r["work_hours"], r["late_minutes"],
                                  r["overtime_minutes"], r["half_day"]), (st, None, None, None, 0, 0, False))
                s = self.stored()
                self.assertEqual((s["punch_in"], s["punch_out"], s["work_hours"]), (None, None, None))

    def test_punch_times_with_absent_leave_are_rejected_by_the_request_model_and_write_nothing(self):
        """ABSENT/LEAVE combined with punch_in/punch_out is rejected when the request model is built
        (FastAPI turns that ValidationError into HTTP 422 - see TestPhase4Http), so the endpoint, and with it
        the database, is never reached."""
        from pydantic import ValidationError
        before = copy.deepcopy(self.seed())
        for st in ("ABSENT", "LEAVE"):
            for kw in (dict(punch_in=ist_ms(2026, 7, 6, 9, 0)), dict(punch_out=ist_ms(2026, 7, 6, 18, 0))):
                with self.subTest(status=st, kw=sorted(kw)):
                    with self.assertRaises(ValidationError) as cm:
                        m.RegularizeIn(reason="fixing it", regularized_by="hr.admin", status=st, **kw)
                    self.assertIn("cannot be supplied", cm.exception.errors()[0]["msg"])
        # nothing was called, nothing was written, history and the stored record are untouched
        self.assertEqual(self.fake.attendance_logs.calls, [])
        self.assertEqual(self.fake.employees.calls, [])
        self.assertNotIn("find_one_and_update", self.fake.attendance_logs.calls)
        self.assertEqual(self.stored()["history"], [])
        self.assertEqual(self.stored(), before)

    def test_missing_punch_in_for_a_presence_status_is_422(self):
        self.seed(status="ABSENT", punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0)
        code, detail = self.status_of(self.patch, status="PRESENT")
        self.assertEqual(code, 422)
        self.assertIn("punch_in", detail)

    def test_rule_violations_are_422_and_write_nothing(self):
        self.seed()
        pi, po = ist_ms(2026, 7, 6, 9, 41, 1), ist_ms(2026, 7, 6, 19, 10)
        bad = [dict(punch_out=pi), dict(punch_out=pi - 1000), dict(punch_in=po), dict(punch_in=po + 1000),
               dict(punch_in=ist_ms(2026, 7, 7, 9, 0)), dict(punch_in=ist_ms(2026, 7, 5, 23, 59, 59)),
               dict(punch_out=pi + 86401_000)]
        for kw in bad:
            with self.subTest(kw=kw):
                self.assertEqual(self.status_of(self.patch, **kw)[0], 422)
        self.assertEqual(self.stored()["history"], [])
        self.assertNotIn("find_one_and_update", self.fake.attendance_logs.calls)

    def test_duration_exactly_24_hours_is_allowed(self):
        self.seed(punch_out=None, work_hours=None, overtime_minutes=0)
        base = ist_ms(2026, 7, 6, 9, 41, 1)
        self.assertEqual(self.status_of(self.patch, punch_out=base + 86400_000)[0], 200)
        self.fake.attendance_logs.docs.clear()
        self.seed(punch_out=None, work_hours=None, overtime_minutes=0)
        self.assertEqual(self.status_of(self.patch, punch_out=base + 86401_000)[0], 422)

    def test_noop_is_422_and_creates_no_history(self):
        self.seed()
        for kw in (dict(), dict(status="PRESENT"), dict(punch_in=ist_ms(2026, 7, 6, 9, 41, 1)),
                   dict(punch_out=ist_ms(2026, 7, 6, 19, 10)), dict(punch_out=ist_ms(2026, 7, 6, 19, 10, 0, 500))):
            with self.subTest(kw=kw):
                self.assertEqual(self.status_of(self.patch, **kw)[0], 422)
        self.assertEqual(self.stored()["history"], [])
        self.assertNotIn("find_one_and_update", self.fake.attendance_logs.calls)

    def test_unknown_employee_is_404(self):
        self.seed()
        self.assertEqual(self.status_of(self.patch, "EMP9999", status="WFH"), (404, "employee not found"))
        self.assertEqual(self.fake.attendance_logs.calls, [])

    def test_missing_attendance_record_is_404(self):
        self.seed()
        self.assertEqual(self.status_of(self.patch, "EMP0001", "2026-07-07", status="WFH"), (404, "attendance record not found"))
        self.add_emp("EMP0002")
        self.assertEqual(self.status_of(self.patch, "EMP0002", "2026-07-06", status="WFH")[0], 404)

    def test_invalid_path_dates_are_validation_errors_before_any_lookup(self):
        for bad in ("2026-02-30", "2026-7-6", "20260706", "yesterday", "2026-13-01", "2026-07-06T00:00", "0000-01-01"):
            with self.subTest(date=bad), self.assertRaises(RequestValidationError) as cm:
                self.patch(date=bad, status="WFH")
            self.assertEqual(cm.exception.errors()[0]["loc"], ("path", "date"))
        self.assertEqual(self.fake.employees.calls, [])

    def test_existing_history_is_preserved_and_one_entry_appended(self):
        old = [{"at": at(utc_ms(2026, 7, 7, 5, 0)), "by": "a", "reason": "first fix",
                "changes": {"late_minutes": {"from": 35, "to": 11}}},
               {"at": at(utc_ms(2026, 7, 7, 6, 0)), "by": "b", "reason": "second fix", "changes": {}}]
        self.seed(history=copy.deepcopy(old))
        r = self.patch(status="WFH")
        s = self.stored()["history"]
        self.assertEqual(len(s), 3)
        self.assertEqual(s[:2], old)
        self.assertEqual(len(r["history"]), 3)
        self.assertEqual(r["history"][0]["changes"], {"late_minutes": {"from": 35, "to": 11}})
        self.patch(status="ON_DUTY")
        self.assertEqual(len(self.stored()["history"]), 4)

    def test_consecutive_patches_chain_old_to_new(self):
        self.seed()
        self.patch(punch_out=ist_ms(2026, 7, 6, 18, 35))
        self.patch(punch_out=ist_ms(2026, 7, 6, 19, 5))
        h = self.stored()["history"]
        self.assertEqual(h[1]["changes"]["punch_out"]["from"], h[0]["changes"]["punch_out"]["to"])

    def test_client_supplied_server_fields_are_ignored(self):
        self.seed()
        body = m.RegularizeIn(reason="fixing it", regularized_by="hr.admin", status="WFH",
                              history=[{"at": 1, "by": "evil"}], created_at=1, _id="evil", id="evil",
                              late_minutes=99, work_hours=1.0, overtime_minutes=77, half_day=True, date="2000-01-01")
        r = m.regularize_attendance("EMP0001", "2026-07-06", body)       # path date wins; body `date` is ignored
        self.assertEqual((r["late_minutes"], r["work_hours"], r["overtime_minutes"], r["half_day"], r["date"]),
                         (11, 9.48, 40, False, "2026-07-06"))
        self.assertEqual(len(r["history"]), 1)
        self.assertEqual(r["history"][0]["by"], "hr.admin")
        s = self.stored()
        self.assertEqual(s["_id"], "rec1")
        self.assertNotIn("created_at", s)

    def test_unrelated_records_are_untouched(self):
        self.add_emp("EMP0002")
        self.seed()
        other1 = self.seed(base_rec(_id="r2", emp_code="EMP0002"))
        other2 = self.seed(base_rec(_id="r3", date="2026-07-07", punch_in=at(ist_ms(2026, 7, 7, 9, 41, 1)),
                                    punch_out=at(ist_ms(2026, 7, 7, 19, 10))))
        self.patch(status="WFH")
        self.assertEqual(self.stored("EMP0002"), other1)
        self.assertEqual(self.stored("EMP0001", "2026-07-07"), other2)

    def test_unrelated_fields_survive(self):
        self.seed(note="keep me", shift_tag="x")
        self.patch(status="WFH")
        s = self.stored()
        self.assertEqual((s["note"], s["shift_tag"]), ("keep me", "x"))

    def test_legacy_record_missing_optional_fields(self):
        legacy = {"_id": "l", "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                  "punch_in": at(ist_ms(2026, 7, 6, 9, 41, 1)), "punch_out": at(ist_ms(2026, 7, 6, 19, 10)), "work_hours": 9.48}
        self.seed(legacy)
        r = self.patch(status="WFH")
        self.assertEqual(set(r), ATT_KEYS)
        self.assertEqual(len(r["history"]), 1)
        self.assertEqual(self.stored()["history"][0]["by"], "hr.admin")     # the trail was started

    def test_legacy_record_with_null_history(self):
        self.seed(history=None)
        r = self.patch(status="WFH")
        self.assertEqual(len(r["history"]), 1)

    def test_naive_stored_punch_times_work(self):
        self.seed(punch_in=datetime(2026, 7, 6, 4, 11, 1), punch_out=datetime(2026, 7, 6, 13, 40, 0))
        r = self.patch(status="WFH")
        self.assertEqual((r["punch_in"], r["punch_out"]), (ist_ms(2026, 7, 6, 9, 41, 1), ist_ms(2026, 7, 6, 19, 10)))

    def test_overnight_employee_through_the_endpoint(self):
        self.add_emp("EMP0005", "22:00", "06:00")
        self.seed(base_rec(emp_code="EMP0005", punch_in=at(ist_ms(2026, 7, 6, 21, 55)),
                           punch_out=at(ist_ms(2026, 7, 7, 6, 40)), work_hours=8.75, late_minutes=0, overtime_minutes=40))
        self.assertEqual(self.status_of(self.patch, "EMP0005", "2026-07-06", punch_in=ist_ms(2026, 7, 7, 6, 0))[0], 422)
        r = self.patch("EMP0005", "2026-07-06", punch_in=ist_ms(2026, 7, 7, 3, 0))
        self.assertEqual((r["late_minutes"], r["work_hours"], r["half_day"]), (300, 3.67, True))

    def test_update_is_one_conditional_write_pinned_to_the_state_read(self):
        self.seed(history=[{"x": 1}])
        self.patch(status="WFH")
        flt, upd = self.fake.attendance_logs.last_update
        self.assertEqual(flt["history"], {"$size": 1})
        self.assertEqual(flt["_id"], "rec1")
        self.assertIn("punch_out", flt)
        self.assertIn("$push", upd)
        self.assertEqual(self.fake.attendance_logs.calls.count("find_one_and_update"), 1)

    # ---- lost races (simulated: MOCK of the interleaving, not of MongoDB's atomicity)
    def test_concurrent_patch_wins_first_loser_gets_409_and_adds_no_history(self):
        rec = self.seed()
        winner_entry = {"at": at(utc_ms(2026, 7, 8, 5, 0)), "by": "other", "reason": "winner", "changes": {}}

        def other_patch_commits():
            d = self.stored()
            d["status"] = "ON_DUTY"
            d["history"].append(winner_entry)
        self.fake.attendance_logs.before_update = other_patch_commits
        self.assertEqual(self.status_of(self.patch, status="WFH")[0], 409)
        s = self.stored()
        self.assertEqual((s["status"], s["history"]), ("ON_DUTY", [winner_entry]))   # winner intact, nothing from the loser

    def test_concurrent_punch_out_change_makes_the_patch_lose(self):
        self.seed(punch_out=None, work_hours=None, overtime_minutes=0)
        self.fake.attendance_logs.before_update = lambda: self.stored().update(punch_out=at(ist_ms(2026, 7, 6, 17, 0)))
        self.assertEqual(self.status_of(self.patch, status="WFH")[0], 409)
        self.assertEqual(self.stored()["history"], [])
        self.assertEqual(self.stored()["status"], "PRESENT")

    def test_409_loser_is_not_retried_so_no_duplicate_entry(self):
        self.seed()
        self.fake.attendance_logs.before_update = lambda: self.stored()["history"].append({"x": "winner"})
        self.status_of(self.patch, status="WFH")
        self.assertEqual(self.fake.attendance_logs.calls.count("find_one_and_update"), 1)

    def test_unexpected_database_error_is_not_swallowed(self):
        self.seed()
        self.fake.attendance_logs.force_error = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.patch(status="WFH")


# ======================================================================== 3. HTTP on the fake
@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestPhase4Http(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)

    def req(self, body, code="EMP0001", date="2026-07-06"):
        return self.c.patch(f"/attendance/{code}/{date}", json=body)

    GOOD = {"reason": "client forgot", "regularized_by": "hr.admin"}

    def test_200_exact_schema(self):
        self.seed()
        r = self.req({**self.GOOD, "status": "WFH", "punch_out": ist_ms(2026, 7, 6, 18, 35)})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(set(b), ATT_KEYS)
        self.assertEqual(set(b["history"][0]), ENTRY_KEYS)
        self.assertEqual(b["history"][0]["changes"]["status"], {"from": "PRESENT", "to": "WFH"})
        self.assertIsInstance(b["history"][0]["at"], int)
        self.assertIsInstance(b["punch_out"], int)
        self.assertEqual((b["status"], b["work_hours"], b["overtime_minutes"]), ("WFH", 8.9, 0))
        self.assertNotIn("_id", r.text)
        self.assertNotIn('"id"', r.text)

    def test_422_body_validation(self):
        self.seed()
        cases = [
            {}, {"regularized_by": "x"}, {"reason": "long enough"},
            {**self.GOOD, "reason": "four"}, {**self.GOOD, "reason": "x" * 201}, {**self.GOOD, "reason": 12345},
            {**self.GOOD, "regularized_by": ""}, {**self.GOOD, "regularized_by": "y" * 51},
            {**self.GOOD, "status": "SICK"}, {**self.GOOD, "status": "present"}, {**self.GOOD, "status": None},
            {**self.GOOD, "punch_in": 1783312500}, {**self.GOOD, "punch_in": 1.5e12}, {**self.GOOD, "punch_in": "1783312500000"},
            {**self.GOOD, "punch_in": True}, {**self.GOOD, "punch_in": None}, {**self.GOOD, "punch_in": 4102444800001},
            {**self.GOOD, "punch_out": 1783312500}, {**self.GOOD, "punch_out": False}, {**self.GOOD, "punch_out": None},
            {**self.GOOD, "status": "ABSENT", "punch_in": ist_ms(2026, 7, 6, 9, 0)},
            {**self.GOOD, "status": "LEAVE", "punch_out": ist_ms(2026, 7, 6, 18, 0)},
        ]
        for body in cases:
            with self.subTest(body=body):
                r = self.req(body)
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)
        self.assertEqual(self.stored()["history"], [])

    def test_boundary_lengths_are_accepted(self):
        for i, (reason, by) in enumerate([("12345", "y"), ("x" * 200, "y" * 50)]):
            self.fake.attendance_logs.docs.clear()
            self.seed()
            self.assertEqual(self.req({"reason": reason, "regularized_by": by, "status": "WFH"}).status_code, 200)

    def test_422_invalid_path_date_has_standard_body(self):
        self.seed()
        for bad in ("2026-02-30", "2026-7-6", "20260706", "not-a-date"):
            with self.subTest(date=bad):
                r = self.req({**self.GOOD, "status": "WFH"}, date=bad)
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)

    def test_rule_422_noop_and_window(self):
        self.seed()
        r = self.req(self.GOOD)
        self.assertEqual((r.status_code, isinstance(r.json()["detail"], str)), (422, True))
        r = self.req({**self.GOOD, "punch_out": ist_ms(2026, 7, 6, 9, 41, 1)})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.req({**self.GOOD, "punch_out": ist_ms(2026, 7, 8, 12, 0)}).status_code, 422)

    def test_404s(self):
        self.seed()
        r = self.req({**self.GOOD, "status": "WFH"}, code="EMP4040")
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "employee not found"}))
        r = self.req({**self.GOOD, "status": "WFH"}, date="2026-07-09")
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "attendance record not found"}))

    def test_validation_runs_before_lookup(self):
        r = self.req({"reason": "x"}, code="EMP4040")
        self.assertEqual(r.status_code, 422)

    def test_409_on_lost_race(self):
        self.seed()
        self.fake.attendance_logs.before_update = lambda: self.stored()["history"].append({"x": 1})
        r = self.req({**self.GOOD, "status": "WFH"})
        self.assertEqual(r.status_code, 409)
        self.assertIsInstance(r.json()["detail"], str)

    def test_extra_client_fields_ignored(self):
        self.seed()
        r = self.req({**self.GOOD, "status": "WFH", "history": [], "created_at": 5, "_id": "x", "late_minutes": 99})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["late_minutes"], 11)
        self.assertEqual(len(r.json()["history"]), 1)

    def test_openapi_lists_the_route(self):
        self.assertIn("patch", self.c.get("/openapi.json").json()["paths"]["/attendance/{emp_code}/{date}"])


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
class TestMongoPhase4(unittest.TestCase):
    """REAL MongoDB in a throwaway database (attendance_test_p4_<random>), dropped at the end."""
    PREFIX = "attendance_test_p4_"

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
        m.create_employee(m.EmployeeIn(emp_code="EMP0001", name="N", email="n@x.io", department="D", joined_on="2026-01-05"))

    def patch(self, date="2026-07-06", code="EMP0001", reason="fixing it", by="hr.admin", **fields):
        return m.regularize_attendance(code, date, m.RegularizeIn(reason=reason, regularized_by=by, **fields))

    def raw(self, date="2026-07-06", code="EMP0001"):
        return self.tdb.attendance_logs.find_one({"emp_code": code, "date": date})

    def punch_flow(self, date_ms=(2026, 7, 6)):
        y, mo, d = date_ms
        m.punch_in(m.PunchInIn(emp_code="EMP0001", punched_at=ist_ms(y, mo, d, 9, 41, 1)))
        return m.punch_out(m.PunchOutIn(emp_code="EMP0001", punched_at=ist_ms(y, mo, d, 19, 10)))

    def test_bson_types_in_storage_and_epoch_ms_in_responses(self):
        self.punch_flow()
        r = self.patch(punch_in=ist_ms(2026, 7, 6, 9, 20), status="WFH")
        self.assertEqual((r["work_hours"], r["late_minutes"], r["overtime_minutes"], r["status"]), (9.83, 0, 40, "WFH"))
        e = r["history"][0]
        self.assertIsInstance(e["at"], int)
        self.assertEqual(e["changes"]["punch_in"], {"from": ist_ms(2026, 7, 6, 9, 41, 1), "to": ist_ms(2026, 7, 6, 9, 20)})
        T = self.tdb.attendance_logs
        q = lambda path: T.count_documents({path: {"$type": "date"}})
        self.assertEqual((q("punch_in"), q("punch_out"), q("history.0.at"), q("history.0.changes.punch_in.from"),
                          q("history.0.changes.punch_in.to")), (1, 1, 1, 1, 1))
        self.assertEqual(T.count_documents({"work_hours": {"$type": "double"}, "late_minutes": {"$type": "int"}}), 1)
        self.assertEqual(m.serialize_attendance(self.raw()), r)

    def test_history_accumulates_and_is_never_overwritten(self):
        self.punch_flow()
        for i, hh in enumerate((18, 19, 20)):
            self.patch(punch_out=ist_ms(2026, 7, 6, hh, 5), reason=f"edit number {i}")
        h = self.raw()["history"]
        self.assertEqual([e["reason"] for e in h], ["edit number 0", "edit number 1", "edit number 2"])
        for prev, nxt in zip(h, h[1:]):
            self.assertEqual(prev["changes"]["punch_out"]["to"], nxt["changes"]["punch_out"]["from"])

    def test_noop_and_422s_write_nothing(self):
        self.punch_flow()
        for kw in (dict(), dict(status="PRESENT"), dict(punch_out=ist_ms(2026, 7, 6, 9, 41, 1)),
                   dict(status="ABSENT", punch_in=ist_ms(2026, 7, 6, 9, 0))):
            with self.subTest(kw=kw):
                try:
                    self.patch(**kw)
                    self.fail("expected an error")
                except (HTTPException, ValueError) as e:
                    self.assertIn(getattr(e, "status_code", 422), (422,))
        self.assertEqual(self.raw()["history"], [])

    def test_404s_real_mongo(self):
        self.punch_flow()
        for args, kw in ((("2026-07-06", "EMP4040"), {}), (("2026-07-09",), {})):
            with self.assertRaises(HTTPException) as cm:
                self.patch(*args, status="WFH")
            self.assertEqual(cm.exception.status_code, 404)

    def test_legacy_documents_real_mongo(self):
        base = {"emp_code": "EMP0001", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 4, 11, 1, tzinfo=UTC),
                "punch_out": datetime(2026, 7, 6, 13, 40, tzinfo=UTC), "work_hours": 9.48}
        self.tdb.attendance_logs.insert_one({**base, "date": "2026-07-06"})                      # no history at all
        self.tdb.attendance_logs.insert_one({**base, "date": "2026-07-07", "history": None})     # null history
        for date in ("2026-07-06", "2026-07-07"):
            y, mo, d = map(int, date.split("-"))
            self.tdb.attendance_logs.update_one({"date": date}, {"$set": {
                "punch_in": at(ist_ms(y, mo, d, 9, 41, 1)), "punch_out": at(ist_ms(y, mo, d, 19, 10))}})
            r = self.patch(date, status="WFH")
            self.assertEqual(len(r["history"]), 1)
            self.assertEqual(len(self.raw(date)["history"]), 1)
            r2 = self.patch(date, status="ON_DUTY")                  # now a real array: $push path
            self.assertEqual(len(r2["history"]), 2)

    def test_unrelated_records_untouched_real_mongo(self):
        self.punch_flow()
        m.punch_in(m.PunchInIn(emp_code="EMP0001", punched_at=ist_ms(2026, 7, 7, 9, 30)))
        before = self.raw("2026-07-07")
        self.patch(status="WFH")
        self.assertEqual(self.raw("2026-07-07"), before)

    # ---- concurrency
    def test_concurrent_distinct_patches_lose_no_history_and_no_update(self):
        self.punch_flow()
        n = 16
        res = _run_parallel(lambda i: self.patch(punch_out=ist_ms(2026, 7, 6, 19, 11 + i), reason=f"reason-{i:02d}"), n)
        codes = [_code(r) for r in res]
        self.assertTrue(set(codes) <= {200, 409}, codes)
        wins = [(i, v) for i, (k, v) in enumerate(res) if k == "ok"]
        self.assertGreaterEqual(len(wins), 1)
        h = self.raw()["history"]
        self.assertEqual(len(h), len(wins), (codes, len(h)))                       # exactly one entry per success
        self.assertEqual(sorted(e["reason"] for e in h), sorted(f"reason-{i:02d}" for i, _ in wins))
        prev = at(ist_ms(2026, 7, 6, 19, 10))
        for e in h:                                                                # a consistent chain: no lost update
            self.assertEqual(e["changes"]["punch_out"]["from"], prev)
            prev = e["changes"]["punch_out"]["to"]
        final = self.raw()
        self.assertEqual(final["punch_out"], prev)
        self.assertEqual(final["work_hours"], m.compute_work_hours(final["punch_in"], final["punch_out"]))

    def test_concurrent_identical_patch_appends_exactly_one_entry(self):
        self.punch_flow()
        res = _run_parallel(lambda i: self.patch(status="WFH", reason=f"same change {i}"), 12)
        codes = [_code(r) for r in res]
        self.assertEqual(codes.count(200), 1, codes)
        self.assertTrue(set(codes) <= {200, 409, 422}, codes)                       # 409 raced, 422 = saw the winner
        self.assertEqual(len(self.raw()["history"]), 1)
        self.assertEqual(self.raw()["status"], "WFH")

    def test_concurrent_patch_vs_punch_out_never_leaves_stale_derived_fields(self):
        for trial in range(20):
            date = f"2026-08-{trial + 1:02d}"
            d = trial + 1
            self.tdb.attendance_logs.insert_one({
                "emp_code": "EMP0001", "date": date, "status": "PRESENT", "punch_in": at(ist_ms(2026, 8, d, 9, 41, 1)),
                "punch_out": None, "work_hours": None, "late_minutes": 11, "overtime_minutes": 0, "half_day": False,
                "history": []})
            res = _run_parallel(lambda i, date=date, d=d: (
                self.patch(date, punch_in=ist_ms(2026, 8, d, 9, 0)) if i == 0 else
                m.punch_out(m.PunchOutIn(emp_code="EMP0001", punched_at=ist_ms(2026, 8, d, 19, 10)))), 2)
            final = self.raw(date)
            with self.subTest(trial=trial, codes=[_code(r) for r in res]):
                self.assertTrue(all(c in (200, 409) for c in [_code(r) for r in res]))
                self.assertLessEqual(len(final["history"]), 1)
                self.assertEqual(final["late_minutes"], m.compute_late_minutes(final["punch_in"], *DAY[:1], date))
                if final["punch_out"] is not None:
                    self.assertEqual(final["work_hours"], m.compute_work_hours(final["punch_in"], final["punch_out"]))
                    self.assertEqual(final["overtime_minutes"], m.compute_overtime(final["punch_out"], *DAY, date))
                else:
                    self.assertIsNone(final["work_hours"])

    def test_http_flow_through_testclient_real_mongo(self):
        from fastapi.testclient import TestClient
        self.punch_flow()
        c = TestClient(m.app)
        r = c.patch("/attendance/EMP0001/2026-07-06", json={"reason": "forgot to punch", "regularized_by": "hr", "status": "LEAVE"})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual((b["status"], b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"],
                          b["overtime_minutes"], b["half_day"]), ("LEAVE", None, None, None, 0, 0, False))
        self.assertEqual(set(b), ATT_KEYS)
        self.assertEqual(c.patch("/attendance/EMP0001/2026-07-06", json={"reason": "again fine", "regularized_by": "hr", "status": "LEAVE"}).status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
