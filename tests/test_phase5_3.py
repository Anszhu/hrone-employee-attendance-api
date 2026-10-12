"""Phase 5.3 tests: GET /analytics/leaderboard/late?month=YYYY-MM[&limit=N][&department=...]
and GET /admin/explain/late_leaderboard

Layers (each says what it proves):
  1. TestPipelineShape / TestBuildResponse      pure; run anywhere.
  2. TestPipelineSemanticsEmulated              the REAL pipeline run by tests/_agg_emulator_p53.py, an EMULATION of
                                                the MongoDB operators it uses (not MongoDB), compared with an
                                                independent oracle.
  3. TestLeaderboardEndpointFake / TestLeaderboardHttp / TestExplainHttpFake
                                                the endpoints on an in-memory fake whose aggregate() uses the
                                                emulator. MOCKED: proves routing, 422 mapping, "one aggregation,
                                                no find", "explain reuses the builder".
  4. TestMongoLeaderboard                       REAL MongoDB, opt-in via TEST_MONGO_URI; disposable database
                                                attendance_test_p53_<random>, dropped afterwards. Never touches
                                                MONGO_DB. Includes the strict EXPLAIN checks (IXSCAN present,
                                                COLLSCAN absent) on ~100,000 attendance records.
"""
import calendar
import json
import os
import random
import unittest
import uuid

from tests._agg_emulator_p53 import mini_aggregate
from tests._explain_util import describe, summarize
from tests._loader import ROOT, have, load_main

m, MODE = load_main()
FULL = MODE == "full"
HAVE_HTTP = FULL and have("httpx")
TEST_URI = os.environ.get("TEST_MONGO_URI")

ITEM_KEYS = ["rank", "emp_code", "name", "department", "total_late_minutes", "late_count"]
ANY = object()


def emp(code, dept="Eng", name=None, **extra):
    d = {"emp_code": code, "department": dept, "name": name or f"Name {code}", "email": "n@x.io",
         "joined_on": "2026-01-05", "shift_start": "09:30", "shift_end": "18:30"}
    d.update(extra)
    return d


def lg(code, date, late=0, status="PRESENT", **extra):
    d = {"emp_code": code, "date": date, "status": status, "late_minutes": late, "half_day": False,
         "work_hours": None, "overtime_minutes": 0, "history": []}
    d.update(extra)
    return d


def run(emps, logs, year=2026, month=7, limit=10, department=None):
    """The real pipeline on the emulator -> the real response builder."""
    rows = mini_aggregate(logs, m.late_leaderboard_pipeline(year, month, limit, department), {"employees": emps})
    return m.build_late_leaderboard(f"{year:04d}-{month:02d}", rows)


def oracle(emps, logs, year, month, limit, department=None):
    """Independent algorithm: plain loops, rank = 1 + number of employees with a strictly larger total."""
    first = f"{year:04d}-{month:02d}-01"
    last = f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
    by_code = {e["emp_code"]: e for e in emps}
    tot, cnt = {}, {}
    for l in logs:
        if not (first <= l["date"] <= last):
            continue
        v = l.get("late_minutes")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            tot[l["emp_code"]] = tot.get(l["emp_code"], 0) + v
            cnt[l["emp_code"]] = cnt.get(l["emp_code"], 0) + 1
    rows = []
    for code, t in tot.items():
        e = by_code.get(code)
        if e is None or not isinstance(e.get("department"), str):
            continue
        if department is not None and e["department"] != department:
            continue
        rows.append((code, e["name"], e["department"], t, cnt[code]))
    out = []
    for code, name, dept, t, c in rows:
        rank = 1 + sum(1 for r in rows if r[3] > t)
        if rank <= limit:
            out.append({"rank": rank, "emp_code": code, "name": name, "department": dept,
                        "total_late_minutes": t, "late_count": c})
    out.sort(key=lambda r: (-r["total_late_minutes"], r["emp_code"]))
    return {"month": f"{year:04d}-{month:02d}", "items": out}


def tied_world():
    """Totals 100, 90, 90, 80 (+ one employee with nothing late)."""
    emps = [emp("E1"), emp("E2"), emp("E3"), emp("E4"), emp("E5")]
    logs = [lg("E1", "2026-07-01", 100), lg("E2", "2026-07-01", 90), lg("E3", "2026-07-01", 90),
            lg("E4", "2026-07-01", 80), lg("E5", "2026-07-01", 0)]
    return emps, logs


# ======================================================================== 1. pure
class TestPipelineShape(unittest.TestCase):
    def stages(self, **kw):
        args = dict(year=2026, month=7, limit=10, department=None)
        args.update(kw)
        return m.late_leaderboard_pipeline(**args)

    def names(self, p):
        return [next(iter(s)) for s in p]

    def test_stage_order_without_department(self):
        self.assertEqual(self.names(self.stages()), ["$match", "$group", "$match", "$lookup", "$unwind", "$match",
                                                     "$setWindowFields", "$match", "$sort", "$project"])

    def test_first_stage_is_the_inclusive_month_range_on_date(self):
        self.assertEqual(self.stages()[0], {"$match": {"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}}})
        self.assertEqual(self.stages(year=2028, month=2)[0]["$match"]["date"]["$lte"], "2028-02-29")
        self.assertEqual(self.stages(year=2026, month=2)[0]["$match"]["date"]["$lte"], "2026-02-28")

    def test_window_sorts_by_total_only_so_ties_stay_ties(self):
        sw = self.stages()[6]["$setWindowFields"]
        self.assertEqual(sw["sortBy"], {"total_late_minutes": -1})
        self.assertEqual(sw["output"], {"rank": {"$rank": {}}})
        self.assertNotIn("partitionBy", sw)

    def test_limit_is_a_rank_filter_after_ranking_never_a_limit_stage(self):
        p = self.stages(limit=2)
        self.assertEqual(p[7], {"$match": {"rank": {"$lte": 2}}})
        self.assertNotIn("$limit", self.names(p))
        self.assertNotIn("$skip", self.names(p))

    def test_final_sort_is_total_desc_then_emp_code_asc(self):
        self.assertEqual(list(self.stages()[8]["$sort"].items()), [("total_late_minutes", -1), ("_id", 1)])

    def test_department_match_precedes_ranking_and_is_exact(self):
        string_only = {"$eq": [{"$type": "$emp.department"}, "string"]}
        self.assertEqual(self.stages()[5], {"$match": {"$expr": string_only}})     # no filter, non-strings excluded
        for dept in ("Sales", "", "sales"):
            p = self.stages(department=dept)
            self.assertEqual(p[5], {"$match": {"$expr": {"$and": [string_only, {"$eq": ["$emp.department", dept]}]}}})
            self.assertLess(5, self.names(p).index("$setWindowFields"))

    def test_department_type_check_is_an_expression_not_the_array_matching_query_form(self):
        text = json.dumps(self.stages(department="Engineering")[5])
        self.assertIn("$expr", text)
        self.assertNotIn('"emp.department"', text.replace("$emp.department", ""))   # no query-style field predicate

    def test_uses_stored_late_minutes_only(self):
        text = json.dumps(self.stages())
        for banned in ("punch_in", "shift_start", "punch_out"):
            self.assertNotIn(banned, text)

    def test_collection_constant_and_projection(self):
        self.assertEqual(m.LATE_LEADERBOARD_COLLECTION, "attendance_logs")
        proj = self.stages()[-1]["$project"]
        self.assertEqual(proj["_id"], 0)
        self.assertEqual(set(proj), {"_id", "rank", "emp_code", "name", "department", "total_late_minutes", "late_count"})


class TestBuildResponse(unittest.TestCase):
    def test_exact_keys_order_and_int_coercion(self):
        r = m.build_late_leaderboard("2026-07", [
            {"rank": 1, "emp_code": "E1", "name": "A", "department": "X", "total_late_minutes": 10.0, "late_count": 2}])
        self.assertEqual(list(r), ["month", "items"])
        self.assertEqual(list(r["items"][0]), ITEM_KEYS)
        self.assertIsInstance(r["items"][0]["total_late_minutes"], int)

    def test_empty(self):
        self.assertEqual(m.build_late_leaderboard("2026-07", []), {"month": "2026-07", "items": []})


# ======================================================================== 2. emulated semantics
class TestPipelineSemanticsEmulated(unittest.TestCase):
    def test_no_matching_attendance(self):
        self.assertEqual(run([emp("E1")], []), {"month": "2026-07", "items": []})
        self.assertEqual(run([emp("E1")], [lg("E1", "2026-06-30", 50)])["items"], [])

    def test_missing_null_zero_negative_late_minutes(self):
        emps = [emp("E1"), emp("E2"), emp("E3"), emp("E4"), emp("E5")]
        missing = lg("E1", "2026-07-01"); del missing["late_minutes"]
        logs = [missing, lg("E2", "2026-07-01", None), lg("E3", "2026-07-01", 0), lg("E4", "2026-07-01", -30),
                lg("E5", "2026-07-01", 12), lg("E5", "2026-07-02", -100), lg("E5", "2026-07-03", None)]
        r = run(emps, logs)
        self.assertEqual(r["items"], [{"rank": 1, "emp_code": "E5", "name": "Name E5", "department": "Eng",
                                       "total_late_minutes": 12, "late_count": 1}])    # negatives neither subtract nor count

    def test_several_records_sum_and_count(self):
        r = run([emp("E1")], [lg("E1", "2026-07-01", 10), lg("E1", "2026-07-02", 0), lg("E1", "2026-07-03", 25),
                              lg("E1", "2026-07-04", 5)])
        self.assertEqual((r["items"][0]["total_late_minutes"], r["items"][0]["late_count"]), (40, 3))

    def test_orphan_logs_are_ignored(self):
        r = run([emp("E1")], [lg("E1", "2026-07-01", 5), lg("GHOST", "2026-07-01", 500)])
        self.assertEqual([i["emp_code"] for i in r["items"]], ["E1"])
        self.assertEqual(r["items"][0]["rank"], 1)                                      # the orphan never took rank 1

    def test_zero_total_employees_are_excluded(self):
        r = run([emp("E1"), emp("E2")], [lg("E1", "2026-07-01", 0), lg("E2", "2026-07-01", -5)])
        self.assertEqual(r["items"], [])

    def test_weekends_and_absent_leave_records_with_positive_lateness_count(self):
        # 2026-07-04 is a Saturday, 2026-07-05 a Sunday
        r = run([emp("E1"), emp("E2"), emp("E3")], [
            lg("E1", "2026-07-04", 10), lg("E1", "2026-07-05", 20),
            lg("E2", "2026-07-06", 7, status="ABSENT"), lg("E3", "2026-07-06", 9, status="LEAVE")])
        self.assertEqual({i["emp_code"]: (i["total_late_minutes"], i["late_count"]) for i in r["items"]},
                         {"E1": (30, 2), "E2": (7, 1), "E3": (9, 1)})

    def test_month_boundaries_are_inclusive_and_leap_february(self):
        logs = [lg("E1", d, 1) for d in ("2026-06-30", "2026-07-01", "2026-07-31", "2026-08-01")]
        self.assertEqual(run([emp("E1")], logs)["items"][0]["late_count"], 2)
        logs = [lg("E1", d, 1) for d in ("2028-01-31", "2028-02-01", "2028-02-29", "2028-03-01")]
        self.assertEqual(run([emp("E1")], logs, 2028, 2)["items"][0]["late_count"], 2)
        logs = [lg("E1", d, 1) for d in ("2026-02-28", "2026-03-01")]
        self.assertEqual(run([emp("E1")], logs, 2026, 2)["items"][0]["late_count"], 1)

    def test_competition_ranks_1_2_2_4(self):
        r = run(*tied_world())
        self.assertEqual([(i["emp_code"], i["rank"], i["total_late_minutes"]) for i in r["items"]],
                         [("E1", 1, 100), ("E2", 2, 90), ("E3", 2, 90), ("E4", 4, 80)])

    def test_limit_applies_to_ranks_not_rows(self):
        emps, logs = tied_world()
        self.assertEqual([(i["emp_code"], i["rank"]) for i in run(emps, logs, limit=2)["items"]],
                         [("E1", 1), ("E2", 2), ("E3", 2)])                             # a tie at the cutoff: 3 rows
        self.assertEqual([i["emp_code"] for i in run(emps, logs, limit=1)["items"]], ["E1"])
        self.assertEqual(len(run(emps, logs, limit=3)["items"]), 3)                     # rank 3 does not exist
        self.assertEqual(len(run(emps, logs, limit=4)["items"]), 4)

    def test_tie_at_rank_one_returns_everyone_tied_even_with_limit_1(self):
        emps = [emp("E3"), emp("E1"), emp("E2")]
        logs = [lg("E3", "2026-07-01", 60), lg("E1", "2026-07-01", 60), lg("E2", "2026-07-01", 10)]
        r = run(emps, logs, limit=1)
        self.assertEqual([(i["emp_code"], i["rank"]) for i in r["items"]], [("E1", 1), ("E3", 1)])

    def test_final_order_total_desc_then_emp_code_asc(self):
        emps = [emp(c) for c in ("E9", "E2", "E5", "E1")]
        logs = [lg("E9", "2026-07-01", 50), lg("E2", "2026-07-01", 50), lg("E5", "2026-07-01", 70), lg("E1", "2026-07-01", 50)]
        self.assertEqual([i["emp_code"] for i in run(emps, logs)["items"]], ["E5", "E1", "E2", "E9"])

    def test_department_filter_exact_case_sensitive_and_ranks_within_department(self):
        emps = [emp("E1", "Sales"), emp("E2", "Eng"), emp("E3", "Sales"), emp("E4", "sales")]
        logs = [lg("E1", "2026-07-01", 10), lg("E2", "2026-07-01", 500), lg("E3", "2026-07-01", 30),
                lg("E4", "2026-07-01", 900)]
        r = run(emps, logs, department="Sales")
        self.assertEqual([(i["emp_code"], i["rank"], i["department"]) for i in r["items"]], [("E3", 1, "Sales"), ("E1", 2, "Sales")])
        self.assertEqual(run(emps, logs, department="SALES")["items"], [])
        self.assertEqual(run(emps, logs, department="Nowhere")["items"], [])
        # the department is applied BEFORE ranking: with limit=1 the Sales leader is still returned
        self.assertEqual([i["emp_code"] for i in run(emps, logs, limit=1, department="Sales")["items"]], ["E3"])
        self.assertEqual([i["rank"] for i in run(emps, logs)["items"]], [1, 2, 3, 4])    # unfiltered: global ranks

    def test_missing_or_non_string_department_is_excluded_and_does_not_take_a_rank(self):
        no_dept = emp("E1"); del no_dept["department"]
        emps = [no_dept, emp("E2", None), emp("E3", 5), emp("E4", "Eng")]
        logs = [lg(c, "2026-07-01", 100 - i) for i, c in enumerate(("E1", "E2", "E3", "E4"))]
        r = run(emps, logs)
        self.assertEqual([(i["emp_code"], i["rank"]) for i in r["items"]], [("E4", 1)])

    def dept_world(self):
        """One employee per department shape, all with positive lateness."""
        no_dept = emp("E_MISSING"); del no_dept["department"]
        emps = [emp("E_ARRAY", ["Engineering"]), emp("E_ARRAY2", ["Sales", "Engineering"]), emp("E_NUM", 5),
                emp("E_FLOAT", 1.5), emp("E_BOOL", True), emp("E_OBJ", {"name": "Engineering"}),
                emp("E_NULL", None), no_dept, emp("E_STR", "Engineering"), emp("E_EMPTY", "")]
        logs = [lg(e["emp_code"], "2026-07-01", 10 + i) for i, e in enumerate(emps)]
        return emps, logs

    def test_regression_array_department_containing_the_string_is_excluded(self):
        emps = [emp("E1", ["Engineering"]), emp("E2", "Engineering")]
        logs = [lg("E1", "2026-07-01", 500), lg("E2", "2026-07-01", 10)]
        for dept in (None, "Engineering"):
            with self.subTest(department=dept):
                r = run(emps, logs, department=dept)
                self.assertEqual([(i["emp_code"], i["rank"]) for i in r["items"]], [("E2", 1)])   # the array did NOT take rank 1

    def test_regression_number_missing_null_and_other_non_string_departments_are_excluded(self):
        emps, logs = self.dept_world()
        for dept in (None, "Engineering", ""):
            with self.subTest(department=dept):
                got = {i["emp_code"] for i in run(emps, logs, department=dept)["items"]}
                self.assertEqual(got, {"E_STR", "E_EMPTY"} if dept is None else {"E_STR"} if dept == "Engineering" else {"E_EMPTY"})

    def test_regression_valid_string_department_is_kept_and_ranked_among_strings_only(self):
        emps, logs = self.dept_world()
        r = run(emps, logs)
        self.assertEqual([(i["emp_code"], i["rank"], i["department"]) for i in r["items"]],
                         [("E_EMPTY", 1, ""), ("E_STR", 2, "Engineering")])      # 18 > 17: only string departments ranked

    def test_regression_explicit_empty_string_filters_exactly(self):
        emps, logs = self.dept_world()
        self.assertEqual([i["emp_code"] for i in run(emps, logs, department="")["items"]], ["E_EMPTY"])
        self.assertEqual(run([emp("E1", ["" ])], [lg("E1", "2026-07-01", 5)], department="")["items"], [])   # [""] is not ""

    def test_the_emulator_reproduces_the_array_pitfall_of_the_old_query_predicate(self):
        """Sensitivity check: with the OLD query-form predicate the emulator (which follows MongoDB's array-matching
        rule) lets an array department through, so the regression tests above can actually fail."""
        old_stage = {"$match": {"emp.department": {"$type": "string", "$eq": "Engineering"}}}
        rows = mini_aggregate([{"emp": {"department": ["Engineering"]}}, {"emp": {"department": "Engineering"}},
                               {"emp": {"department": 5}}], [old_stage])
        self.assertEqual(len(rows), 2)

    def test_omitted_department_means_no_filter_and_empty_string_is_an_exact_match(self):
        emps = [emp("E1", "Eng"), emp("E2", ""), emp("E3", "Sales")]
        logs = [lg("E1", "2026-07-01", 1), lg("E2", "2026-07-01", 2), lg("E3", "2026-07-01", 3)]
        self.assertEqual(len(run(emps, logs, department=None)["items"]), 3)
        r = run(emps, logs, department="")
        self.assertEqual([(i["emp_code"], i["department"], i["rank"]) for i in r["items"]], [("E2", "", 1)])
        self.assertEqual(run([emp("E1", "Eng")], [lg("E1", "2026-07-01", 1)], department="")["items"], [])

    def test_random_worlds_match_the_oracle(self):
        for seed in range(25):
            rng = random.Random(seed)
            emps, logs = random_world(rng, 40, 8)
            for limit in (1, 2, 5, 50):
                for dept in (None, "Eng", "eng", "", "Sales"):
                    with self.subTest(seed=seed, limit=limit, dept=dept):
                        self.assertEqual(run(emps, logs, limit=limit, department=dept),
                                         oracle(emps, logs, 2026, 7, limit, dept))

    def test_unsupported_operators_still_raise(self):
        with self.assertRaises(NotImplementedError):
            mini_aggregate([], [{"$bucket": {}}])


def random_world(rng, n_emps, per, months=(7,), invalid=True):
    depts = ["Eng", "Sales", "eng", "", "Ops"]
    emps, logs = [], []
    for i in range(n_emps):
        dept = rng.choice(depts)
        if invalid and i % 7 == 0:
            dept = rng.choice([None, 5, "MISSING"])
        e = emp(f"E{i:05d}", dept)
        if dept == "MISSING":
            del e["department"]
        emps.append(e)
        for month in months:
            for day in rng.sample(range(1, calendar.monthrange(2026, month)[1] + 1), per):
                d = lg(e["emp_code"], f"2026-{month:02d}-{day:02d}", rng.choice([0, 0, 5, 10, 11, 45, -5, None, 10]),
                       status=rng.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]))
                if rng.random() < 0.05:
                    del d["late_minutes"]
                logs.append(d)
    for k in range(3):                                                  # orphans
        logs.append(lg(f"GHOST{k}", "2026-07-03", 999))
    return emps, logs


# ======================================================================== 3. endpoint on fakes
@unittest.skipUnless(FULL, "needs fastapi/pydantic/pymongo installed: NOT EXECUTED")
class FakeDbCase(unittest.TestCase):
    def setUp(self):
        from tests._fakes_p53 import FakeDBP53
        self.fake = FakeDBP53()
        self._orig_db = m.db
        m.db = self.fake

    def tearDown(self):
        m.db = self._orig_db

    def load(self, emps, logs):
        for i, e in enumerate(emps):
            self.fake.employees.docs.append({"_id": f"e{i}", **e})
        for i, l in enumerate(logs):
            self.fake.attendance_logs.docs.append({"_id": f"a{i}", **l})


class TestLeaderboardEndpointFake(FakeDbCase):
    def test_envelope_item_keys_and_no_id(self):
        self.load(*tied_world())
        r = m.late_leaderboard(month="2026-07", limit=10, department=None)
        self.assertEqual(list(r), ["month", "items"])
        for item in r["items"]:
            self.assertEqual(list(item), ITEM_KEYS)
            self.assertNotIn("_id", item)
        self.assertEqual(r, oracle(*tied_world(), 2026, 7, 10))

    def test_uses_one_aggregation_from_the_shared_builder_and_never_find(self):
        self.load(*tied_world())
        m.late_leaderboard(month="2026-07", limit=2, department="Eng")
        self.assertEqual(self.fake.attendance_logs.calls, ["aggregate"])
        self.assertEqual(self.fake.employees.calls, [])
        self.assertEqual(self.fake.attendance_logs.last_pipeline, m.late_leaderboard_pipeline(2026, 7, 2, "Eng"))

    def test_invalid_month_is_a_validation_error_before_any_query(self):
        from fastapi.exceptions import RequestValidationError
        for bad in ("2026-13", "2026-00", "2026-7", "202607", "", "0000-05", "garbage"):
            with self.subTest(month=bad), self.assertRaises(RequestValidationError):
                m.late_leaderboard(month=bad, limit=10, department=None)
        self.assertEqual(self.fake.attendance_logs.calls, [])


@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestLeaderboardHttp(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)

    def get(self, qs):
        return self.c.get("/analytics/leaderboard/late" + (f"?{qs}" if qs is not None else ""))

    def test_200_envelope_and_item_schema(self):
        self.load(*tied_world())
        r = self.get("month=2026-07")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(list(body), ["month", "items"])
        self.assertEqual([list(i) for i in body["items"]], [ITEM_KEYS] * 4)
        self.assertEqual([i["rank"] for i in body["items"]], [1, 2, 2, 4])
        self.assertNotIn("_id", r.text)

    def test_422_missing_and_invalid_month(self):
        for qs in (None, "", "limit=5", "month=", "month=2026-13", "month=2026-1", "month=0000-05", "month=2026-00",
                   "month=202607", "month=2026-07-01", "month=abc", "month=%202026-07"):
            with self.subTest(qs=qs):
                r = self.get(qs)
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)
        self.assertEqual(self.fake.attendance_logs.calls, [])

    def test_limit_bounds(self):
        self.load(*tied_world())
        for ok in ("1", "50", "10"):
            with self.subTest(limit=ok):
                self.assertEqual(self.get(f"month=2026-07&limit={ok}").status_code, 200)
        for bad in ("0", "51", "-1", "abc", "", "1.5", "10x"):
            with self.subTest(limit=bad):
                self.assertEqual(self.get(f"month=2026-07&limit={bad}").status_code, 422)

    def test_default_limit_is_10(self):
        emps = [emp(f"E{i:02d}") for i in range(1, 14)]
        logs = [lg(f"E{i:02d}", "2026-07-01", i) for i in range(1, 14)]          # 13 distinct totals
        self.load(emps, logs)
        self.assertEqual(len(self.get("month=2026-07").json()["items"]), 10)
        self.assertEqual(len(self.get("month=2026-07&limit=50").json()["items"]), 13)

    def test_limit_semantics_over_http(self):
        self.load(*tied_world())
        self.assertEqual([i["rank"] for i in self.get("month=2026-07&limit=2").json()["items"]], [1, 2, 2])
        self.assertEqual([i["emp_code"] for i in self.get("month=2026-07&limit=1").json()["items"]], ["E1"])

    def test_empty_and_unknown_department_are_200_with_empty_items(self):
        self.load(*tied_world())
        self.assertEqual((self.get("month=2026-08").status_code, self.get("month=2026-08").json()), (200, {"month": "2026-08", "items": []}))
        r = self.get("month=2026-07&department=Unknown")
        self.assertEqual((r.status_code, r.json()), (200, {"month": "2026-07", "items": []}))
        r = self.get("month=2026-07&department=")
        self.assertEqual((r.status_code, r.json()), (200, {"month": "2026-07", "items": []}))   # exact "" match, not "omitted"

    def test_department_exact_case_sensitive_over_http(self):
        self.load([emp("E1", "Sales"), emp("E2", "sales")], [lg("E1", "2026-07-01", 5), lg("E2", "2026-07-01", 9)])
        self.assertEqual([i["emp_code"] for i in self.get("month=2026-07&department=Sales").json()["items"]], ["E1"])
        self.assertEqual(self.get("month=2026-07&department=SALES").json()["items"], [])

    def test_openapi_lists_the_three_parameters_and_only_200_and_422(self):
        op = self.c.get("/openapi.json").json()["paths"]["/analytics/leaderboard/late"]["get"]
        names = {p["name"]: p for p in op["parameters"]}
        self.assertTrue(names["month"]["required"])
        self.assertFalse(names["limit"]["required"])
        self.assertFalse(names["department"]["required"])
        self.assertEqual(names["limit"]["schema"]["default"], 10)
        self.assertEqual((names["limit"]["schema"]["minimum"], names["limit"]["schema"]["maximum"]), (1, 50))
        self.assertEqual(set(op["responses"]) - {"200", "422"}, set())


@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestExplainHttpFake(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)

    def test_explain_reuses_the_endpoints_pipeline_with_execution_stats(self):
        self.load(*tied_world())
        self.c.get("/analytics/leaderboard/late?month=2026-07&limit=3&department=Eng")
        endpoint_pipeline = self.fake.attendance_logs.last_pipeline
        r = self.c.get("/admin/explain/late_leaderboard?month=2026-07&limit=3&department=Eng")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(sorted(body), ["collection", "endpoint", "explain"])
        self.assertEqual((body["endpoint"], body["collection"]), ("late_leaderboard", "attendance_logs"))
        (cmd,) = self.fake.commands
        self.assertEqual(cmd["verbosity"], "executionStats")
        self.assertEqual(cmd["explain"]["aggregate"], "attendance_logs")
        self.assertEqual(cmd["explain"]["pipeline"], endpoint_pipeline)

    def test_month_is_required_and_validated_for_this_operation(self):
        for qs in ("", "month=", "month=0000-05", "month=2026-13", "limit=0&month=2026-07", "limit=51&month=2026-07"):
            with self.subTest(qs=qs):
                self.assertEqual(self.c.get("/admin/explain/late_leaderboard?" + qs).status_code, 422)
        self.assertEqual(self.fake.commands, [])

    def test_unknown_endpoint_is_422(self):
        self.assertEqual(self.c.get("/admin/explain/nope?month=2026-07").status_code, 422)


# ======================================================================== explain-plan checks (shared)
def plan_problems(explain_output) -> list:
    """Problems that fail the grader's explain condition; an empty list means the plan passes. Uses the Phase 5.2
    tests/_explain_util.summarize (unchanged). Checks: IXSCAN present, COLLSCAN absent (anywhere in the output, as the
    grader does), and every exposed $lookup `collectionScans` counter == 0. Also reports (not fails) whether any
    collectionScans counter was exposed at all, via plan_exposes_collection_scans."""
    s = summarize(explain_output)
    problems = []
    if not s["has_IXSCAN"]:
        problems.append("no IXSCAN in the plan")
    if s["has_COLLSCAN"]:
        problems.append("COLLSCAN present in the plan")
    for counters in s["lookup_counters"]:
        if "collectionScans" in counters and counters["collectionScans"] != 0:
            problems.append(f"$lookup collectionScans={counters['collectionScans']}")
    return problems


def plan_exposes_collection_scans(explain_output) -> bool:
    return any("collectionScans" in c for c in summarize(explain_output)["lookup_counters"])


class TestPlanChecks(unittest.TestCase):
    """The checker itself, on hand-written explain documents shaped like MongoDB 7.0 output (NOT real explain output)."""
    GOOD = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "FETCH", "inputStage": {
        "stage": "IXSCAN", "indexName": "date_-1_emp_code_1"}}}}},
        {"$lookup": {"from": "employees"}, "totalDocsExamined": 5, "totalKeysExamined": 5, "collectionScans": 0,
         "indexesUsed": ["emp_code_1"]}]}

    def test_good_plan_passes_and_exposes_the_counter(self):
        self.assertEqual(plan_problems(self.GOOD), [])
        self.assertTrue(plan_exposes_collection_scans(self.GOOD))

    def test_missing_ixscan_fails(self):
        self.assertIn("no IXSCAN in the plan", plan_problems({"stages": [{"$cursor": {"x": {"stage": "FETCH"}}}]}))

    def test_collscan_anywhere_fails(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["stages"][0]["$cursor"]["queryPlanner"]["rejectedPlans"] = [{"stage": "COLLSCAN"}]
        self.assertIn("COLLSCAN present in the plan", plan_problems(bad))

    def test_lookup_collection_scan_counter_must_be_zero(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["stages"][1]["collectionScans"] = 3
        self.assertIn("$lookup collectionScans=3", plan_problems(bad))

    def test_counter_not_exposed_is_reported_not_silently_assumed(self):
        no_counter = json.loads(json.dumps(self.GOOD))
        del no_counter["stages"][1]["collectionScans"]
        self.assertEqual(plan_problems(no_counter), [])
        self.assertFalse(plan_exposes_collection_scans(no_counter))


# ======================================================================== 4. REAL MongoDB (opt-in)
def seed_big(rng, n_emps=5000, per=20, months=(6, 7, 8, 9)):
    """~n_emps*per attendance records spread over four months (July is ~25 %), so the month range is selective."""
    depts = ["Engineering", "Sales", "Support", "engineering", "HR", "Ops", "Legal", "Data"]
    emps, logs = [], []
    for i in range(n_emps):
        dept = rng.choice(depts)
        if i % 41 == 0:
            dept = rng.choice([None, 5, ""])
        e = emp(f"E{i:05d}", dept)
        emps.append(e)
        for k in range(per):
            month = months[k % len(months)]
            day = 1 + (k * 7 + i) % calendar.monthrange(2026, month)[1]
            logs.append(lg(e["emp_code"], f"2026-{month:02d}-{day:02d}", rng.choice([0, 0, 1, 10, 11, 45, -5, None]),
                           status=rng.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"])))
    return emps, logs


@unittest.skipUnless(FULL and HAVE_HTTP and TEST_URI,
                     "set TEST_MONGO_URI to run the real-MongoDB integration tests: NOT EXECUTED")
class TestMongoLeaderboard(unittest.TestCase):
    """REAL MongoDB in a throwaway database (attendance_test_p53_<random>), dropped at the end."""
    PREFIX = "attendance_test_p53_"

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
        cls.server_version = cls.client.server_info().get("version")

    @classmethod
    def tearDownClass(cls):
        m.db, m._indexes_ready = cls._orig_db, cls._orig_ready
        if cls.dbname.startswith(cls.PREFIX):
            cls.client.drop_database(cls.dbname)
        cls.client.close()

    def setUp(self):
        for c in ("employees", "attendance_logs"):
            self.tdb[c].delete_many({})

    def seed(self, emps, logs):
        for coll, docs in (("employees", emps), ("attendance_logs", logs)):
            for i in range(0, len(docs), 5000):
                self.tdb[coll].insert_many([dict(d) for d in docs[i:i + 5000]])

    def board(self, month="2026-07", limit=10, department=None):
        return m.late_leaderboard(month=month, limit=limit, department=department)

    def test_server_version_is_reported(self):
        print(f"\n[real MongoDB server version: {self.server_version}]")
        self.assertTrue(self.server_version)

    def test_existing_index_definitions_are_unchanged(self):
        e = {tuple(v["key"]) for k, v in self.tdb.employees.index_information().items() if k != "_id_"}
        a = {tuple(v["key"]) for k, v in self.tdb.attendance_logs.index_information().items() if k != "_id_"}
        self.assertEqual(e, {(("emp_code", 1),), (("department", 1), ("emp_code", 1))})
        self.assertEqual(len(a), 4)

    def test_real_rank_ties_limit_and_order(self):
        emps, logs = tied_world()
        self.seed(emps, logs)
        self.assertEqual([(i["emp_code"], i["rank"]) for i in self.board()["items"]],
                         [("E1", 1), ("E2", 2), ("E3", 2), ("E4", 4)])
        self.assertEqual([(i["emp_code"], i["rank"]) for i in self.board(limit=2)["items"]], [("E1", 1), ("E2", 2), ("E3", 2)])
        self.assertEqual([i["emp_code"] for i in self.board(limit=1)["items"]], ["E1"])

    def test_real_semantics_edge_cases_match_the_oracle(self):
        no_dept = emp("E7"); del no_dept["department"]
        missing = lg("E1", "2026-07-02"); del missing["late_minutes"]
        emps = [emp("E1", "Sales"), emp("E2", "sales"), emp("E3", ""), emp("E4", 5), emp("E5", None), no_dept, emp("E6", "Eng")]
        logs = [missing, lg("E1", "2026-07-01", 10), lg("E1", "2026-07-03", -9), lg("E2", "2026-07-04", 20),
                lg("E3", "2026-07-05", 30, status="LEAVE"), lg("E4", "2026-07-05", 40), lg("E5", "2026-07-05", 50),
                lg("E7", "2026-07-05", 60), lg("E6", "2026-06-30", 70), lg("E6", "2026-07-31", 5, status="ABSENT"),
                lg("GHOST", "2026-07-06", 999), lg("E6", "2026-08-01", 80)]
        self.seed(emps, logs)
        for dept in (None, "Sales", "sales", "SALES", "", "Eng", "Unknown"):
            for limit in (1, 2, 50):
                with self.subTest(dept=dept, limit=limit):
                    self.assertEqual(self.board(limit=limit, department=dept), oracle(emps, logs, 2026, 7, limit, dept))
        self.assertEqual(self.board(department="")["items"][0]["emp_code"], "E3")

    def test_real_department_types_array_number_null_missing_string_and_empty(self):
        """REAL $expr/$type behaviour (the emulator only models it)."""
        no_dept = emp("E_MISSING"); del no_dept["department"]
        emps = [emp("E_ARRAY", ["Engineering"]), emp("E_ARRAY2", ["Sales", "Engineering"]), emp("E_NUM", 5),
                emp("E_NULL", None), no_dept, emp("E_STR", "Engineering"), emp("E_EMPTY", "")]
        logs = [lg(e["emp_code"], "2026-07-01", 100 - i) for i, e in enumerate(emps)]
        self.seed(emps, logs)
        self.assertEqual([i["emp_code"] for i in self.board()["items"]], ["E_STR", "E_EMPTY"])
        self.assertEqual([(i["emp_code"], i["rank"]) for i in self.board(department="Engineering")["items"]], [("E_STR", 1)])
        self.assertEqual([i["emp_code"] for i in self.board(department="")["items"]], ["E_EMPTY"])
        for dept in (None, "Engineering", ""):
            self.assertEqual(self.board(department=dept), oracle(emps, logs, 2026, 7, 10, dept))

    def test_real_leap_february_and_boundaries(self):
        logs = [lg("E1", d, 1) for d in ("2028-01-31", "2028-02-01", "2028-02-29", "2028-03-01")]
        self.seed([emp("E1")], logs)
        self.assertEqual(self.board("2028-02")["items"][0]["late_count"], 2)

    def test_real_random_world_matches_the_oracle(self):
        emps, logs = random_world(random.Random(3), 400, 10)
        self.seed(emps, logs)
        for limit in (1, 3, 10, 50):
            for dept in (None, "Eng", "eng", "", "Sales"):
                with self.subTest(limit=limit, dept=dept):
                    self.assertEqual(self.board(limit=limit, department=dept), oracle(emps, logs, 2026, 7, limit, dept))

    # ---------------- the release-blocker checks: real EXPLAIN on ~100,000 attendance records ----------------
    def seed_large(self):
        emps, logs = seed_big(random.Random(7))
        self.seed(emps, logs)
        self.assertEqual(self.tdb.attendance_logs.count_documents({}), 100_000)
        return emps, logs

    def explain(self, department):
        pipeline = m.late_leaderboard_pipeline(2026, 7, 10, department)
        return self.tdb.command({"explain": {"aggregate": m.LATE_LEADERBOARD_COLLECTION, "pipeline": pipeline, "cursor": {}},
                                 "verbosity": "executionStats"})

    def check_plan(self, out, label):
        print(f"\n[explain, {label}] server={self.server_version} {describe(out)} "
              f"collectionScans_exposed={plan_exposes_collection_scans(out)}")
        self.assertEqual(plan_problems(out), [], f"{label} plan: {describe(out)}")

    def test_explain_unfiltered_uses_an_index_and_no_collection_scan(self):
        self.seed_large()
        self.check_plan(self.explain(None), "unfiltered")

    def test_explain_with_department_filter_uses_an_index_and_no_collection_scan(self):
        self.seed_large()
        self.check_plan(self.explain("Engineering"), "department=Engineering")

    def test_large_dataset_results_match_the_oracle(self):
        emps, logs = self.seed_large()
        for dept in (None, "Engineering", "engineering"):
            with self.subTest(department=dept):
                self.assertEqual(self.board(limit=50, department=dept), oracle(emps, logs, 2026, 7, 50, dept))

    def test_http_explain_route_with_real_mongo(self):
        from fastapi.testclient import TestClient
        self.seed(*tied_world())
        c = TestClient(m.app)
        r = c.get("/admin/explain/late_leaderboard?month=2026-07&limit=2")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["endpoint"], body["collection"]), ("late_leaderboard", "attendance_logs"))
        self.assertIn("executionStats", json.dumps(body["explain"]))
        self.assertEqual(c.get("/admin/explain/late_leaderboard").status_code, 422)
        r = c.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        self.assertEqual([i["rank"] for i in r.json()["items"]], [1, 2, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
