"""Phase 5.5 tests: analytics contract audit. Finding F1: GET /admin/explain/{endpoint} answered 501 for attendance_list,
employee_monthly and department_summary although openapi.yaml / PROBLEM_STATEMENT (Part C) require all five targets.

  1. fake-database tests (MOCKED: routing, 422 mapping, "explain reuses the endpoint's builder", shapes)
  2. TestMongoExplainAll   REAL MongoDB, opt-in via TEST_MONGO_URI (disposable attendance_test_p55_<random> database):
                           strict IXSCAN / no COLLSCAN checks for all five targets on ~100,000 attendance records.
                           SKIPPED (not passed) when TEST_MONGO_URI is unset.
Run:  python -m unittest tests.test_phase5_5 -v"""
import json
import os
import random
import unittest
import uuid

from app import main as m
from tests._explain_util import describe
from tests.test_phase5_4 import (FakeDbCase, HAVE_HTTP, TEST_URI, emp, lg, plan_exposes_collection_scans, plan_problems,
                                 plan_totals, seed_big, week_world)

ALL_TARGETS = ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend")
# One valid query string per target.
VALID_QS = {
    "attendance_list": "",
    "employee_monthly": "emp_code=E001&month=2026-07",
    "department_summary": "month=2026-07",
    "late_leaderboard": "month=2026-07",
    "department_trend": "department=Eng&from=2026-07-06&to=2026-07-14",
}


@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestExplainAllTargets(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)

    def explain(self, target, qs=None):
        qs = VALID_QS[target] if qs is None else qs
        return self.c.get(f"/admin/explain/{target}" + (f"?{qs}" if qs else ""))

    def test_every_documented_target_is_implemented(self):
        for target in ALL_TARGETS:
            with self.subTest(target=target):
                r = self.explain(target)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(sorted(r.json()), ["collection", "endpoint", "explain"])
                self.assertEqual(r.json()["endpoint"], target)
        self.assertEqual(len(self.fake.commands), 5)
        self.assertTrue(all(c["verbosity"] == "executionStats" for c in self.fake.commands))

    def test_unknown_target_is_422(self):
        self.assertEqual(self.c.get("/admin/explain/nope").status_code, 422)

    # ---- employee_monthly
    def test_employee_monthly_explains_the_endpoints_pipeline(self):
        r = self.explain("employee_monthly", "emp_code=EMP0001&month=2026-07")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["collection"], "attendance_logs")
        (cmd,) = self.fake.commands
        self.assertEqual(cmd["explain"], {"aggregate": "attendance_logs", "cursor": {},
                                          "pipeline": m.employee_monthly_pipeline("EMP0001", 2026, 7)})

    def test_employee_monthly_requires_emp_code_and_a_valid_month(self):
        for qs in ("", "emp_code=EMP0001", "month=2026-07", "emp_code=EMP0001&month=2026-13", "emp_code=EMP0001&month=0000-01",
                   "emp_code=EMP0001&month=2026-7"):
            with self.subTest(qs=qs):
                self.assertEqual(self.explain("employee_monthly", qs).status_code, 422)
        self.assertEqual(self.fake.commands, [])
        self.assertEqual(sorted(e["loc"][-1] for e in self.explain("employee_monthly", "").json()["detail"]),
                         ["emp_code", "month"])

    def test_employee_monthly_unknown_employee_is_explained_not_404(self):
        self.assertEqual(self.explain("employee_monthly", "emp_code=NOBODY&month=2026-07").status_code, 200)

    # ---- department_summary
    def test_department_summary_explains_the_endpoints_pipeline(self):
        r = self.explain("department_summary", "month=2028-02&department=Eng")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["collection"], "employees")
        (cmd,) = self.fake.commands
        self.assertEqual(cmd["explain"], {"aggregate": "employees", "cursor": {},
                                          "pipeline": m.department_summary_pipeline(2028, 2, "Eng")})
        self.explain("department_summary", "month=2028-02")
        self.assertEqual(self.fake.commands[1]["explain"]["pipeline"], m.department_summary_pipeline(2028, 2, None))

    def test_department_summary_requires_a_valid_month(self):
        for qs in ("", "department=Eng", "month=2026-13", "month=abc"):
            with self.subTest(qs=qs):
                self.assertEqual(self.explain("department_summary", qs).status_code, 422)
        self.assertEqual(self.fake.commands, [])

    # ---- attendance_list
    def test_attendance_list_explains_a_find_built_from_the_endpoints_helpers(self):
        qs = "emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31&status=WFH&page=3&page_size=25"
        r = self.explain("attendance_list", qs)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["collection"], "attendance_logs")
        (cmd,) = self.fake.commands
        self.assertEqual(cmd["explain"], {
            "find": "attendance_logs", "projection": {"_id": 0}, "skip": 50, "limit": 25,
            "filter": m.attendance_list_filter("EMP0001", "2026-07-01", "2026-07-31", "WFH"),
            "sort": {"date": -1, "emp_code": 1}})
        self.assertEqual(list(cmd["explain"]["sort"].items()), m.ATTENDANCE_SORT)

    def test_attendance_list_defaults_match_the_endpoint_defaults(self):
        self.explain("attendance_list", "")
        (cmd,) = self.fake.commands
        self.assertEqual((cmd["explain"]["filter"], cmd["explain"]["skip"], cmd["explain"]["limit"]),
                         ({}, 0, m.DEFAULT_PAGE_SIZE))

    def test_attendance_list_filter_equals_the_filter_the_endpoint_queries_with(self):
        seen = {}

        class Logs:
            calls = []

            def count_documents(self, q):
                seen["count"] = q
                return 0

            def find(self, q, *a, **k):
                seen["find"] = q
                raise AssertionError("skip >= total, find must not run")
        self.fake.attendance_logs = Logs()
        qs = "emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31&status=PRESENT"
        self.assertEqual(self.c.get("/attendance?" + qs).status_code, 200)
        self.explain("attendance_list", qs)
        self.assertEqual(self.fake.commands[0]["explain"]["filter"], seen["count"])

    def test_attendance_list_validation_matches_the_endpoint(self):
        for qs in ("date_from=2026-02-30", "date_to=bad", "date_from=2026-07-31&date_to=2026-07-01", "status=NOPE",
                   "page=0", "page_size=0", "page_size=101", "page=x", "date_from=20260701"):
            with self.subTest(qs=qs):
                self.assertEqual(self.explain("attendance_list", qs).status_code, 422)
                self.assertEqual(self.c.get("/attendance?" + qs).status_code, 422)
        self.assertEqual(self.fake.commands, [])

    # ---- the older targets keep their behaviour
    def test_late_leaderboard_and_trend_still_require_their_parameters(self):
        self.assertEqual(self.explain("late_leaderboard", "").status_code, 422)
        self.assertEqual(self.explain("department_trend", "department=Eng").status_code, 422)
        self.assertEqual(self.fake.commands, [])


# ======================================================================== REAL MongoDB (opt-in)
@unittest.skipUnless(HAVE_HTTP and TEST_URI, "set TEST_MONGO_URI to run the real-MongoDB integration tests: NOT EXECUTED")
class TestMongoExplainAll(unittest.TestCase):
    PREFIX = "attendance_test_p55_"

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from pymongo import MongoClient
        cls.client = MongoClient(TEST_URI, tz_aware=True, serverSelectionTimeoutMS=4000)
        cls.client.admin.command("ping")
        cls.dbname = cls.PREFIX + uuid.uuid4().hex[:8]
        cls.tdb = cls.client[cls.dbname]
        cls._orig_db, cls._orig_ready = m.db, m._indexes_ready
        m.db = cls.tdb
        m.ensure_indexes(cls.tdb)
        m._indexes_ready = True
        cls.server_version = cls.client.server_info().get("version")
        emps, logs = seed_big(random.Random(7))
        for coll, docs in (("employees", emps), ("attendance_logs", logs)):
            for i in range(0, len(docs), 5000):
                cls.tdb[coll].insert_many([dict(d) for d in docs[i:i + 5000]])
        cls.c = TestClient(m.app)

    @classmethod
    def tearDownClass(cls):
        m.db, m._indexes_ready = cls._orig_db, cls._orig_ready
        if cls.dbname.startswith(cls.PREFIX):
            cls.client.drop_database(cls.dbname)
        cls.client.close()

    CASES = (
        ("attendance_list default", "attendance_list", ""),
        ("attendance_list emp_code", "attendance_list", "emp_code=E00010"),
        ("attendance_list date range", "attendance_list", "date_from=2026-07-01&date_to=2026-07-31"),
        ("attendance_list status", "attendance_list", "status=WFH"),
        ("attendance_list emp_code+range+status", "attendance_list", "emp_code=E00010&date_from=2026-07-01&date_to=2026-09-30&status=PRESENT&page=2"),
        ("employee_monthly", "employee_monthly", "emp_code=E00010&month=2026-07"),
        ("department_summary unfiltered", "department_summary", "month=2026-07"),
        ("department_summary filtered", "department_summary", "month=2026-07&department=Engineering"),
        ("late_leaderboard unfiltered", "late_leaderboard", "month=2026-07"),
        ("late_leaderboard filtered", "late_leaderboard", "month=2026-07&department=Engineering"),
        ("department_trend wide", "department_trend", "department=BigDept&from=2026-07-01&to=2026-09-30"),
        ("department_trend one day", "department_trend", "department=Engineering&from=2026-07-06&to=2026-07-06"),
    )

    def test_every_target_uses_an_index_and_no_collection_scan(self):
        self.assertGreaterEqual(self.tdb.attendance_logs.count_documents({}), 95_000)
        for label, target, qs in self.CASES:
            with self.subTest(label=label):
                r = self.c.get(f"/admin/explain/{target}" + (f"?{qs}" if qs else ""))
                self.assertEqual(r.status_code, 200, r.text)
                out = r.json()["explain"]
                print(f"\n[explain {label}] server={self.server_version} {describe(out)} totals={plan_totals(out)} "
                      f"collectionScans_exposed={plan_exposes_collection_scans(out)}")
                self.assertEqual(plan_problems(out), [], f"{label}: {describe(out)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
