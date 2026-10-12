"""Phase 5.2 tests: GET /analytics/departments/summary?month=YYYY-MM[&department=...]

Layers (each says what it proves):
  1. TestHundredths / TestHalfUpMean / TestPipelineShape / TestBuildResponse   pure; run anywhere.
  2. TestPipelineSemanticsEmulated   the REAL pipeline executed by tests/_agg_emulator_p52.py, an EMULATION of the
                                     MongoDB operators it uses (not MongoDB); compared with an independent oracle.
  3. TestDepartmentEndpointFake / TestDepartmentHttp   the endpoint on an in-memory fake whose aggregate() uses the
                                     emulator. MOCKED: proves routing, 422 mapping, "one aggregation, no find".
  4. TestMongoDepartmentSummary      REAL MongoDB, opt-in via TEST_MONGO_URI; disposable database
                                     attendance_test_p52_<random>, dropped afterwards. Never touches MONGO_DB.
                                     Includes the strict EXPLAIN checks (IXSCAN present, COLLSCAN absent).
"""
import calendar
import json
import os
import random
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from fractions import Fraction

from tests._agg_emulator_p52 import ev, mini_aggregate
from tests._explain_util import describe, summarize
from tests._loader import ROOT, have, load_main

m, MODE = load_main()
FULL = MODE == "full"
HAVE_HTTP = FULL and have("httpx")
TEST_URI = os.environ.get("TEST_MONGO_URI")

ITEM_KEYS = ["department", "headcount", "present_days", "avg_work_hours", "late_count", "total_late_minutes",
             "leave_count", "on_duty_count"]
ANY = object()


def emp(code, dept, joined="2026-01-05", **extra):
    d = {"emp_code": code, "department": dept, "joined_on": joined, "name": "N", "email": "n@x.io",
         "shift_start": "09:30", "shift_end": "18:30"}
    d.update(extra)
    return d


def lg(code, date, status="PRESENT", late=0, hours=None, half=False, **extra):
    d = {"emp_code": code, "date": date, "status": status, "late_minutes": late, "half_day": half,
         "work_hours": hours, "overtime_minutes": 0, "history": []}
    d.update(extra)
    return d


def without(doc, *keys):
    return {k: v for k, v in doc.items() if k not in keys}


def run(emps, logs, year=2026, month=7, department=None):
    """The real pipeline on the emulator -> the real response builder."""
    rows = mini_aggregate(emps, m.department_summary_pipeline(year, month, department), {"attendance_logs": logs})
    return m.build_department_summary(f"{year:04d}-{month:02d}", rows)


def items_by_dept(result):
    return {i["department"]: i for i in result["items"]}


# ------------------------------ independent oracle (different algorithm: Fractions / Decimals / calendar) ------------------------------
def oracle(emps, logs, year, month, department=ANY):
    last = f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
    first = f"{year:04d}-{month:02d}-01"
    eligible = [e for e in emps if isinstance(e.get("department"), str) and e["department"] != ""
                and isinstance(e.get("joined_on"), str) and e["joined_on"] <= last]
    if department is not ANY and department is not None:
        eligible = [e for e in eligible if e["department"] == department]
    out = []
    for dept in sorted({e["department"] for e in eligible}):
        codes = {e["emp_code"] for e in eligible if e["department"] == dept}
        recs = [l for l in logs if l["emp_code"] in codes and first <= l["date"] <= last]
        present = Fraction(0)
        hours = []
        for l in recs:
            is_pres = l["status"] in ("PRESENT", "WFH", "ON_DUTY")
            weekday = calendar.weekday(year, month, int(l["date"][8:10])) <= 4
            if is_pres and weekday:
                present += Fraction(1, 2) if l.get("half_day") is True else 1
            w = l.get("work_hours")
            if is_pres and isinstance(w, (int, float)) and not isinstance(w, bool):
                hours.append(Decimal(str(w)))
        avg = None
        if hours:
            avg = float((sum(hours) / len(hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        late = [l["late_minutes"] for l in recs if isinstance(l.get("late_minutes"), (int, float)) and l["late_minutes"] > 0]
        out.append({"department": dept, "headcount": len(codes), "present_days": float(present), "avg_work_hours": avg,
                    "late_count": len(late), "total_late_minutes": sum(late),
                    "leave_count": sum(1 for l in recs if l["status"] == "LEAVE"),
                    "on_duty_count": sum(1 for l in recs if l["status"] == "ON_DUTY")})
    return {"month": f"{year:04d}-{month:02d}", "items": out}


# ======================================================================== 1. pure
class TestHundredths(unittest.TestCase):
    """work_hours -> integer hundredths, evaluated with the emulator (Python floats == BSON doubles)."""

    def conv(self, v):
        return ev(m.work_hours_hundredths("$w"), {"w": v})

    def test_every_two_decimal_value_from_0_to_100_converts_exactly(self):
        for k in range(0, 10001):
            v = k / 100                                   # the double a 2-decimal value is stored as
            self.assertEqual(self.conv(v), k, f"{v!r} -> {self.conv(v)} (expected {k})")

    def test_the_cases_the_contract_calls_out(self):
        self.assertEqual(self.conv(8.01), 801)
        self.assertEqual(self.conv(0.0), 0)
        self.assertEqual(self.conv(8.0), 800)
        self.assertEqual(self.conv(8), 800)               # an integer-valued work_hours
        self.assertEqual(self.conv(24.0), 2400)

    def test_naive_float_multiplication_is_not_exact_which_is_why_the_floor_is_used(self):
        self.assertEqual(8.01 * 100, 801.0)                # 8.01 happens to be fine ...
        for v, k in ((0.29, 29), (1.15, 115)):             # ... but these 2-decimal values are not
            self.assertNotEqual(v * 100, k)                # 28.999999999999996 / 114.99999999999999
            self.assertEqual(int(v * 100), k - 1)          # truncating would be wrong
            self.assertEqual(self.conv(v), k)              # the floor(x*100 + 0.5) form is right

    def test_three_decimal_values_are_outside_the_contract_and_not_guaranteed(self):
        # R4 stores 2 decimals, so 8.005 / 8.015 cannot occur in valid data. The behaviour for them is pinned
        # here so a change would be noticed - it is a LIMITATION, not a guarantee:
        self.assertEqual(self.conv(8.005), 801)           # the double is 8.00500000000000078 (above the tie)
        self.assertEqual(self.conv(8.015), 802)           # the double is 8.01500000000000057 (above the tie)
        self.assertEqual(self.conv(0.0), 0)
        # 2.675 is stored as 2.67499999999999982 (BELOW the tie) yet x*100 itself rounds to exactly 267.5:
        self.assertEqual(self.conv(2.675), 268)           # exact-double half-up would give 267

    def test_result_type_is_an_integer(self):
        self.assertIs(type(self.conv(8.01)), int)

    def test_expression_uses_no_round(self):
        self.assertNotIn("$round", json.dumps(m.work_hours_hundredths("$w")))


class TestHalfUpMean(unittest.TestCase):
    def test_the_contract_case_8_00_and_8_01(self):
        self.assertEqual(m.half_up_mean_hundredths(800 + 801, 2), 8.01)
        self.assertEqual(round((8.00 + 8.01) / 2, 2), 8.0)             # what a float sum + round() would give
        self.assertNotEqual(m.half_up_mean_hundredths(1601, 2), round((8.00 + 8.01) / 2, 2))

    def test_ties_and_neighbours(self):
        f = m.half_up_mean_hundredths
        self.assertEqual(f(800 + 803, 2), 8.02)           # 8.015 -> 8.02
        self.assertEqual(f(799 + 800, 2), 8.0)            # 7.995 -> 8.00
        self.assertEqual(f(0 + 1, 2), 0.01)               # 0.005 -> 0.01
        self.assertEqual(f(800 + 800, 2), 8.0)
        self.assertEqual(f(800 + 800 + 801, 3), 8.0)      # 8.00333
        self.assertEqual(f(801 + 802 + 802, 3), 8.02)     # 8.01667
        self.assertEqual(f(0, 1), 0.0)                    # a single 0.0 is a real value
        self.assertEqual(f(0, 4), 0.0)

    def test_no_values_is_none(self):
        self.assertIsNone(m.half_up_mean_hundredths(0, 0))
        self.assertIsNone(m.half_up_mean_hundredths(5, 0))

    def test_result_type_and_value_on_whole_numbers(self):
        v = m.half_up_mean_hundredths(900, 1)
        self.assertEqual(v, 9.0)
        self.assertIsInstance(v, float)

    def test_exhaustive_against_an_exact_oracle(self):
        rng = random.Random(52)
        for _ in range(4000):
            n = rng.randint(1, 12)
            hundredths = [rng.randint(0, 2400) for _ in range(n)]
            exact = Fraction(sum(hundredths), n)
            expected = float(Decimal((exact + Fraction(1, 2)).__floor__()) / 100)
            self.assertEqual(m.half_up_mean_hundredths(sum(hundredths), n), expected, hundredths)
        for n in range(1, 40):                              # every residue, including exact ties
            for total in range(0, 40 * n):
                exact = Fraction(total, n)
                expected = float(Decimal((exact + Fraction(1, 2)).__floor__()) / 100)
                self.assertEqual(m.half_up_mean_hundredths(total, n), expected, (total, n))

    def test_no_builtin_round_in_the_helpers(self):
        import ast
        from tests._loader import MAIN
        tree = ast.parse(MAIN.read_text())
        for name in ("half_up_mean_hundredths", "work_hours_hundredths", "build_department_summary"):
            fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = [c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
            self.assertNotIn("round", calls, name)


class TestBuildResponse(unittest.TestCase):
    def test_envelope_item_order_and_types(self):
        row = {"department": "Eng", "headcount": 2, "present_halves": 6, "work_n": 3, "work_hundredths": 2713,
               "late_count": 1, "total_late_minutes": 35, "leave_count": 1, "on_duty_count": 0}
        r = m.build_department_summary("2026-07", [row])
        self.assertEqual(list(r), ["month", "items"])
        self.assertEqual(r["month"], "2026-07")
        item = r["items"][0]
        self.assertEqual(list(item), ITEM_KEYS)
        self.assertEqual((item["present_days"], item["avg_work_hours"]), (3.0, 9.04))
        for k in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            self.assertIs(type(item[k]), int, k)
        self.assertIsInstance(item["present_days"], float)
        self.assertNotIn("_id", item)

    def test_empty_rows_is_an_empty_items_array(self):
        self.assertEqual(m.build_department_summary("2026-07", []), {"month": "2026-07", "items": []})

    def test_no_eligible_hours_gives_null_average(self):
        r = m.build_department_summary("2026-07", [{"department": "D", "headcount": 1, "present_halves": 0, "work_n": 0,
                                                    "work_hundredths": 0}])
        self.assertIsNone(r["items"][0]["avg_work_hours"])
        self.assertEqual(r["items"][0]["late_count"], 0)

    def test_float_valued_sums_become_ints_and_order_is_preserved(self):
        rows = [{"department": "b", "headcount": 1.0, "present_halves": 2.0, "work_n": 1.0, "work_hundredths": 800.0,
                 "late_count": 1.0, "total_late_minutes": 12.0, "leave_count": 0.0, "on_duty_count": 0.0},
                {"department": "a", "headcount": 1, "present_halves": 0}]
        r = m.build_department_summary("2026-07", rows)
        self.assertEqual([i["department"] for i in r["items"]], ["b", "a"])      # the database's order is kept
        self.assertIs(type(r["items"][0]["total_late_minutes"]), int)


class TestPipelineShape(unittest.TestCase):
    def test_stage_order(self):
        p = m.department_summary_pipeline(2026, 7)
        self.assertEqual([next(iter(s)) for s in p], ["$match", "$lookup", "$group", "$sort", "$project"])

    def test_match_stage_headcount_rules_and_optional_filter(self):
        self.assertEqual(m.department_summary_pipeline(2026, 7)[0]["$match"],
                         {"joined_on": {"$lte": "2026-07-31"}, "department": {"$type": "string", "$ne": ""}})
        self.assertEqual(m.department_summary_pipeline(2026, 7, "Sales")[0]["$match"]["department"]["$eq"], "Sales")
        self.assertEqual(m.department_summary_pipeline(2026, 7, "sales")[0]["$match"]["department"]["$eq"], "sales")
        self.assertEqual(m.department_summary_pipeline(2026, 7, "")[0]["$match"]["department"]["$eq"], "")   # explicit "" is kept
        self.assertNotIn("$eq", m.department_summary_pipeline(2026, 7, None)[0]["$match"]["department"])

    def test_month_bounds_including_leap_february(self):
        self.assertEqual(m.department_summary_pipeline(2028, 2)[0]["$match"]["joined_on"]["$lte"], "2028-02-29")
        lk = m.department_summary_pipeline(2028, 2)[1]["$lookup"]
        self.assertEqual(lk["pipeline"][0]["$match"]["date"], {"$gte": "2028-02-01", "$lte": "2028-02-29"})

    def test_lookup_uses_the_indexed_join_and_projects_only_what_is_needed(self):
        lk = m.department_summary_pipeline(2026, 7)[1]["$lookup"]
        self.assertEqual((lk["from"], lk["localField"], lk["foreignField"], lk["as"]),
                         ("attendance_logs", "emp_code", "emp_code", "logs"))
        self.assertEqual(lk["pipeline"][1]["$project"],
                         {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1})
        self.assertIn([("emp_code", 1), ("date", 1)], [k for k, o in m.INDEXES["attendance_logs"] if o.get("unique")])

    def test_group_is_by_department_with_no_unwind(self):
        p = m.department_summary_pipeline(2026, 7)
        self.assertEqual(p[2]["$group"]["_id"], "$department")
        self.assertEqual(p[2]["$group"]["headcount"], {"$sum": 1})
        self.assertEqual(p[3], {"$sort": {"_id": 1}})
        self.assertEqual(p[4]["$project"]["_id"], 0)
        self.assertEqual(p[4]["$project"]["department"], "$_id")

    def test_no_forbidden_operators(self):
        text = json.dumps(m.department_summary_pipeline(2026, 7, "Sales"))
        for forbidden in ("$round", "$unwind", "$avg", "$function", "$where", "$facet"):
            self.assertNotIn(forbidden, text)

    def test_legacy_defaults_are_explicit(self):
        text = json.dumps(m.department_summary_pipeline(2026, 7))
        self.assertIn('{"$ifNull": ["$$l.late_minutes", 0]}', text)
        self.assertIn('{"$ifNull": ["$$l.half_day", false]}', text)
        self.assertIn('{"$isNumber": "$$l.work_hours"}', text)

    def test_deterministic_and_parameter_specific(self):
        a = m.department_summary_pipeline(2026, 7, "Sales")
        self.assertEqual(a, m.department_summary_pipeline(2026, 7, "Sales"))
        self.assertNotEqual(a, m.department_summary_pipeline(2026, 8, "Sales"))
        self.assertNotEqual(a, m.department_summary_pipeline(2026, 7, "Support"))
        self.assertNotEqual(a, m.department_summary_pipeline(2026, 7, None))
        self.assertIn("2026-07-04", json.dumps(a))                        # weekend list embedded

    def test_runs_on_the_employees_collection(self):
        self.assertEqual(m.DEPARTMENT_SUMMARY_COLLECTION, "employees")
        self.assertIn([("emp_code", 1)], [k for k, o in m.INDEXES["employees"] if o.get("unique")])

    def test_index_definitions_are_unchanged(self):
        self.assertEqual(len(m.INDEXES["employees"]), 2)
        self.assertEqual(len(m.INDEXES["attendance_logs"]), 4)


# ======================================================================== 2. emulated pipeline semantics
SAMPLE_EXPECTED = {
    "Engineering": dict(headcount=2, present_days=3.0, avg_work_hours=9.04, late_count=0, total_late_minutes=0, leave_count=0, on_duty_count=0),
    "Sales": dict(headcount=2, present_days=1.5, avg_work_hours=6.29, late_count=1, total_late_minutes=35, leave_count=1, on_duty_count=0),
    "Support": dict(headcount=2, present_days=2.0, avg_work_hours=8.75, late_count=0, total_late_minutes=0, leave_count=0, on_duty_count=0),
}


class TestPipelineSemanticsEmulated(unittest.TestCase):
    """The real pipeline run by an EMULATION of MongoDB's operators (tests/_agg_emulator_p52.py)."""

    def test_sample_data_hand_computed(self):
        emps = json.loads((ROOT / "sample_data" / "employees.json").read_text())
        logs = json.loads((ROOT / "sample_data" / "attendance_logs.json").read_text())
        r = run(emps, logs)
        self.assertEqual([i["department"] for i in r["items"]], ["Engineering", "Sales", "Support"])
        for item in r["items"]:
            self.assertEqual({k: v for k, v in item.items() if k != "department"}, SAMPLE_EXPECTED[item["department"]])
        self.assertEqual(r, oracle(emps, logs, 2026, 7))

    # ---- headcount
    def headcount_world(self):
        emps = [emp("A1", "Alpha", "2026-01-01"), emp("A2", "Alpha", "2026-07-31"), emp("A3", "Alpha", "2026-08-01"),
                emp("B1", "Beta", "2026-08-15"), emp("C1", "Gamma", "2026-03-01")]
        logs = [lg("A1", d) for d in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07")]
        logs += [lg("A3", "2026-07-06", late=99, hours=9.0), lg("GHOST", "2026-07-06", late=77, hours=7.0),
                 lg("B1", "2026-07-06", late=55, hours=5.0)]
        return emps, logs

    def test_headcount_rules(self):
        emps, logs = self.headcount_world()
        r = items_by_dept(run(emps, logs))
        self.assertEqual(set(r), {"Alpha", "Gamma"})                     # Beta has only a late joiner: omitted
        self.assertEqual(r["Alpha"]["headcount"], 2)                      # A1 + A2 (joined on the last day); A3 excluded
        self.assertEqual(r["Gamma"]["headcount"], 1)                      # zero logs still counts
        self.assertEqual((r["Gamma"]["present_days"], r["Gamma"]["avg_work_hours"], r["Gamma"]["late_count"]), (0.0, None, 0))
        self.assertEqual((r["Alpha"]["present_days"], r["Alpha"]["late_count"], r["Alpha"]["total_late_minutes"]), (5.0, 0, 0))

    def test_headcount_is_not_multiplied_by_the_number_of_logs(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", f"2026-07-{d:02d}") for d in range(1, 21)]
        self.assertEqual(run(emps, logs)["items"][0]["headcount"], 1)

    def test_ineligible_and_orphan_logs_are_excluded_from_every_metric(self):
        emps, logs = self.headcount_world()
        a = items_by_dept(run(emps, logs))["Alpha"]
        self.assertEqual((a["total_late_minutes"], a["avg_work_hours"]), (0, None))   # no 99 / 77 / 55 leaked in

    def test_joined_on_boundary_days(self):
        emps = [emp("L", "D", "2026-07-31"), emp("N", "D", "2026-08-01"), emp("F", "D", "2026-07-01")]
        self.assertEqual(run(emps, [])["items"][0]["headcount"], 2)
        self.assertEqual(run([emp("N", "D", "2026-08-01")], [])["items"], [])

    def test_missing_null_empty_and_non_string_departments_never_make_an_item(self):
        emps = [emp("E1", None), without(emp("E2", "x"), "department"), emp("E3", ""), emp("E4", 123), emp("E5", ["a"]),
                emp("E6", "Real")]
        logs = [lg(c, "2026-07-01") for c in ("E1", "E2", "E3", "E4", "E5", "E6")]
        r = run(emps, logs)
        self.assertEqual([i["department"] for i in r["items"]], ["Real"])
        self.assertEqual(r["items"][0]["headcount"], 1)

    def test_departments_are_case_sensitive_and_sorted_by_code_point(self):
        emps = [emp("1", "engineering"), emp("2", "Sales"), emp("3", "Engineering"), emp("4", "HR"), emp("5", "Zed")]
        r = run(emps, [])
        self.assertEqual([i["department"] for i in r["items"]], ["Engineering", "HR", "Sales", "Zed", "engineering"])

    # ---- department filter
    def test_department_filter_exact_case_sensitive_and_empty_string(self):
        emps = [emp("1", "Sales"), emp("2", "sales"), emp("3", "Sales ")]
        self.assertEqual([i["department"] for i in run(emps, [], department="Sales")["items"]], ["Sales"])
        self.assertEqual([i["department"] for i in run(emps, [], department="sales")["items"]], ["sales"])
        self.assertEqual(run(emps, [], department="SALES")["items"], [])
        self.assertEqual(run(emps, [], department="Sal")["items"], [])
        self.assertEqual(run(emps, [], department="Nope")["items"], [])
        self.assertEqual(run([emp("1", "")], [], department="")["items"], [])        # explicit "": exact match, but empty depts are excluded
        self.assertEqual(len(run(emps, [], department=None)["items"]), 3)            # omitted: no filter

    # ---- present days
    def test_present_days_rules(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-07-01", "PRESENT"), lg("E1", "2026-07-02", "WFH"), lg("E1", "2026-07-03", "ON_DUTY"),
                lg("E1", "2026-07-06", "ABSENT"), lg("E1", "2026-07-07", "LEAVE"), lg("E1", "2026-07-08", "HOLIDAY"),
                lg("E1", "2026-07-09", "PRESENT", half=True), lg("E1", "2026-07-10", "PRESENT", half=None),
                without(lg("E1", "2026-07-13", "PRESENT"), "half_day"), lg("E1", "2026-07-04", "PRESENT"),
                lg("E1", "2026-07-05", "ON_DUTY"), lg("E1", "2026-07-14", None)]
        item = run(emps, logs)["items"][0]
        self.assertEqual(item["present_days"], 5.5)                        # 2+2+2+1+2+2 halves = 11 -> 5.5
        self.assertEqual((item["leave_count"], item["on_duty_count"]), (1, 2))   # Sunday ON_DUTY counts as on-duty

    def test_half_day_on_every_presence_status_and_weekend_half_day(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-07-01", "PRESENT", half=True), lg("E1", "2026-07-02", "WFH", half=True),
                lg("E1", "2026-07-03", "ON_DUTY", half=True), lg("E1", "2026-07-04", "PRESENT", half=True)]
        self.assertEqual(run(emps, logs)["items"][0]["present_days"], 1.5)

    def test_half_day_flag_on_a_non_presence_status_adds_nothing(self):
        self.assertEqual(run([emp("E1", "D")], [lg("E1", "2026-07-01", "LEAVE", half=True)])["items"][0]["present_days"], 0.0)

    def test_non_boolean_half_day_is_not_a_half_day(self):
        item = run([emp("E1", "D")], [lg("E1", "2026-07-01", half=1), lg("E1", "2026-07-02", half="true")])["items"][0]
        self.assertEqual(item["present_days"], 2.0)

    def test_stored_values_are_used_not_recomputed_from_punch_times(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-07-01", late=0, hours=3.0, punch_in=datetime(2026, 7, 1, 4, 11, 1, tzinfo=timezone.utc),
                   punch_out=datetime(2026, 7, 1, 13, 40, tzinfo=timezone.utc))]
        item = run(emps, logs)["items"][0]
        self.assertEqual((item["late_count"], item["total_late_minutes"], item["avg_work_hours"]), (0, 0, 3.0))

    # ---- average work hours
    def test_mean_is_across_records_not_across_employee_means(self):
        emps = [emp("E1", "D"), emp("E2", "D")]
        logs = [lg("E1", "2026-07-01", hours=8.0), lg("E1", "2026-07-02", hours=8.0), lg("E1", "2026-07-03", hours=8.0),
                lg("E2", "2026-07-01", hours=12.0)]
        self.assertEqual(run(emps, logs)["items"][0]["avg_work_hours"], 9.0)       # (24+12)/4, not (8+12)/2 = 10

    def test_zero_hours_are_values(self):
        item = run([emp("E1", "D")], [lg("E1", "2026-07-01", hours=0.0), lg("E1", "2026-07-02", hours=8.0)])["items"][0]
        self.assertEqual(item["avg_work_hours"], 4.0)
        self.assertEqual(run([emp("E1", "D")], [lg("E1", "2026-07-01", hours=0.0)])["items"][0]["avg_work_hours"], 0.0)

    def test_null_missing_and_non_numeric_hours_are_excluded(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-07-01", hours=8.0), lg("E1", "2026-07-02", hours=None),
                without(lg("E1", "2026-07-03"), "work_hours"), lg("E1", "2026-07-06", hours="9.0"),
                lg("E1", "2026-07-07", hours=True)]
        self.assertEqual(run(emps, logs)["items"][0]["avg_work_hours"], 8.0)

    def test_absent_leave_and_unknown_status_hours_never_count(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-07-01", "ABSENT", hours=99.0), lg("E1", "2026-07-02", "LEAVE", hours=99.0),
                lg("E1", "2026-07-03", "HOLIDAY", hours=99.0), lg("E1", "2026-07-06", "PRESENT", hours=6.0)]
        self.assertEqual(run(emps, logs)["items"][0]["avg_work_hours"], 6.0)

    def test_all_open_records_give_null(self):
        emps = [emp("E1", "D")]
        item = run(emps, [lg("E1", "2026-07-01", hours=None), lg("E1", "2026-07-02", "LEAVE", hours=None)])["items"][0]
        self.assertIsNone(item["avg_work_hours"])

    def test_weekend_presence_hours_count_toward_the_average(self):          # contract: no weekday limit on the mean
        item = run([emp("E1", "D")], [lg("E1", "2026-07-04", hours=10.0), lg("E1", "2026-07-06", hours=8.0)])["items"][0]
        self.assertEqual((item["avg_work_hours"], item["present_days"]), (9.0, 1.0))

    def test_half_up_edge_cases_end_to_end(self):
        def avg(*hours):
            emps = [emp(f"E{i}", "D") for i in range(len(hours))]
            logs = [lg(f"E{i}", "2026-07-01", hours=h) for i, h in enumerate(hours)]
            return run(emps, logs)["items"][0]["avg_work_hours"]
        self.assertEqual(avg(8.00, 8.01), 8.01)           # THE contract case
        self.assertEqual(avg(8.00, 8.03), 8.02)
        self.assertEqual(avg(7.99, 8.00), 8.0)
        self.assertEqual(avg(0.00, 0.01), 0.01)
        self.assertEqual(avg(8.01, 8.01), 8.01)
        self.assertEqual(avg(8.00, 8.00, 8.01), 8.0)
        self.assertEqual(avg(8.01, 8.02, 8.02), 8.02)

    # ---- late / leave / on-duty
    def test_late_boundaries_negative_and_defaults(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-07-01", late=0), lg("E1", "2026-07-02", late=1), lg("E1", "2026-07-03", late=10),
                lg("E1", "2026-07-06", late=-5), lg("E1", "2026-07-07", late=None), without(lg("E1", "2026-07-08"), "late_minutes"),
                lg("E1", "2026-07-04", late=20)]
        item = run(emps, logs)["items"][0]
        self.assertEqual((item["late_count"], item["total_late_minutes"]), (3, 31))      # 1 + 10 + 20; -5 is neither counted nor summed

    def test_late_counts_every_status_and_weekend(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-07-01", "ABSENT", late=3), lg("E1", "2026-07-02", "LEAVE", late=5),
                lg("E1", "2026-07-05", "WFH", late=7), lg("E1", "2026-07-06", "HOLIDAY", late=9)]
        item = run(emps, logs)["items"][0]
        self.assertEqual((item["late_count"], item["total_late_minutes"]), (4, 24))

    def test_leave_counts_on_weekends_and_on_duty_is_both_presence_and_on_duty(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-07-04", "LEAVE"), lg("E1", "2026-07-05", "LEAVE"), lg("E1", "2026-07-06", "LEAVE"),
                lg("E1", "2026-07-07", "ON_DUTY"), lg("E1", "2026-07-11", "ON_DUTY")]
        item = run(emps, logs)["items"][0]
        self.assertEqual((item["leave_count"], item["on_duty_count"], item["present_days"]), (3, 2, 1.0))

    def test_other_months_excluded_and_first_and_last_day_included(self):
        emps = [emp("E1", "D")]
        logs = [lg("E1", "2026-06-30", "LEAVE", late=1), lg("E1", "2026-07-01", "LEAVE", late=2),
                lg("E1", "2026-07-31", "LEAVE", late=4), lg("E1", "2026-08-01", "LEAVE", late=8)]
        item = run(emps, logs)["items"][0]
        self.assertEqual((item["leave_count"], item["late_count"], item["total_late_minutes"]), (2, 2, 6))

    def test_records_before_joined_on_inside_the_month_are_included(self):          # carried-forward 5.1 decision
        emps = [emp("E1", "D", "2026-07-15")]
        logs = [lg("E1", "2026-07-13", late=10, hours=8.0), lg("E1", "2026-07-15", hours=9.0)]
        item = run(emps, logs)["items"][0]
        self.assertEqual((item["present_days"], item["late_count"], item["avg_work_hours"]), (2.0, 1, 8.5))

    def test_legacy_documents_do_not_break_the_aggregation(self):
        emps = [emp("E1", "D")]
        logs = [without(lg("E1", "2026-07-01"), "half_day", "late_minutes", "work_hours"),
                lg("E1", "2026-07-02", half=None, late=None, hours=None),
                without(lg("E1", "2026-07-03", "WFH", half=True, hours=7.0), "late_minutes"),
                without(lg("E1", "2026-07-06", late=15, hours=9.0), "half_day"),
                without(lg("E1", "2026-07-07", "LEAVE"), "half_day", "late_minutes", "work_hours", "history")]
        item = run(emps, logs)["items"][0]
        self.assertEqual((item["present_days"], item["avg_work_hours"], item["late_count"], item["total_late_minutes"],
                          item["leave_count"]), (3.5, 8.0, 1, 15, 1))     # halves: 2 + 2 + 1 + 2 = 7

    def test_empty_database_and_empty_month(self):
        self.assertEqual(run([], []), {"month": "2026-07", "items": []})
        item = run([emp("E1", "D")], [lg("E1", "2026-06-15")])["items"][0]
        self.assertEqual((item["headcount"], item["present_days"], item["avg_work_hours"], item["late_count"]), (1, 0.0, None, 0))

    def test_matches_the_independent_oracle_on_random_data(self):
        rng = random.Random(5252)
        statuses = ["PRESENT"] * 4 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE", "HOLIDAY"]
        depts = ["Engineering", "Sales", "Support", "engineering", "HR", "Ops", None, "", 5]
        emps = [emp(f"E{i:03d}", rng.choice(depts), f"2026-{rng.randint(1, 9):02d}-{rng.randint(1, 28):02d}") for i in range(60)]
        logs = []
        for e in emps + [emp("GHOST", "Sales")]:
            for month in (6, 7, 8):
                for day in rng.sample(range(1, calendar.monthrange(2026, month)[1] + 1), 12):
                    d = lg(e["emp_code"], f"2026-{month:02d}-{day:02d}", rng.choice(statuses),
                           late=rng.choice([0, 0, 1, 10, 11, 45, -5, None]), hours=rng.choice([None, 0.0, round(rng.uniform(0, 12), 2)]),
                           half=rng.choice([False, False, True, None]))
                    for k in ("half_day", "late_minutes", "work_hours"):
                        if rng.random() < 0.08:
                            d.pop(k)
                    logs.append(d)
        logs = [l for l in logs if l["emp_code"] != "GHOST"] + [l for l in logs if l["emp_code"] == "GHOST"]
        emps = [e for e in emps if e["emp_code"] != "GHOST"]                    # GHOST has logs but no employee: an orphan
        for month in (6, 7, 8, 9):
            for dept in (None, "Engineering", "Sales", "engineering", "Nope", ""):
                with self.subTest(month=month, department=dept):
                    self.assertEqual(run(emps, logs, 2026, month, dept), oracle(emps, logs, 2026, month, dept))


# ======================================================================== 3. endpoint on a fake DB
if FULL:
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from tests._fakes_p52 import FakeDBP52


@unittest.skipUnless(FULL, "needs fastapi/pydantic/pymongo installed: NOT EXECUTED")
class FakeDbCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDBP52()
        self._orig_db = m.db
        m.db = self.fake

    def tearDown(self):
        m.db = self._orig_db

    def load(self, emps, logs):
        for i, e in enumerate(emps):
            self.fake.employees.docs.append({"_id": f"e{i}", **e})
        for i, l in enumerate(logs):
            self.fake.attendance_logs.docs.append({"_id": f"a{i}", **l})


class TestDepartmentEndpointFake(FakeDbCase):
    def sample(self):
        emps = json.loads((ROOT / "sample_data" / "employees.json").read_text())
        logs = json.loads((ROOT / "sample_data" / "attendance_logs.json").read_text())
        self.load(emps, logs)
        return emps, logs

    def test_exact_envelope_and_sample_values(self):
        emps, logs = self.sample()
        r = m.department_summary(month="2026-07", department=None)
        self.assertEqual(list(r), ["month", "items"])
        self.assertEqual(r["month"], "2026-07")
        for item in r["items"]:
            self.assertEqual(list(item), ITEM_KEYS)
            self.assertNotIn("_id", item)
        self.assertEqual(r, oracle(emps, logs, 2026, 7))

    def test_uses_one_aggregation_built_by_the_builder_and_never_find(self):
        self.sample()
        m.department_summary(month="2026-07", department="Sales")
        self.assertEqual(self.fake.employees.calls, ["aggregate"])
        self.assertEqual(self.fake.attendance_logs.calls, [])                     # the $lookup is inside the aggregation
        self.assertEqual(self.fake.employees.last_pipeline, m.department_summary_pipeline(2026, 7, "Sales"))

    def test_department_filter_variants(self):
        self.sample()
        self.assertEqual([i["department"] for i in m.department_summary("2026-07", "Sales")["items"]], ["Sales"])
        for dept in ("sales", "SALES", "Sal", "Nope", ""):
            with self.subTest(department=dept):
                self.assertEqual(m.department_summary("2026-07", dept), {"month": "2026-07", "items": []})

    def test_empty_month_and_empty_collections_are_200_with_empty_items(self):
        self.assertEqual(m.department_summary("2026-07", None), {"month": "2026-07", "items": []})
        self.sample()
        r = m.department_summary("2025-01", None)                                  # nobody had joined yet
        self.assertEqual(r, {"month": "2025-01", "items": []})

    def test_invalid_months_are_422_before_any_database_call(self):
        for bad in ("2026-13", "2026-1", "0000-05", "2026-00", "202607", "2026-07-01", "", "abc"):
            with self.subTest(month=bad), self.assertRaises(RequestValidationError) as cm:
                m.department_summary(month=bad, department=None)
            self.assertEqual(cm.exception.errors()[0]["loc"], ("query", "month"))
        self.assertEqual(self.fake.employees.calls, [])

    def test_a_database_error_is_not_swallowed(self):
        class Boom:
            def aggregate(self, pipeline):
                raise RuntimeError("boom")
        self.fake.employees = Boom()
        with self.assertRaises(RuntimeError):
            m.department_summary("2026-07", None)

    def test_response_is_json_serialisable_and_types_match_the_contract(self):
        self.sample()
        text = json.dumps(m.department_summary("2026-07", None))
        self.assertNotIn("_id", text)
        for item in json.loads(text)["items"]:
            for k in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
                self.assertIs(type(item[k]), int, k)


@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestDepartmentHttp(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)

    def get(self, qs):
        return self.c.get("/analytics/departments/summary" + (f"?{qs}" if qs is not None else ""))

    def test_200_envelope_and_item_schema(self):
        emps = json.loads((ROOT / "sample_data" / "employees.json").read_text())
        logs = json.loads((ROOT / "sample_data" / "attendance_logs.json").read_text())
        self.load(emps, logs)
        r = self.get("month=2026-07")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(list(body), ["month", "items"])
        self.assertEqual([list(i) for i in body["items"]], [ITEM_KEYS] * 3)
        self.assertEqual(body, oracle(emps, logs, 2026, 7))
        self.assertNotIn("_id", r.text)

    def test_department_filter_over_http_exact_case_sensitive(self):
        self.load([emp("1", "Sales"), emp("2", "sales")], [])
        self.assertEqual([i["department"] for i in self.get("month=2026-07&department=Sales").json()["items"]], ["Sales"])
        self.assertEqual(self.get("month=2026-07&department=SALES").json(), {"month": "2026-07", "items": []})
        r = self.get("month=2026-07&department=")
        self.assertEqual((r.status_code, r.json()), (200, {"month": "2026-07", "items": []}))      # explicit "" is NOT "no filter"
        self.assertEqual(self.get("month=2026-07&department=Unknown").status_code, 200)             # never a 404

    def test_422_missing_and_invalid_month(self):
        for qs in (None, "", "department=Sales", "month=", "month=2026-13", "month=2026-1", "month=0000-05",
                   "month=2026-00", "month=202607", "month=2026-07-01", "month=abc"):
            with self.subTest(qs=qs):
                r = self.get(qs)
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)

    def test_no_404_path_exists_and_openapi_lists_both_parameters(self):
        op = self.c.get("/openapi.json").json()["paths"]["/analytics/departments/summary"]["get"]
        names = {p["name"]: p for p in op["parameters"]}
        self.assertTrue(names["month"]["required"])
        self.assertFalse(names["department"]["required"])
        self.assertNotIn("404", op["responses"])


# ======================================================================== 4. REAL MongoDB (opt-in)
def seed_world(rng, n_emps, days_per_emp, months=(7,), invalid=True):
    """Deterministic random world with unique (emp_code, date) logs."""
    depts = ["Engineering", "Sales", "Support", "engineering", "HR", "Ops", "Legal", "Data"]
    emps, logs = [], []
    for i in range(n_emps):
        dept = rng.choice(depts)
        if invalid and i % 41 == 0:
            dept = rng.choice([None, "", 5])
        e = emp(f"E{i:05d}", dept, f"2026-{rng.randint(1, 9):02d}-{rng.randint(1, 28):02d}")
        if e["department"] is None and i % 82 == 0:
            e.pop("department")
        emps.append(e)
        for month in months:
            for day in rng.sample(range(1, calendar.monthrange(2026, month)[1] + 1), days_per_emp):
                st = rng.choice(["PRESENT"] * 5 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])
                d = lg(e["emp_code"], f"2026-{month:02d}-{day:02d}", st, late=rng.choice([0, 0, 1, 10, 11, 45, -5, None]),
                       hours=rng.choice([None, 0.0, round(rng.uniform(0, 12), 2), round(rng.uniform(0, 12), 2)]),
                       half=rng.choice([False, False, True, None]))
                for k in ("half_day", "late_minutes", "work_hours"):
                    if rng.random() < 0.05:
                        d.pop(k)
                logs.append(d)
    return emps, logs


@unittest.skipUnless(FULL and HAVE_HTTP and TEST_URI,
                     "set TEST_MONGO_URI to run the real-MongoDB integration tests: NOT EXECUTED")
class TestMongoDepartmentSummary(unittest.TestCase):
    """REAL MongoDB in a throwaway database (attendance_test_p52_<random>), dropped at the end."""
    PREFIX = "attendance_test_p52_"

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
        for c in ("employees", "attendance_logs", "hundredths_probe"):
            self.tdb[c].delete_many({})

    def seed(self, emps, logs):
        if emps:
            self.tdb.employees.insert_many([dict(e) for e in emps])
        if logs:
            self.tdb.attendance_logs.insert_many([dict(l) for l in logs])

    def summary(self, month="2026-07", department=None):
        return m.department_summary(month=month, department=department)

    def test_server_version_is_reported(self):
        print(f"\n[real MongoDB server version: {self.server_version}]")
        self.assertTrue(self.server_version)

    def test_index_definitions_on_the_real_server_are_exactly_the_six_phase_1_ones(self):
        e = {tuple(v["key"]) for k, v in self.tdb.employees.index_information().items() if k != "_id_"}
        a = {tuple(v["key"]) for k, v in self.tdb.attendance_logs.index_information().items() if k != "_id_"}
        self.assertEqual(e, {(("emp_code", 1),), (("department", 1), ("emp_code", 1))})
        self.assertEqual(len(a), 4)

    def test_sample_data_matches_the_hand_computed_table(self):
        from bson import json_util
        emps = json_util.loads((ROOT / "sample_data" / "employees.json").read_text())
        logs = json_util.loads((ROOT / "sample_data" / "attendance_logs.json").read_text())
        self.seed(emps, logs)
        r = self.summary()
        self.assertEqual([i["department"] for i in r["items"]], ["Engineering", "Sales", "Support"])
        for item in r["items"]:
            self.assertEqual({k: v for k, v in item.items() if k != "department"}, SAMPLE_EXPECTED[item["department"]])
        self.assertEqual(r, oracle(emps, logs, 2026, 7))

    def test_headcount_rules_real_mongo(self):
        emps = [emp("A1", "Alpha", "2026-01-01"), emp("A2", "Alpha", "2026-07-31"), emp("A3", "Alpha", "2026-08-01"),
                emp("B1", "Beta", "2026-08-15"), emp("C1", "Gamma", "2026-03-01"), emp("X1", None), emp("X2", ""), emp("X3", 7),
                without(emp("X4", "q"), "department")]
        logs = [lg("A1", f"2026-07-{d:02d}") for d in (1, 2, 3, 6, 7)] + [
            lg("A3", "2026-07-06", late=99, hours=9.0), lg("GHOST", "2026-07-06", late=77), lg("B1", "2026-07-06", late=55),
            lg("X1", "2026-07-06", late=11)]
        self.seed(emps, logs)
        r = items_by_dept(self.summary())
        self.assertEqual(set(r), {"Alpha", "Gamma"})
        self.assertEqual((r["Alpha"]["headcount"], r["Gamma"]["headcount"], r["Alpha"]["present_days"]), (2, 1, 5.0))
        self.assertEqual((r["Alpha"]["total_late_minutes"], r["Gamma"]["avg_work_hours"]), (0, None))
        self.assertEqual(self.summary(department=""), {"month": "2026-07", "items": []})

    def test_matches_the_oracle_on_random_data_with_and_without_filter(self):
        rng = random.Random(520)
        emps, logs = seed_world(rng, 120, 14, months=(6, 7, 8))
        self.seed(emps, logs)
        for month in (6, 7, 8, 9):
            for dept in (None, "Engineering", "Sales", "engineering", "HR", "Nope", ""):
                with self.subTest(month=month, department=dept):
                    self.assertEqual(self.summary(f"2026-{month:02d}", dept), oracle(emps, logs, 2026, month, dept))

    def test_half_up_averages_on_real_mongo(self):
        cases = {"T1": (8.00, 8.01), "T2": (8.00, 8.03), "T3": (7.99, 8.00), "T4": (0.00, 0.01), "T5": (0.0, 0.0),
                 "T6": (8.01, 8.02, 8.02), "T7": (8.00, 8.00, 8.01), "T8": (0.0,), "T9": (12.34, 12.35)}
        emps, logs = [], []
        for dept, hours in cases.items():
            for j, h in enumerate(hours):
                code = f"{dept}-{j}"
                emps.append(emp(code, dept))
                logs.append(lg(code, "2026-07-01", hours=h))
        self.seed(emps, logs)
        got = {i["department"]: i["avg_work_hours"] for i in self.summary()["items"]}
        expected = {d: float((sum(Decimal(str(h)) for h in hs) / len(hs)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    for d, hs in cases.items()}
        self.assertEqual(got, expected)
        self.assertEqual(got["T1"], 8.01)            # the contract's own example
        self.assertEqual(got["T8"], 0.0)

    def test_hundredths_conversion_is_exact_for_every_value_0_to_100_on_real_mongo(self):
        self.tdb.hundredths_probe.insert_many([{"_id": k, "w": k / 100} for k in range(0, 10001)])
        rows = list(self.tdb.hundredths_probe.aggregate([{"$project": {"h": m.work_hours_hundredths("$w")}}]))
        self.assertEqual(len(rows), 10001)
        wrong = [(r["_id"], r["h"]) for r in rows if r["h"] != r["_id"]]
        self.assertEqual(wrong, [], f"first mismatches: {wrong[:5]}")

    def test_mean_is_across_records_on_real_mongo(self):
        emps = [emp("E1", "D"), emp("E2", "D")]
        logs = [lg("E1", "2026-07-01", hours=8.0), lg("E1", "2026-07-02", hours=8.0), lg("E1", "2026-07-03", hours=8.0),
                lg("E2", "2026-07-01", hours=12.0), lg("E1", "2026-07-06", "ABSENT", hours=99.0)]
        self.seed(emps, logs)
        self.assertEqual(self.summary()["items"][0]["avg_work_hours"], 9.0)

    def test_empty_result_shapes_real_mongo(self):
        self.assertEqual(self.summary(), {"month": "2026-07", "items": []})
        self.seed([emp("E1", "D")], [])
        item = self.summary()["items"][0]
        self.assertEqual((item["headcount"], item["present_days"], item["avg_work_hours"]), (1, 0.0, None))
        self.assertEqual(self.summary(department="Nope"), {"month": "2026-07", "items": []})

    # ---------------- the release-blocker checks: real EXPLAIN, filtered and unfiltered ----------------
    def seed_large(self):
        rng = random.Random(7)
        n_emps, per = 2000, 10                                            # 2,000 employees x 10 July logs = 20,000 records
        emps, logs = seed_world(rng, n_emps, per, months=(7,), invalid=False)
        self.seed(emps, logs)
        self.assertEqual(self.tdb.attendance_logs.count_documents({}), n_emps * per)
        return emps, logs

    def explain(self, department):
        pipeline = m.department_summary_pipeline(2026, 7, department)
        return self.tdb.command({"explain": {"aggregate": m.DEPARTMENT_SUMMARY_COLLECTION, "pipeline": pipeline, "cursor": {}},
                                 "verbosity": "executionStats"})

    def test_explain_unfiltered_uses_an_index_and_no_collection_scan(self):
        self.seed_large()
        out = self.explain(None)
        print(f"\n[explain, NO department filter] server={self.server_version} {describe(out)}")
        s = summarize(out)
        msg = f"unfiltered plan: {describe(out)}"
        self.assertTrue(s["has_IXSCAN"], msg)
        self.assertFalse(s["has_COLLSCAN"], msg)
        for counters in s["lookup_counters"]:
            if "collectionScans" in counters:
                self.assertEqual(counters["collectionScans"], 0, msg)

    def test_explain_with_department_filter_uses_an_index_and_no_collection_scan(self):
        self.seed_large()
        out = self.explain("Engineering")
        print(f"\n[explain, department=Engineering] server={self.server_version} {describe(out)}")
        s = summarize(out)
        msg = f"filtered plan: {describe(out)}"
        self.assertTrue(s["has_IXSCAN"], msg)
        self.assertFalse(s["has_COLLSCAN"], msg)
        for counters in s["lookup_counters"]:
            if "collectionScans" in counters:
                self.assertEqual(counters["collectionScans"], 0, msg)

    def test_large_dataset_results_still_match_the_oracle(self):
        emps, logs = self.seed_large()
        for dept in (None, "Engineering", "engineering"):
            with self.subTest(department=dept):
                self.assertEqual(self.summary(department=dept), oracle(emps, logs, 2026, 7, dept))

    def test_http_flow_with_real_mongo(self):
        from fastapi.testclient import TestClient
        self.seed([emp("E1", "Sales"), emp("E2", "Sales", "2026-08-02")], [lg("E1", "2026-07-01", hours=8.0)])
        c = TestClient(m.app)
        r = c.get("/analytics/departments/summary?month=2026-07")
        self.assertEqual((r.status_code, r.json()), (200, {"month": "2026-07", "items": [
            {"department": "Sales", "headcount": 1, "present_days": 1.0, "avg_work_hours": 8.0, "late_count": 0,
             "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}]}))
        self.assertEqual(c.get("/analytics/departments/summary?month=0000-05").status_code, 422)
        self.assertEqual(c.get("/analytics/departments/summary").status_code, 422)
        self.assertEqual(c.get("/analytics/departments/summary?month=2026-07&department=sales").json(),
                         {"month": "2026-07", "items": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
