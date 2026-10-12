"""Phase 6 tests: database-failure handling and startup/readiness. Fake database only (MOCKED: they prove HTTP
mapping and lifecycle logic, not MongoDB behaviour). No MongoDB is needed.
Run:  python -m unittest tests.test_phase6 -v"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from pymongo.errors import (ConnectionFailure, DuplicateKeyError, OperationFailure, PyMongoError,
                            ServerSelectionTimeoutError)

from app import main as m

try:
    from fastapi.testclient import TestClient
    HAVE_HTTP = True
except Exception:       # pragma: no cover - httpx missing
    HAVE_HTTP = False

SECRET = "mongodb://admin:hunter2@db.internal.example:27017"
BODY = {"detail": "database unavailable"}


class _Coll:
    def __init__(self, exc=None, **overrides):
        self._exc = exc
        self._ov = overrides

    def __getattr__(self, name):
        if name in self._ov:
            return self._ov[name]

        def call(*_a, **_k):
            raise self._exc
        return call


class _DB:
    """Every collection method raises exc, except the explicit overrides {collection: {method: callable}}."""
    def __init__(self, exc, command=None, **overrides):
        self._exc, self._command, self._ov = exc, command, overrides

    def command(self, *a, **k):
        if self._command is not None:
            return self._command(*a, **k)
        raise self._exc

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Coll(self._exc, **self._ov.get(name, {}))


def _timeout():
    return ServerSelectionTimeoutError(f"{SECRET}: timed out, Timeout: 5.0s")


@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(m.app)       # no `with`: the lifespan (index creation) is not run

    def setUp(self):
        self._orig_db, self._orig_ready = m.db, m._indexes_ready

    def tearDown(self):
        m.db, m._indexes_ready = self._orig_db, self._orig_ready


EMP = {"emp_code": "EMP0001", "name": "A", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-05"}
REG = {"reason": "forgot to punch", "regularized_by": "hr", "status": "PRESENT"}
REQUESTS = (
    ("GET", "/employees", None),
    ("POST", "/employees", EMP),
    ("POST", "/attendance/punch-in", {"emp_code": "EMP0001"}),
    ("POST", "/attendance/punch-out", {"emp_code": "EMP0001"}),
    ("GET", "/attendance", None),
    ("PATCH", "/attendance/EMP0001/2026-07-06", REG),
    ("GET", "/analytics/employees/EMP0001/monthly?month=2026-07", None),
    ("GET", "/analytics/departments/summary?month=2026-07", None),
    ("GET", "/analytics/leaderboard/late?month=2026-07", None),
    ("GET", "/analytics/departments/Eng/trend?from=2026-07-06&to=2026-07-14", None),
    ("GET", "/admin/explain/attendance_list", None),
    ("GET", "/admin/explain/department_trend?department=Eng&from=2026-07-06&to=2026-07-14", None),
)


class TestDatabaseFailure503(Base):
    def test_every_database_backed_endpoint_maps_a_driver_failure_to_503(self):
        for exc in (_timeout(), ConnectionFailure(SECRET), OperationFailure(f"auth failed for {SECRET}")):
            m.db = _DB(exc)
            for method, url, body in REQUESTS:
                with self.subTest(exc=type(exc).__name__, request=f"{method} {url}"):
                    r = self.c.request(method, url, json=body)
                    self.assertEqual(r.status_code, 503, r.text)
                    self.assertEqual(r.json(), BODY)
                    self.assertEqual(r.headers["content-type"], "application/json")

    def test_nothing_from_the_driver_message_is_returned(self):
        m.db = _DB(_timeout())
        for method, url, body in REQUESTS:
            text = self.c.request(method, url, json=body).text
            for leak in ("hunter2", "admin", "internal.example", "27017", "mongodb://", "Timeout"):
                self.assertNotIn(leak, text, f"{method} {url}")

    def test_only_the_class_name_is_logged(self):
        m.db = _DB(_timeout())
        with self.assertLogs("attendance", level="ERROR") as cap:
            self.c.get("/employees")
        joined = "\n".join(cap.output)
        self.assertIn("ServerSelectionTimeoutError", joined)
        self.assertNotIn("hunter2", joined)

    def test_exactly_one_handler_for_pymongo_errors(self):
        handlers = [k for k in m.app.exception_handlers if k is PyMongoError]
        self.assertEqual(len(handlers), 1)
        self.assertNotIn(Exception, m.app.exception_handlers)       # programming errors are not covered

    def test_a_programming_error_is_not_disguised_as_a_database_failure(self):
        m.db = _DB(RuntimeError("bug"))
        with self.assertRaises(RuntimeError):                      # TestClient re-raises unhandled server errors
            self.c.get("/employees")
        r = TestClient(m.app, raise_server_exceptions=False).get("/employees")
        self.assertEqual(r.status_code, 500)
        self.assertNotEqual(r.json() if r.headers.get("content-type", "").startswith("application/json") else None, BODY)


class TestExistingBehaviourPreserved(Base):
    def test_duplicate_key_is_still_409_with_its_own_detail(self):
        dup = DuplicateKeyError(f"E11000 duplicate key {SECRET}")
        m.db = _DB(dup, employees={"insert_one": lambda *_a, **_k: (_ for _ in ()).throw(dup)})
        r = self.c.post("/employees", json=EMP)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json(), {"detail": "emp_code already exists"})

    def test_punch_in_duplicate_is_still_409(self):
        dup = DuplicateKeyError("E11000")
        m.db = _DB(dup,
                   employees={"find_one": lambda *_a, **_k: {"emp_code": "EMP0001", "shift_start": "09:30",
                                                             "shift_end": "18:30", "department": "Eng"}},
                   attendance_logs={"insert_one": lambda *_a, **_k: (_ for _ in ()).throw(dup)})
        r = self.c.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertNotEqual(r.json(), BODY)

    def test_validation_is_still_422_and_never_reaches_the_database(self):
        m.db = _DB(_timeout())
        for method, url, body in (("POST", "/employees", {"emp_code": "bad"}),
                                  ("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": 1.5}),
                                  ("GET", "/analytics/leaderboard/late?month=2026-13", None),
                                  ("GET", "/analytics/departments/Eng/trend?from=2026-07-14&to=2026-07-06", None)):
            with self.subTest(request=f"{method} {url}"):
                r = self.c.request(method, url, json=body)
                self.assertEqual(r.status_code, 422, r.text)

    def test_unknown_employee_is_still_404(self):
        m.db = _DB(_timeout(), employees={"find_one": lambda *_a, **_k: None})
        r = self.c.post("/attendance/punch-out", json={"emp_code": "EMP0404"})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertNotEqual(r.json(), BODY)


# ---------------------------------------------------------------------------------------------------------------
# Punch-out: contract says 404 unknown employee / no punch-in found; 409 already punched out; 422 if
# punched_at <= punch_in or more than 24 h after it (exactly 24 h allowed). In-memory fake, MOCKED: it proves the
# HTTP mapping and the conditional close, not MongoDB behaviour.
import threading
from datetime import datetime, timedelta, timezone

PI = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)          # 09:30 IST on 2026-07-06
EMP_DOC = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}


def ms(dt):
    return int(dt.timestamp() * 1000)


class _Logs:
    """attendance_logs with the few operations punch-out uses; find_one_and_update is atomic (a lock)."""
    def __init__(self, docs, barrier=None):
        self.docs, self.barrier, self.lock, self.updates = [dict(d) for d in docs], barrier, threading.Lock(), 0

    def find_one(self, flt, projection=None, sort=None):
        rows = [d for d in self.docs if d["emp_code"] == flt["emp_code"] and d.get("punch_in") is not None
                and all({"$lte": lambda a, b: a <= b, "$gt": lambda a, b: a > b}[op](d["punch_in"], v)
                        for op, v in flt["punch_in"].items())]
        if sort:
            (field, direction), = sort
            rows.sort(key=lambda d: d[field], reverse=direction < 0)
        found = dict(rows[0]) if rows else None
        if found is not None and self.barrier is not None and "$lte" in flt["punch_in"]:
            self.barrier.wait(timeout=5)                 # both requests have read the OPEN record before either writes
        return found

    def find_one_and_update(self, flt, update, return_document=None):
        with self.lock:
            for d in self.docs:
                if d["_id"] == flt["_id"] and d["punch_in"] == flt["punch_in"] and d.get("punch_out") is None:
                    d.update(update["$set"])
                    self.updates += 1
                    return dict(d)
        return None


def _rec(punch_in=PI, punch_out=None, _id=1, date="2026-07-06"):
    return {"_id": _id, "emp_code": "EMP0001", "date": date, "status": "PRESENT", "punch_in": punch_in,
            "punch_out": punch_out, "late_minutes": 0, "work_hours": None, "overtime_minutes": 0,
            "half_day": False, "history": []}


def _db(docs, employee=True, barrier=None):
    logs = _Logs(docs, barrier)
    return _DB(RuntimeError("unexpected"),
               employees={"find_one": lambda *_a, **_k: dict(EMP_DOC) if employee else None},
               attendance_logs={"find_one": logs.find_one, "find_one_and_update": logs.find_one_and_update}), logs


class TestPunchOutContract(Base):
    def out(self, at, emp="EMP0001"):
        return self.c.post("/attendance/punch-out", json={"emp_code": emp, "punched_at": ms(at)})

    def test_unknown_employee_is_404(self):
        m.db, _ = _db([_rec()], employee=False)
        r = self.out(PI + timedelta(hours=9))
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "employee not found"}))

    def test_employee_with_no_records_is_404(self):
        m.db, _ = _db([])
        r = self.out(PI + timedelta(hours=9))
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "no punch-in found"}))

    def test_punch_out_before_the_open_punch_in_is_422(self):
        m.db, logs = _db([_rec()])
        for delta in (timedelta(seconds=-1), timedelta(hours=-3), timedelta(days=-2)):
            with self.subTest(delta=delta):
                r = self.out(PI + delta)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertEqual(r.json(), {"detail": "punched_at must be after punch_in"})
        self.assertEqual(logs.updates, 0)
        self.assertIsNone(logs.docs[0]["punch_out"])

    def test_punch_out_equal_to_punch_in_is_422_and_leaves_the_record_open(self):
        m.db, logs = _db([_rec()])
        r = self.out(PI)
        self.assertEqual((r.status_code, r.json()), (422, {"detail": "punched_at must be after punch_in"}))
        self.assertIsNone(logs.docs[0]["punch_out"])

    def test_punch_out_after_punch_in_is_200_with_derived_fields(self):
        m.db, logs = _db([_rec()])
        r = self.out(PI + timedelta(hours=9))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["punch_out"], body["work_hours"], body["overtime_minutes"], body["half_day"]),
                         (ms(PI + timedelta(hours=9)), 9.0, 0, False))

    def test_exactly_24_hours_is_allowed_and_one_second_more_is_422(self):
        m.db, _ = _db([_rec()])
        ok = self.out(PI + timedelta(hours=24))
        self.assertEqual((ok.status_code, ok.json()["work_hours"]), (200, 24.0))
        m.db, logs = _db([_rec()])
        bad = self.out(PI + timedelta(hours=24, seconds=1))
        self.assertEqual((bad.status_code, bad.json()), (422, {"detail": "punched_at must be within 24 hours of punch_in"}))
        self.assertIsNone(logs.docs[0]["punch_out"])

    def test_already_punched_out_is_409(self):
        m.db, _ = _db([_rec(punch_out=PI + timedelta(hours=9))])
        r = self.out(PI + timedelta(hours=10))
        self.assertEqual((r.status_code, r.json()), (409, {"detail": "already punched out"}))

    def test_punch_out_before_an_already_closed_later_record_is_422(self):
        # The contract says punched_at <= punch_in is 422; closed state does not change timestamp validity.
        m.db, _ = _db([_rec(punch_out=PI + timedelta(hours=9))])
        r = self.out(PI - timedelta(hours=1))
        self.assertEqual((r.status_code, r.json()), (422, {"detail": "punched_at must be after punch_in"}))

    def test_eligible_earlier_record_still_wins_over_a_later_open_one(self):
        # Contract: close the MOST RECENT record whose punch_in <= punched_at. Here that is the closed day-1 record.
        day1 = _rec(punch_in=PI - timedelta(days=1), punch_out=PI - timedelta(days=1) + timedelta(hours=9),
                    _id=1, date="2026-07-05")
        m.db, _ = _db([day1, _rec(_id=2)])
        r = self.out(PI - timedelta(hours=1))
        self.assertEqual(r.status_code, 409, r.text)

    def test_overnight_punch_out_across_midnight_is_200(self):
        night_in = datetime(2026, 7, 6, 16, 30, tzinfo=timezone.utc)        # 22:00 IST
        m.db, _ = _db([_rec(punch_in=night_in)])
        m.db.employees = _Coll(None, find_one=lambda *_a, **_k: {"emp_code": "EMP0001",
                                                                           "shift_start": "22:00", "shift_end": "06:00"})
        r = self.out(night_in + timedelta(hours=8, minutes=40))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["overtime_minutes"], 40)

    def test_losing_the_conditional_close_is_409(self):
        logs = _Logs([_rec()])
        logs.find_one_and_update = lambda *_a, **_k: None           # someone else closed it between read and write
        m.db = _DB(RuntimeError("x"), employees={"find_one": lambda *_a, **_k: dict(EMP_DOC)},
                   attendance_logs={"find_one": logs.find_one, "find_one_and_update": logs.find_one_and_update})
        r = self.out(PI + timedelta(hours=9))
        self.assertEqual(r.status_code, 409, r.text)

    def test_the_close_is_a_conditional_update_on_an_open_record(self):
        seen = []
        logs = _Logs([_rec()])
        orig = logs.find_one_and_update
        logs.find_one_and_update = lambda flt, upd, **k: (seen.append(flt), orig(flt, upd, **k))[1]
        m.db = _DB(RuntimeError("x"), employees={"find_one": lambda *_a, **_k: dict(EMP_DOC)},
                   attendance_logs={"find_one": logs.find_one, "find_one_and_update": logs.find_one_and_update})
        self.assertEqual(self.out(PI + timedelta(hours=9)).status_code, 200)
        self.assertEqual(seen, [{"_id": 1, "punch_in": PI, "punch_out": None}])

    def test_two_simultaneous_punch_outs_close_the_record_exactly_once(self):
        m.db, logs = _db([_rec()], barrier=threading.Barrier(2))
        results = []

        def go(offset):
            results.append(TestClient(m.app).post("/attendance/punch-out", json={
                "emp_code": "EMP0001", "punched_at": ms(PI + timedelta(hours=9, minutes=offset))}).status_code)
        threads = [threading.Thread(target=go, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(logs.updates, 1)


class TestHealthAndStartup(Base):
    def test_health_503_when_mongo_unreachable_and_body_is_fixed(self):
        m.db = _DB(_timeout())
        m._indexes_ready = False
        r = self.c.get("/health")
        self.assertEqual((r.status_code, r.json()), (503, BODY))
        self.assertNotIn("hunter2", r.text)

    def test_health_503_while_ping_works_but_index_creation_fails(self):
        m.db = _DB(OperationFailure("cannot build unique index"), command=lambda *_a, **_k: {"ok": 1})
        m._indexes_ready = False
        calls = []
        orig = m.ensure_indexes
        m.ensure_indexes = lambda database: (calls.append(1), (_ for _ in ()).throw(OperationFailure("dup rows")))
        try:
            r = self.c.get("/health")
        finally:
            m.ensure_indexes = orig
        self.assertEqual((r.status_code, r.json()), (503, BODY))
        self.assertFalse(m._indexes_ready)
        self.assertEqual(len(calls), 1)

    def test_health_200_only_after_indexes_are_ready_and_not_recreated_once_ready(self):
        m.db = _DB(None, command=lambda *_a, **_k: {"ok": 1})
        m._indexes_ready = False
        calls = []
        orig = m.ensure_indexes
        m.ensure_indexes = lambda database: calls.append(1) or []
        try:
            first = self.c.get("/health")
            second = self.c.get("/health")
        finally:
            m.ensure_indexes = orig
        self.assertEqual((first.status_code, first.json()), (200, {"status": "ok"}))
        self.assertEqual(second.status_code, 200)
        self.assertTrue(m._indexes_ready)
        self.assertEqual(len(calls), 1)

    def test_startup_tolerates_connectivity_failure_and_health_then_reports_503(self):
        m.db = _DB(_timeout())
        m._indexes_ready = False
        orig = m.ensure_indexes
        m.ensure_indexes = lambda database: (_ for _ in ()).throw(_timeout())
        try:
            with TestClient(m.app) as c:               # runs the lifespan: must NOT raise
                self.assertEqual(c.get("/health").status_code, 503)
        finally:
            m.ensure_indexes = orig
        self.assertFalse(m._indexes_ready)

    def test_startup_fails_loudly_on_a_non_connectivity_index_error(self):
        m.db = _DB(None, command=lambda *_a, **_k: {"ok": 1})
        m._indexes_ready = False
        orig = m.ensure_indexes
        m.ensure_indexes = lambda database: (_ for _ in ()).throw(OperationFailure("E11000 duplicate key, index build"))
        try:
            with self.assertRaises(OperationFailure):
                with TestClient(m.app):
                    pass
        finally:
            m.ensure_indexes = orig
        self.assertFalse(m._indexes_ready)

    def test_startup_marks_ready_when_indexes_succeed(self):
        m.db = _DB(None, command=lambda *_a, **_k: {"ok": 1})
        m._indexes_ready = False
        orig = m.ensure_indexes
        m.ensure_indexes = lambda database: []
        try:
            with TestClient(m.app) as c:
                self.assertEqual(c.get("/health").status_code, 200)
        finally:
            m.ensure_indexes = orig
        self.assertTrue(m._indexes_ready)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROBE = ("import sys, json; sys.path.insert(0, sys.argv[1]); import app.main as m; "
          "print(json.dumps({'db': m.db.name, 'nodes': sorted('%s:%s' % n for n in "
          "m.client.topology_description.server_descriptions())}))")


class TestMongoConfiguration(unittest.TestCase):
    """The assignment says: read MONGO_URI and MONGO_DB from the environment, real environment variables win over a
    local .env, never hard-code connection details. It does not make MONGO_URI mandatory, so an unset MONGO_URI keeps
    PyMongo's own default host (documented in README.md / DECISIONS.md). MongoClient connects lazily, so these
    subprocess imports need no MongoDB."""

    def probe(self, root, **env):
        clean = {k: v for k, v in os.environ.items() if k not in ("MONGO_URI", "MONGO_DB")}
        clean.update(env)
        out = subprocess.run([sys.executable, "-c", _PROBE, root], env=clean, cwd=root, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        import json
        return json.loads(out.stdout.strip().splitlines()[-1])

    def copy_app(self, tmp):
        os.makedirs(os.path.join(tmp, "app"))
        shutil.copy(os.path.join(ROOT, "app", "main.py"), os.path.join(tmp, "app", "main.py"))
        open(os.path.join(tmp, "app", "__init__.py"), "w").close()

    def test_configured_values_are_used(self):
        got = self.probe(ROOT, MONGO_URI="mongodb://cfg-host.invalid:27018", MONGO_DB="cfgdb")
        self.assertEqual(got, {"db": "cfgdb", "nodes": ["cfg-host.invalid:27018"]})

    def test_unset_values_use_documented_defaults_and_do_not_break_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.copy_app(tmp)                                  # a copy with no .env above it, so nothing leaks in
            got = self.probe(tmp)
        self.assertEqual(got, {"db": "attendance_db", "nodes": ["localhost:27017"]})

    def test_empty_uri_behaves_like_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.copy_app(tmp)
            self.assertEqual(self.probe(tmp, MONGO_URI="")["nodes"], ["localhost:27017"])

    def test_real_environment_beats_dotenv_and_dotenv_fills_the_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.copy_app(tmp)
            with open(os.path.join(tmp, ".env"), "w") as fh:
                fh.write("MONGO_URI=mongodb://from-file.invalid:1111\nMONGO_DB=fromfile\n")
            both = self.probe(tmp, MONGO_URI="mongodb://from-env.invalid:2222", MONGO_DB="fromenv")
            only_file = self.probe(tmp)
        self.assertEqual(both, {"db": "fromenv", "nodes": ["from-env.invalid:2222"]})
        self.assertEqual(only_file, {"db": "fromfile", "nodes": ["from-file.invalid:1111"]})

    def test_source_has_no_hard_coded_connection_details(self):
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        for needle in ("mongodb://", "mongodb+srv", "localhost", "127.0.0.1"):
            self.assertNotIn(needle, src)


class TestIndexDefinitions(unittest.TestCase):
    def test_required_indexes_present_and_unique_ones_flagged(self):
        emp = {tuple(k): o for k, o in m.INDEXES["employees"]}
        att = {tuple(k): o for k, o in m.INDEXES["attendance_logs"]}
        self.assertTrue(emp[(("emp_code", 1),)].get("unique"))
        self.assertIn((("department", 1), ("emp_code", 1)), emp)
        self.assertTrue(att[(("emp_code", 1), ("date", 1))].get("unique"))
        for keys in ((("date", -1), ("emp_code", 1)), (("status", 1), ("date", -1), ("emp_code", 1)),
                     (("emp_code", 1), ("punch_in", -1))):
            self.assertIn(keys, att)

    def test_ensure_indexes_is_one_create_index_per_spec_and_propagates_errors(self):
        class C:
            def __init__(self):
                self.n = []

            def create_index(self, keys, **o):
                self.n.append((keys, o))
                return "x"

        class D(dict):
            pass
        d = D(employees=C(), attendance_logs=C())
        out = m.ensure_indexes(d)
        self.assertEqual(len(out), sum(len(v) for v in m.INDEXES.values()))

        class Bad:
            def create_index(self, *a, **k):
                raise OperationFailure("boom")
        with self.assertRaises(OperationFailure):
            m.ensure_indexes(D(employees=Bad(), attendance_logs=Bad()))

    def test_no_connection_string_or_credential_is_hard_coded(self):
        with open(m.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("mongodb://", src)
        self.assertNotIn("mongodb+srv", src)


if __name__ == "__main__":
    unittest.main()
