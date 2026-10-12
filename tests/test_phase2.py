"""Phase 2 tests: employee creation, employee listing, punch-in.

Layers (each class says what it does and does NOT prove):
  1. TestMapping*        pure functions; run anywhere (stdlib only).
  2. *Fake               endpoint functions called directly against an IN-MEMORY FAKE database.
                         These are MOCKED: they prove the code's branching, query shape and error
                         mapping. They do NOT prove MongoDB's unique-index or concurrency behaviour.
  3. TestPhase2Http      HTTP status codes / bodies via TestClient, still on the fake database.
  4. TestMongoIntegration  REAL MongoDB, opt-in: set TEST_MONGO_URI. Uses a throwaway database named
                         attendance_test_p2_<random> that is dropped afterwards; MONGO_DB is never used.

Run from the repo root:  python -m unittest discover -s tests -t . -v
"""
import copy
import json
import os
import threading
import time
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
IST = timezone(timedelta(hours=5, minutes=30))


def utc_ms(y, mo, d, h=0, mi=0, s=0, ms=0):
    return calendar.timegm((y, mo, d, h, mi, s)) * 1000 + ms


def ist_ms(y, mo, d, h=0, mi=0, s=0, ms=0):
    """Epoch ms of an IST wall-clock time (independent oracle: UTC ms minus the +05:30 offset)."""
    return utc_ms(y, mo, d, h, mi, s, ms) - (5 * 3600 + 30 * 60) * 1000


def now_ms():
    return int(time.time() * 1000)


EMP_KEYS = {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
ATT_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
            "overtime_minutes", "half_day", "history"}

# ======================================================================== 1. pure mapping
class TestMappingEmployee(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((m.DEFAULT_PAGE_SIZE, m.MAX_PAGE_SIZE), (20, 100))

    def test_page_skip(self):
        self.assertEqual(m.page_skip(1, 20), 0)
        self.assertEqual(m.page_skip(2, 20), 20)
        self.assertEqual(m.page_skip(3, 7), 14)

    def test_department_filter_is_exact(self):
        self.assertEqual(m.employee_list_filter(None), {})
        self.assertEqual(m.employee_list_filter("Sales"), {"department": "Sales"})
        self.assertEqual(m.employee_list_filter("sales"), {"department": "sales"})  # no case folding
        self.assertEqual(m.employee_list_filter(""), {"department": ""})  # present-but-empty is still exact

    def test_build_employee_doc(self):
        data = {"emp_code": "EMP0001", "name": "A"}
        now = datetime(2026, 7, 6, 4, 0, 0, 123456, tzinfo=UTC)
        doc = m.build_employee_doc(data, now)
        self.assertEqual(data, {"emp_code": "EMP0001", "name": "A"})  # input not mutated
        self.assertEqual(doc["created_at"], datetime(2026, 7, 6, 4, 0, 0, 123000, tzinfo=UTC))
        self.assertIsNotNone(doc["created_at"].tzinfo)

    def test_build_employee_doc_overrides_client_created_at(self):
        doc = m.build_employee_doc({"created_at": 5}, datetime(2026, 7, 6, tzinfo=UTC))
        self.assertIsInstance(doc["created_at"], datetime)

    def test_serialize_employee(self):
        doc = {"_id": "x", "emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "D",
               "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05",
               "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC), "secret": 1}
        out = m.serialize_employee(doc)
        self.assertEqual(set(out), EMP_KEYS)
        self.assertEqual(out["created_at"], utc_ms(2026, 1, 5, 4, 30))
        self.assertNotIn("_id", out)
        self.assertNotIn("secret", out)

    def test_serialize_employee_sample_data(self):
        for e in json.loads((ROOT / "sample_data" / "employees.json").read_text()):
            e = dict(e)
            e["created_at"] = _dt(e["created_at"])
            with self.subTest(emp=e["emp_code"]):
                out = m.serialize_employee(e)
                self.assertEqual(set(out), EMP_KEYS)
                self.assertIsInstance(out["created_at"], int)
                self.assertGreaterEqual(out["created_at"], m.EPOCH_MS_MIN)

    def test_serialize_employee_missing_created_at_is_null_not_an_error(self):
        out = m.serialize_employee({"emp_code": "EMP0001"})
        self.assertIsNone(out["created_at"])


def _dt(v):
    return None if v is None else datetime.strptime(v["$date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _sample_attendance():
    docs = json.loads((ROOT / "sample_data" / "attendance_logs.json").read_text())
    out = []
    for d in docs:
        d = copy.deepcopy(d)
        d["punch_in"], d["punch_out"] = _dt(d["punch_in"]), _dt(d["punch_out"])
        for h in d.get("history", []):
            h["at"] = _dt(h["at"])
            for k in ("punch_in", "punch_out"):
                if k in h["changes"]:
                    h["changes"][k] = {"from": _dt(h["changes"][k]["from"]), "to": _dt(h["changes"][k]["to"])}
        out.append(d)
    return out


class TestMappingAttendance(unittest.TestCase):
    DAY = {"shift_start": "09:30", "shift_end": "18:30"}
    NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}

    def doc(self, ms, emp=None, status="PRESENT"):
        return m.build_attendance_doc(emp or self.DAY, "EMP0001", status, m.instant_from_epoch_ms(ms))

    # ---- defaults
    def test_new_record_defaults(self):
        d = self.doc(ist_ms(2026, 7, 6, 9, 28))
        self.assertEqual((d["punch_out"], d["work_hours"], d["overtime_minutes"], d["half_day"], d["history"]),
                         (None, None, 0, False, []))
        self.assertEqual((d["emp_code"], d["status"], d["date"], d["late_minutes"]), ("EMP0001", "PRESENT", "2026-07-06", 0))

    def test_status_passes_through(self):
        for s in ("PRESENT", "WFH", "ON_DUTY"):
            self.assertEqual(self.doc(ist_ms(2026, 7, 6, 9, 0), status=s)["status"], s)

    # ---- IST date boundaries
    def test_ist_date_boundary_across_utc_midnight(self):
        self.assertEqual(self.doc(utc_ms(2026, 7, 6, 18, 29, 59))["date"], "2026-07-06")  # 23:59:59 IST
        self.assertEqual(self.doc(utc_ms(2026, 7, 6, 18, 30, 0))["date"], "2026-07-07")   # 00:00:00 IST
        self.assertEqual(self.doc(utc_ms(2026, 7, 6, 0, 0, 0))["date"], "2026-07-06")     # 05:30 IST
        self.assertEqual(self.doc(utc_ms(2026, 7, 6, 23, 59, 59))["date"], "2026-07-07")  # 05:29:59 IST

    def test_utc_date_differs_from_ist_date(self):
        ms = utc_ms(2026, 7, 6, 20, 0)  # 01:30 IST on the 7th, still the 6th in UTC
        self.assertEqual(m.instant_from_epoch_ms(ms).date().isoformat(), "2026-07-06")
        self.assertEqual(self.doc(ms)["date"], "2026-07-07")

    # ---- overnight shifts
    def test_overnight_attendance_dates(self):
        self.assertEqual(self.doc(ist_ms(2026, 7, 6, 21, 55), self.NIGHT)["date"], "2026-07-06")
        self.assertEqual(self.doc(ist_ms(2026, 7, 7, 3, 0), self.NIGHT)["date"], "2026-07-06")
        self.assertEqual(self.doc(ist_ms(2026, 7, 7, 5, 59, 59), self.NIGHT)["date"], "2026-07-06")
        self.assertEqual(self.doc(ist_ms(2026, 7, 7, 6, 0, 0), self.NIGHT)["date"], "2026-07-07")
        self.assertEqual(self.doc(ist_ms(2026, 7, 7, 0, 0, 0), self.NIGHT)["date"], "2026-07-06")

    def test_overnight_late_minutes(self):
        self.assertEqual(self.doc(ist_ms(2026, 7, 7, 3, 0), self.NIGHT)["late_minutes"], 300)
        self.assertEqual(self.doc(ist_ms(2026, 7, 6, 21, 55), self.NIGHT)["late_minutes"], 0)
        self.assertEqual(self.doc(ist_ms(2026, 7, 6, 22, 10, 0), self.NIGHT)["late_minutes"], 0)
        self.assertEqual(self.doc(ist_ms(2026, 7, 6, 22, 10, 1), self.NIGHT)["late_minutes"], 10)

    # ---- late threshold
    def test_late_threshold_boundaries(self):
        def late(h, mi, s=0, ms=0):
            return self.doc(ist_ms(2026, 7, 6, h, mi, s, ms))["late_minutes"]
        self.assertEqual(late(9, 39, 59), 0)
        self.assertEqual(late(9, 40, 0), 0)        # exactly 10:00 -> not late
        self.assertEqual(late(9, 40, 1), 10)       # 10:00 + 1s -> late, floor(10:01 min) = 10
        self.assertEqual(late(9, 40, 59), 10)      # minute flooring
        self.assertEqual(late(9, 41, 0), 11)
        self.assertEqual(late(10, 15, 59), 45)
        self.assertEqual(late(9, 30, 0), 0)
        self.assertEqual(late(8, 0, 0), 0)

    def test_late_uses_employee_shift(self):
        emp = {"shift_start": "14:00", "shift_end": "23:00"}
        self.assertEqual(self.doc(ist_ms(2026, 7, 6, 14, 10, 0), emp)["late_minutes"], 0)
        self.assertEqual(self.doc(ist_ms(2026, 7, 6, 14, 11, 0), emp)["late_minutes"], 11)

    # ---- whole-second truncation
    def test_truncation_before_date_late_and_storage(self):
        # 09:40:00.900 counts as 09:40:00 -> not late
        d = self.doc(ist_ms(2026, 7, 6, 9, 40, 0, 900))
        self.assertEqual(d["late_minutes"], 0)
        self.assertEqual(d["punch_in"].microsecond, 0)
        self.assertEqual(m.datetime_to_epoch_ms(d["punch_in"]), ist_ms(2026, 7, 6, 9, 40, 0))
        # 23:59:59.900 IST stays on the same date
        self.assertEqual(self.doc(ist_ms(2026, 7, 6, 23, 59, 59, 900))["date"], "2026-07-06")
        # 05:59:59.900 on a 22:00-06:00 shift is still before 06:00 -> previous day
        self.assertEqual(self.doc(ist_ms(2026, 7, 7, 5, 59, 59, 900), self.NIGHT)["date"], "2026-07-06")


class TestMappingSerializeAttendance(unittest.TestCase):
    def test_exact_key_set_and_no_id_leak(self):
        src = {"_id": "oid", "id": "alias", "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
               "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=UTC), "punch_out": None, "work_hours": None,
               "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [], "extra": 1}
        out = m.serialize_attendance(src)
        self.assertEqual(set(out), ATT_KEYS)
        self.assertNotIn("_id", out)
        self.assertNotIn("id", out)
        self.assertEqual(out["punch_in"], utc_ms(2026, 7, 6, 3, 58))
        self.assertIsNone(out["punch_out"])

    def test_legacy_defaults(self):
        legacy = {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                  "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
                  "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC), "work_hours": 8.98}
        out = m.serialize_attendance(legacy)
        self.assertEqual(set(out), ATT_KEYS)
        self.assertEqual((out["late_minutes"], out["overtime_minutes"], out["half_day"], out["history"]),
                         (0, 0, False, []))
        self.assertEqual(out["work_hours"], 8.98)

    def test_null_and_missing_everything_optional(self):
        out = m.serialize_attendance({"emp_code": "E", "date": "2026-07-08", "status": "ABSENT",
                                      "late_minutes": None, "overtime_minutes": None,
                                      "half_day": None, "history": None})
        self.assertEqual((out["punch_in"], out["punch_out"], out["work_hours"]), (None, None, None))
        self.assertEqual((out["late_minutes"], out["overtime_minutes"], out["half_day"], out["history"]),
                         (0, 0, False, []))

    def test_history_instants_become_epoch_ms(self):
        h = {"at": datetime(2026, 7, 8, 5, 0, tzinfo=UTC), "by": "hr", "reason": "glitch", "changes": {
            "punch_in": {"from": datetime(2026, 7, 7, 4, 35, tzinfo=UTC), "to": datetime(2026, 7, 7, 3, 58, tzinfo=UTC)},
            "punch_out": {"from": None, "to": datetime(2026, 7, 7, 13, 0, tzinfo=UTC)},
            "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}}
        out = m.serialize_attendance({"emp_code": "E", "date": "2026-07-07", "status": "PRESENT", "history": [h]})
        e = out["history"][0]
        self.assertEqual(e["at"], utc_ms(2026, 7, 8, 5, 0))
        self.assertEqual(e["changes"]["punch_in"], {"from": utc_ms(2026, 7, 7, 4, 35), "to": utc_ms(2026, 7, 7, 3, 58)})
        self.assertEqual(e["changes"]["punch_out"], {"from": None, "to": utc_ms(2026, 7, 7, 13, 0)})
        self.assertEqual(e["changes"]["late_minutes"], {"from": 35, "to": 0})
        self.assertEqual(e["changes"]["work_hours"], {"from": 8.42, "to": 9.03})
        self.assertEqual(set(e), {"at", "by", "reason", "changes"})

    def test_all_nine_sample_documents(self):
        docs = _sample_attendance()
        self.assertEqual(len(docs), 9)
        for d in docs:
            with self.subTest(emp=d["emp_code"], date=d["date"]):
                out = m.serialize_attendance(d)
                self.assertEqual(set(out), ATT_KEYS)
                json.dumps(out)  # JSON-serialisable: no datetimes left
        legacy = m.serialize_attendance(docs[-1])
        self.assertEqual((legacy["half_day"], legacy["history"]), (False, []))
        regularized = m.serialize_attendance(docs[7])
        self.assertEqual(len(regularized["history"]), 1)
        self.assertIsInstance(regularized["history"][0]["changes"]["punch_in"]["from"], int)


# ======================================================================== 2. in-memory fake DB
def _project(doc, projection):
    doc = dict(doc)
    if not projection:
        return doc
    include = [k for k, v in projection.items() if v and k != "_id"]
    if include:
        out = {k: doc[k] for k in include if k in doc}
        if projection.get("_id", 1):
            out["_id"] = doc.get("_id")
        return out
    for k, v in projection.items():
        if not v:
            doc.pop(k, None)
    return doc


class FakeCursor:
    def __init__(self, docs, projection):
        self.docs, self.projection = list(docs), projection
        self.sort_spec = None
        self.skip_n = 0
        self.limit_n = None

    def sort(self, key, direction=1):
        self.sort_spec = (key, direction)
        self.docs.sort(key=lambda d: d[key], reverse=direction < 0)
        return self

    def skip(self, n):
        self.skip_n = n
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def __iter__(self):
        docs = self.docs[self.skip_n:]
        if self.limit_n:
            docs = docs[: self.limit_n]
        return iter([_project(d, self.projection) for d in docs])


class FakeCollection:
    """Minimal in-memory collection. NOT MongoDB: a unique key is checked in plain Python."""

    def __init__(self, unique=None):
        from pymongo.errors import DuplicateKeyError
        self._dup = DuplicateKeyError
        self.unique = unique
        self.docs = []
        self.calls = []                 # method names, in order
        self.count_filters = []
        self.last_cursor = None
        self.force_duplicate = False    # simulate "another request inserted first"
        self.force_error = None         # simulate an unexpected failure
        self._n = 0

    @staticmethod
    def _match(doc, flt):
        return all(doc.get(k) == v for k, v in (flt or {}).items())

    def insert_one(self, doc):
        self.calls.append("insert_one")
        if self.force_error:
            raise self.force_error
        if self.force_duplicate:
            raise self._dup("E11000 duplicate key error (simulated)")
        if self.unique and any(all(d.get(k) == doc.get(k) for k in self.unique) for d in self.docs):
            raise self._dup("E11000 duplicate key error (fake unique check)")
        self._n += 1
        doc["_id"] = f"oid{self._n}"    # like PyMongo, insert_one adds _id to the caller's dict
        self.docs.append(copy.deepcopy(doc))

    def find_one(self, flt=None, projection=None):
        self.calls.append("find_one")
        for d in self.docs:
            if self._match(d, flt):
                return _project(d, projection)
        return None

    def count_documents(self, flt):
        self.calls.append("count_documents")
        self.count_filters.append(flt)
        return sum(1 for d in self.docs if self._match(d, flt))

    def find(self, flt=None, projection=None):
        self.calls.append("find")
        self.last_cursor = FakeCursor([d for d in self.docs if self._match(d, flt)], projection)
        return self.last_cursor


class FakeDB:
    def __init__(self):
        self.employees = FakeCollection(unique=("emp_code",))
        self.attendance_logs = FakeCollection(unique=("emp_code", "date"))

    def __getitem__(self, name):
        return getattr(self, name)


@unittest.skipUnless(FULL, "needs fastapi/pydantic/pymongo installed: NOT EXECUTED")
class FakeDbCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDB()
        self._orig_db = m.db
        m.db = self.fake

    def tearDown(self):
        m.db = self._orig_db

    def add_emp(self, code="EMP0001", dept="Engineering", start="09:30", end="18:30", joined="2026-01-05"):
        self.fake.employees.docs.append({
            "_id": "seed-" + code, "emp_code": code, "name": "N " + code, "email": f"{code.lower()}@x.io",
            "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined,
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)})

    def status_of(self, fn, *a, **kw):
        from fastapi import HTTPException
        try:
            return 200, fn(*a, **kw)
        except HTTPException as e:
            return e.status_code, e.detail


EMP_PAYLOAD = dict(emp_code="EMP0001", name="Asha Rao", email="asha@example.com",
                   department="Engineering", joined_on="2026-01-05")


class TestCreateEmployeeFake(FakeDbCase):
    def create(self, **over):
        return m.create_employee(m.EmployeeIn(**{**EMP_PAYLOAD, **over}))

    def test_success_and_defaults(self):
        before = now_ms()
        out = self.create()
        after = now_ms()
        self.assertEqual(set(out), EMP_KEYS)
        self.assertEqual((out["shift_start"], out["shift_end"]), ("09:30", "18:30"))
        self.assertEqual(out["emp_code"], "EMP0001")
        self.assertIsInstance(out["created_at"], int)
        self.assertTrue(before - 1 <= out["created_at"] <= after + 1)
        self.assertNotIn("_id", out)
        self.assertNotIn("id", out)

    def test_stored_document_is_tz_aware_and_matches_response(self):
        out = self.create()
        stored = self.fake.employees.docs[0]
        self.assertIsNotNone(stored["created_at"].tzinfo)
        self.assertEqual(m.datetime_to_epoch_ms(stored["created_at"]), out["created_at"])

    def test_client_supplied_server_fields_are_ignored(self):
        out = self.create(created_at=1, _id="evil", id="evil")
        self.assertNotEqual(out["created_at"], 1)
        self.assertGreaterEqual(out["created_at"], m.EPOCH_MS_MIN)
        self.assertNotEqual(self.fake.employees.docs[0]["_id"], "evil")  # store-generated, not client-supplied

    def test_overnight_and_custom_shift_stored(self):
        out = self.create(shift_start="22:00", shift_end="06:00")
        self.assertEqual((out["shift_start"], out["shift_end"]), ("22:00", "06:00"))

    def test_duplicate_returns_409(self):
        self.create()
        code, detail = self.status_of(self.create)
        self.assertEqual((code, detail), (409, "emp_code already exists"))
        self.assertEqual(len(self.fake.employees.docs), 1)

    def test_duplicate_key_race_returns_409_and_is_not_check_then_insert(self):
        # Simulates losing the race: the insert is rejected by the (fake) unique index.
        self.fake.employees.force_duplicate = True
        code, detail = self.status_of(self.create)
        self.assertEqual((code, detail), (409, "emp_code already exists"))
        self.assertEqual(self.fake.employees.calls, ["insert_one"])  # no find_one pre-check

    def test_unexpected_error_is_not_swallowed(self):
        self.fake.employees.force_error = RuntimeError("disk on fire")
        with self.assertRaises(RuntimeError):
            self.create()


class TestListEmployeesFake(FakeDbCase):
    def setUp(self):
        super().setUp()
        # inserted out of order on purpose
        for code, dept in [("EMP0007", "Sales"), ("EMP0002", "Engineering"), ("EMP0005", "Sales"),
                           ("EMP0001", "Engineering"), ("EMP0004", "Support"), ("EMP0003", "Sales"),
                           ("EMP0006", "Engineering")]:
            self.add_emp(code, dept)

    def list(self, **kw):
        args = dict(department=None, page=1, page_size=20)
        args.update(kw)
        return m.list_employees(**args)

    def codes(self, r):
        return [i["emp_code"] for i in r["items"]]

    def test_sorted_by_emp_code_ascending(self):
        r = self.list()
        self.assertEqual(self.codes(r), [f"EMP000{i}" for i in range(1, 8)])
        self.assertEqual((r["total"], r["page"], r["page_size"]), (7, 1, 20))

    def test_sort_and_pagination_are_pushed_to_the_database(self):
        self.list(page=2, page_size=3)
        c = self.fake.employees.last_cursor
        self.assertEqual((c.sort_spec, c.skip_n, c.limit_n), (("emp_code", 1), 3, 3))
        self.assertNotIn("find_one", self.fake.employees.calls)

    def test_pagination_pages(self):
        self.assertEqual(self.codes(self.list(page=1, page_size=3)), ["EMP0001", "EMP0002", "EMP0003"])
        self.assertEqual(self.codes(self.list(page=2, page_size=3)), ["EMP0004", "EMP0005", "EMP0006"])
        self.assertEqual(self.codes(self.list(page=3, page_size=3)), ["EMP0007"])

    def test_page_one_is_not_skipped(self):  # regression: starter used skip = page * page_size
        self.assertEqual(self.codes(self.list(page=1, page_size=2))[0], "EMP0001")

    def test_department_filter_exact_case_sensitive_and_total_is_filtered(self):
        r = self.list(department="Sales")
        self.assertEqual(self.codes(r), ["EMP0003", "EMP0005", "EMP0007"])
        self.assertEqual(r["total"], 3)
        self.assertEqual(self.fake.employees.count_filters[-1], {"department": "Sales"})
        r = self.list(department="sales")
        self.assertEqual((r["items"], r["total"]), ([], 0))
        r = self.list(department="Sal")
        self.assertEqual((r["items"], r["total"]), ([], 0))

    def test_filtered_total_with_paging(self):
        r = self.list(department="Engineering", page=2, page_size=2)
        self.assertEqual(self.codes(r), ["EMP0006"])
        self.assertEqual(r["total"], 3)

    def test_out_of_range_page_is_200_with_empty_items_and_true_total(self):
        r = self.list(page=99, page_size=3)
        self.assertEqual((r["items"], r["total"], r["page"], r["page_size"]), ([], 7, 99, 3))

    def test_exact_end_boundary(self):
        self.assertEqual(self.list(page=2, page_size=7)["items"], [])
        self.assertEqual(len(self.list(page=1, page_size=7)["items"]), 7)

    def test_absurdly_large_page_does_not_overflow(self):
        r = self.list(page=10 ** 30, page_size=100)
        self.assertEqual((r["items"], r["total"]), ([], 7))

    def test_items_follow_contract_schema_without_internal_fields(self):
        for item in self.list()["items"]:
            self.assertEqual(set(item), EMP_KEYS)
            self.assertIsInstance(item["created_at"], int)

    def test_empty_collection(self):
        self.fake.employees.docs.clear()
        r = self.list()
        self.assertEqual((r["items"], r["total"]), ([], 0))


class TestPunchInFake(FakeDbCase):
    def setUp(self):
        super().setUp()
        self.add_emp("EMP0001")

    def punch(self, ms=None, emp="EMP0001", **kw):
        if ms is not None:  # omit the field entirely: an explicit null is a 422 (Phase 1 rule)
            kw["punched_at"] = ms
        return m.punch_in(m.PunchInIn(emp_code=emp, **kw))

    def punch_fresh(self, ms, **kw):
        self.fake.attendance_logs.docs.clear()
        return self.punch(ms, **kw)

    def test_success_and_initial_values(self):
        ms = ist_ms(2026, 7, 6, 9, 28)
        out = self.punch(ms)
        self.assertEqual(set(out), ATT_KEYS)
        self.assertEqual(out, {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                               "punch_in": ms, "punch_out": None, "work_hours": None, "late_minutes": 0,
                               "overtime_minutes": 0, "half_day": False, "history": []})

    def test_response_never_leaks_internal_ids(self):
        out = self.punch(ist_ms(2026, 7, 6, 9, 28))
        self.assertNotIn("_id", out)
        self.assertNotIn("id", out)
        self.assertIn("_id", self.fake.attendance_logs.docs[0])  # the store has one; the API hides it

    def test_default_punched_at_is_now_truncated_to_the_second(self):
        before = now_ms()
        out = self.punch(None)
        after = now_ms()
        self.assertEqual(out["punch_in"] % 1000, 0)
        self.assertTrue(before - 1000 <= out["punch_in"] <= after)

    def test_stored_instant_is_tz_aware_and_truncated(self):
        out = self.punch(ist_ms(2026, 7, 6, 9, 28, 30, 900))
        stored = self.fake.attendance_logs.docs[0]["punch_in"]
        self.assertIsNotNone(stored.tzinfo)
        self.assertEqual(stored.microsecond, 0)
        self.assertEqual(out["punch_in"], ist_ms(2026, 7, 6, 9, 28, 30))

    def test_accepts_wfh_and_on_duty(self):
        self.assertEqual(self.punch(ist_ms(2026, 7, 6, 9, 0), status="WFH")["status"], "WFH")
        self.assertEqual(self.punch_fresh(ist_ms(2026, 7, 6, 9, 0), status="ON_DUTY")["status"], "ON_DUTY")

    def test_unknown_employee_is_404_and_writes_nothing(self):
        code, detail = self.status_of(self.punch, ist_ms(2026, 7, 6, 9, 0), "EMP9999")
        self.assertEqual((code, detail), (404, "employee not found"))
        self.assertEqual(self.fake.attendance_logs.calls, [])

    def test_duplicate_same_date_is_409(self):
        self.punch(ist_ms(2026, 7, 6, 9, 0))
        code, detail = self.status_of(self.punch, ist_ms(2026, 7, 6, 15, 0))
        self.assertEqual((code, detail), (409, "already punched in for this date"))
        self.assertEqual(len(self.fake.attendance_logs.docs), 1)

    def test_other_date_and_other_employee_are_not_duplicates(self):
        self.add_emp("EMP0002")
        self.punch(ist_ms(2026, 7, 6, 9, 0))
        self.assertEqual(self.punch(ist_ms(2026, 7, 7, 9, 0))["date"], "2026-07-07")
        self.assertEqual(self.punch(ist_ms(2026, 7, 6, 9, 0), "EMP0002")["emp_code"], "EMP0002")

    def test_duplicate_key_race_returns_409_and_is_not_check_then_insert(self):
        self.fake.attendance_logs.force_duplicate = True
        code, detail = self.status_of(self.punch, ist_ms(2026, 7, 6, 9, 0))
        self.assertEqual((code, detail), (409, "already punched in for this date"))
        self.assertEqual(self.fake.attendance_logs.calls, ["insert_one"])  # no find_one pre-check

    def test_unexpected_error_is_not_swallowed(self):
        self.fake.attendance_logs.force_error = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.punch(ist_ms(2026, 7, 6, 9, 0))

    def test_late_boundaries_through_the_endpoint(self):
        cases = [((9, 40, 0, 0), 0), ((9, 40, 0, 900), 0), ((9, 40, 1, 0), 10), ((9, 40, 59, 0), 10),
                 ((9, 41, 0, 0), 11), ((10, 15, 59, 0), 45)]
        for (h, mi, s, ms), expected in cases:
            with self.subTest(at=f"{h}:{mi}:{s}.{ms}"):
                self.assertEqual(self.punch_fresh(ist_ms(2026, 7, 6, h, mi, s, ms))["late_minutes"], expected)

    def test_ist_boundary_and_overnight_through_the_endpoint(self):
        self.assertEqual(self.punch_fresh(utc_ms(2026, 7, 6, 18, 29, 59))["date"], "2026-07-06")
        self.assertEqual(self.punch_fresh(utc_ms(2026, 7, 6, 18, 30, 0))["date"], "2026-07-07")
        self.add_emp("EMP0005", "Support", "22:00", "06:00")
        r = self.punch_fresh(ist_ms(2026, 7, 7, 3, 0), emp="EMP0005")
        self.assertEqual((r["date"], r["late_minutes"]), ("2026-07-06", 300))
        r = self.punch_fresh(ist_ms(2026, 7, 7, 6, 0), emp="EMP0005")
        self.assertEqual(r["date"], "2026-07-07")

    def test_uses_the_employees_configured_shift(self):
        self.add_emp("EMP0009", "Sales", "14:00", "23:00")
        self.assertEqual(self.punch(ist_ms(2026, 7, 6, 14, 11), emp="EMP0009")["late_minutes"], 11)


# ======================================================================== 3. HTTP layer on the fake DB
@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestPhase2Http(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)  # not a context manager: lifespan (index creation) does not run

    def test_create_employee_201_shape_and_defaults(self):
        r = self.c.post("/employees", json={**EMP_PAYLOAD, "created_at": 1, "_id": "x"})
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(set(body), EMP_KEYS)
        self.assertEqual((body["shift_start"], body["shift_end"]), ("09:30", "18:30"))
        self.assertGreaterEqual(body["created_at"], m.EPOCH_MS_MIN)

    def test_create_employee_422_matrix(self):
        for over in (dict(emp_code="EMP1"), dict(emp_code="EMP1234567"), dict(emp_code="emp0001"),
                     dict(name=""), dict(name="x" * 101), dict(email="nope"), dict(email="a" * 120 + "@b.co"),
                     dict(department=""), dict(department="d" * 51), dict(joined_on="2026-02-30"),
                     dict(shift_start="24:61"), dict(shift_end="9:30"),
                     dict(shift_start="10:00", shift_end="10:00")):
            with self.subTest(over=over):
                r = self.c.post("/employees", json={**EMP_PAYLOAD, **over})
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)
        self.assertEqual(self.fake.employees.docs, [])

    def test_create_employee_overnight_shift_is_valid(self):
        r = self.c.post("/employees", json={**EMP_PAYLOAD, "shift_start": "22:00", "shift_end": "06:00"})
        self.assertEqual(r.status_code, 201)

    def test_create_employee_duplicate_409_detail_shape(self):
        self.assertEqual(self.c.post("/employees", json=EMP_PAYLOAD).status_code, 201)
        r = self.c.post("/employees", json=EMP_PAYLOAD)
        self.assertEqual((r.status_code, r.json()), (409, {"detail": "emp_code already exists"}))

    def test_create_employee_simulated_race_409(self):
        self.fake.employees.force_duplicate = True
        r = self.c.post("/employees", json=EMP_PAYLOAD)
        self.assertEqual((r.status_code, r.json()), (409, {"detail": "emp_code already exists"}))

    def test_list_invalid_pagination_is_422(self):
        for qs in ("page=0", "page=-1", "page=abc", "page_size=0", "page_size=101", "page_size=-5", "page_size=x"):
            with self.subTest(qs=qs):
                self.assertEqual(self.c.get("/employees?" + qs).status_code, 422)

    def test_list_valid_edges_and_defaults(self):
        for i in range(1, 6):
            self.add_emp(f"EMP000{i}", "Sales" if i % 2 else "Support")
        r = self.c.get("/employees")
        self.assertEqual((r.status_code, r.json()["page"], r.json()["page_size"], r.json()["total"]), (200, 1, 20, 5))
        self.assertEqual(self.c.get("/employees?page_size=100").status_code, 200)
        self.assertEqual(self.c.get("/employees?page_size=1").status_code, 200)
        r = self.c.get("/employees?department=Sales&page=1&page_size=2").json()
        self.assertEqual((r["total"], [i["emp_code"] for i in r["items"]]), (3, ["EMP0001", "EMP0003"]))
        r = self.c.get("/employees?page=50").json()
        self.assertEqual((r["items"], r["total"]), ([], 5))

    def test_punch_in_201_404_409_and_no_id(self):
        self.add_emp("EMP0001")
        ms = ist_ms(2026, 7, 6, 9, 28)
        r = self.c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms, "created": 1})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(set(r.json()), ATT_KEYS)
        self.assertEqual(r.json()["punch_in"], ms)
        r = self.c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})
        self.assertEqual((r.status_code, r.json()), (409, {"detail": "already punched in for this date"}))
        r = self.c.post("/attendance/punch-in", json={"emp_code": "EMP4040", "punched_at": ms})
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "employee not found"}))

    def test_punch_in_status_422(self):
        self.add_emp("EMP0001")
        for s in ("ABSENT", "LEAVE", "absent", "", "HALF_DAY", 1, None):
            with self.subTest(status=s):
                r = self.c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": s})
                self.assertEqual(r.status_code, 422)
        self.assertEqual(self.fake.attendance_logs.docs, [])

    def test_punch_in_bad_timestamps_422(self):
        self.add_emp("EMP0001")
        for v in (1783312500, 1783312500000.5, "1783312500000", True, 0, 4102444800001, None):
            with self.subTest(v=v):
                r = self.c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": v})
                self.assertEqual(r.status_code, 422)

    def test_punch_in_missing_emp_code_422(self):
        self.assertEqual(self.c.post("/attendance/punch-in", json={}).status_code, 422)

    def test_punch_in_simulated_race_409(self):
        self.add_emp("EMP0001")
        self.fake.attendance_logs.force_duplicate = True
        r = self.c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 0)})
        self.assertEqual((r.status_code, r.json()), (409, {"detail": "already punched in for this date"}))


# ======================================================================== 4. REAL MongoDB (opt-in)
def _run_parallel(fn, n):
    barrier = threading.Barrier(n)
    results = [None] * n

    def work(i):
        barrier.wait()
        try:
            results[i] = ("ok", fn(i))
        except Exception as e:  # classified by the caller
            results[i] = ("err", e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    return results


def _status(result):
    from fastapi import HTTPException
    kind, val = result
    if kind == "ok":
        return 200
    return val.status_code if isinstance(val, HTTPException) else repr(val)


@unittest.skipUnless(FULL and HAVE_HTTP and TEST_URI,
                     "set TEST_MONGO_URI to run the real-MongoDB integration tests: NOT EXECUTED")
class TestMongoIntegration(unittest.TestCase):
    """REAL MongoDB. Uses a throwaway database (attendance_test_p2_<random>), dropped at the end."""
    PREFIX = "attendance_test_p2_"

    @classmethod
    def setUpClass(cls):
        from pymongo import MongoClient
        cls.client = MongoClient(TEST_URI, tz_aware=True, serverSelectionTimeoutMS=4000)
        cls.client.admin.command("ping")
        cls.dbname = cls.PREFIX + uuid.uuid4().hex[:8]
        cls.tdb = cls.client[cls.dbname]
        cls._orig_db = m.db
        m.db = cls.tdb
        m.ensure_indexes(cls.tdb)

    @classmethod
    def tearDownClass(cls):
        m.db = cls._orig_db
        if cls.dbname.startswith(cls.PREFIX):
            cls.client.drop_database(cls.dbname)
        cls.client.close()

    def setUp(self):
        self.tdb.employees.delete_many({})
        self.tdb.attendance_logs.delete_many({})

    def emp(self, code="EMP0001", dept="Engineering", start="09:30", end="18:30"):
        return m.create_employee(m.EmployeeIn(emp_code=code, name="N", email="n@x.io", department=dept,
                                              shift_start=start, shift_end=end, joined_on="2026-01-05"))

    def test_startup_creates_indexes_and_is_idempotent(self):
        from fastapi.testclient import TestClient
        self.tdb.employees.drop_indexes()
        self.tdb.attendance_logs.drop_indexes()
        for _ in range(2):  # start the app twice against the same database
            with TestClient(m.app) as c:
                self.assertEqual(c.get("/health").status_code, 200)
        e = self.tdb.employees.index_information()
        a = self.tdb.attendance_logs.index_information()
        self.assertTrue(any(v.get("unique") and v["key"] == [("emp_code", 1)] for v in e.values()))
        self.assertTrue(any(v.get("unique") and v["key"] == [("emp_code", 1), ("date", 1)] for v in a.values()))
        self.assertTrue(any(v["key"] == [("department", 1), ("emp_code", 1)] for v in e.values()))

    def test_concurrent_employee_creation_exactly_one_201(self):
        res = _run_parallel(lambda i: self.emp("EMP0042"), 20)
        codes = sorted(str(_status(r)) for r in res)
        self.assertEqual(codes, ["200"] + ["409"] * 19, codes)  # 200 == the endpoint function returned
        self.assertEqual(self.tdb.employees.count_documents({"emp_code": "EMP0042"}), 1)

    def test_concurrent_punch_in_same_date_exactly_one_201(self):
        self.emp("EMP0001")
        base = ist_ms(2026, 7, 6, 9, 0)
        res = _run_parallel(lambda i: m.punch_in(m.PunchInIn(emp_code="EMP0001", punched_at=base + i * 1000)), 20)
        codes = sorted(str(_status(r)) for r in res)
        self.assertEqual(codes, ["200"] + ["409"] * 19, codes)
        self.assertEqual(self.tdb.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-06"}), 1)

    def test_listing_sort_pagination_filter_total(self):
        import random
        codes = [f"EMP{i:04d}" for i in range(1, 251)]
        random.Random(7).shuffle(codes)
        depts = ["Engineering", "Sales", "Support"]
        self.tdb.employees.insert_many([
            {"emp_code": c, "name": "N", "email": "n@x.io", "department": depts[int(c[3:]) % 3],
             "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05",
             "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC)} for c in codes])
        p1 = m.list_employees(department=None, page=1, page_size=100)
        p2 = m.list_employees(department=None, page=2, page_size=100)
        p3 = m.list_employees(department=None, page=3, page_size=100)
        got = [i["emp_code"] for p in (p1, p2, p3) for i in p["items"]]
        self.assertEqual(got, sorted(codes))
        self.assertEqual((len(p1["items"]), len(p2["items"]), len(p3["items"]), p1["total"]), (100, 100, 50, 250))
        s = m.list_employees(department="Sales", page=1, page_size=100)
        expected = sorted(c for c in codes if int(c[3:]) % 3 == 1)
        self.assertEqual([i["emp_code"] for i in s["items"]], expected)
        self.assertEqual(s["total"], len(expected))
        self.assertEqual(m.list_employees(department="sales", page=1, page_size=20)["total"], 0)
        far = m.list_employees(department=None, page=999, page_size=20)
        self.assertEqual((far["items"], far["total"]), ([], 250))

    def test_list_query_uses_an_index_per_explain(self):
        self.tdb.employees.insert_many([
            {"emp_code": f"EMP{i:04d}", "department": ["A", "B", "C"][i % 3]} for i in range(1, 301)])
        for q in ({}, {"department": "B"}):
            with self.subTest(q=q):
                plan = m.employee_page_cursor(q, 20, 20).explain()["queryPlanner"]["winningPlan"]
                text = json.dumps(plan, default=str)
                self.assertIn("IXSCAN", text)
                self.assertNotIn("COLLSCAN", text)
                self.assertNotIn('"SORT"', text)  # sort satisfied by the index, no in-memory sort stage

    def test_punch_in_round_trip_tz_aware_and_truncated(self):
        self.emp("EMP0001")
        ms = ist_ms(2026, 7, 6, 9, 41, 1, 900)
        out = m.punch_in(m.PunchInIn(emp_code="EMP0001", punched_at=ms))
        self.assertEqual((out["late_minutes"], out["punch_in"]), (11, ms - 900))
        raw = self.tdb.attendance_logs.find_one({"emp_code": "EMP0001"})
        self.assertIsNotNone(raw["punch_in"].tzinfo)
        self.assertEqual(m.datetime_to_epoch_ms(raw["punch_in"]), out["punch_in"])
        self.assertEqual(m.serialize_attendance(raw), out)

    def test_legacy_attendance_document_serializes_with_defaults(self):
        self.tdb.attendance_logs.insert_one({
            "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
            "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC), "work_hours": 8.98})
        out = m.serialize_attendance(self.tdb.attendance_logs.find_one({"emp_code": "EMP0002"}))
        self.assertEqual(set(out), ATT_KEYS)
        self.assertEqual((out["half_day"], out["history"], out["late_minutes"], out["overtime_minutes"]),
                         (False, [], 0, 0))

    def test_duplicate_employee_sequential_409(self):
        from fastapi import HTTPException
        self.emp("EMP0001")
        with self.assertRaises(HTTPException) as cm:
            self.emp("EMP0001")
        self.assertEqual(cm.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
