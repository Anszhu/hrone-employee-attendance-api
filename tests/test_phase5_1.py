"""Phase 5.1 tests: GET /analytics/employees/{emp_code}/monthly?month=YYYY-MM

Layers (each says what it proves):
  1. TestMonthParsing / TestWorkingDays / TestRounding / TestPipelineShape   pure; run anywhere.
  2. TestPipelineSemanticsEmulated   the REAL pipeline executed by tests/_agg_emulator.py, an EMULATION of the
                                     MongoDB operators it uses (not MongoDB). Catches logic mistakes without a server.
  3. TestMonthlyEndpointFake / TestMonthlyHttp   endpoint on an in-memory fake whose aggregate() uses the emulator.
                                     MOCKED: proves routing, 404/422 mapping, "aggregate not find".
  4. TestMongoMonthly                REAL MongoDB, opt-in via TEST_MONGO_URI; disposable database
                                     attendance_test_p51_<random>, dropped afterwards. Never touches MONGO_DB.
"""
import calendar
import json
import os
import random
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

from tests._agg_emulator import mini_aggregate
from tests._loader import load_main, have

m, MODE = load_main()
FULL = MODE == "full"
HAVE_HTTP = FULL and have("httpx")
TEST_URI = os.environ.get("TEST_MONGO_URI")

MONTHLY_KEYS = ["emp_code", "month", "working_days", "present_days", "leave_days", "late_count",
                "total_late_minutes", "total_overtime_minutes", "attendance_pct"]
SENTINEL = object()


def rec(emp, date, status="PRESENT", late=0, ot=0, half=False, **extra):
    d = {"emp_code": emp, "date": date, "status": status, "late_minutes": late, "overtime_minutes": ot,
         "half_day": half, "punch_in": None, "punch_out": None, "work_hours": None, "history": []}
    d.update(extra)
    return d


def without(doc, *keys):
    return {k: v for k, v in doc.items() if k not in keys}


# ---------------- independent oracle: different algorithm from the implementation ----------------
def oracle_working_days(joined_on, year, month):
    jy, jm, jd = map(int, joined_on.split("-"))
    n = 0
    for week in calendar.monthcalendar(year, month):          # rows are Mon..Sun; 0 = outside the month
        for col, day in enumerate(week):
            if day and col <= 4 and (year, month, day) >= (jy, jm, jd):
                n += 1
    return n


def oracle_monthly(docs, emp_code, joined_on, year, month):
    prefix = f"{year:04d}-{month:02d}-"
    mine = [d for d in docs if d["emp_code"] == emp_code and d["date"].startswith(prefix)]
    wd = oracle_working_days(joined_on, year, month)
    present = Fraction(0)
    for d in mine:
        weekday = calendar.weekday(year, month, int(d["date"][8:10]))
        if d["status"] in ("PRESENT", "WFH", "ON_DUTY") and weekday <= 4:
            present += Fraction(1, 2) if d.get("half_day") is True else 1
    pct = None
    if wd:
        exact = present * 100 / wd                             # exact Fraction, no floats
        hundredths = (exact * 100 + Fraction(1, 2)).__floor__()  # half-up
        pct = float(Decimal(hundredths) / 100)
    g = lambda d, k: d.get(k) or 0
    return {"emp_code": emp_code, "month": f"{year:04d}-{month:02d}", "working_days": wd,
            "present_days": float(present), "leave_days": sum(1 for d in mine if d["status"] == "LEAVE"),
            "late_count": sum(1 for d in mine if g(d, "late_minutes") > 0),
            "total_late_minutes": sum(g(d, "late_minutes") for d in mine),
            "total_overtime_minutes": sum(g(d, "overtime_minutes") for d in mine), "attendance_pct": pct}


# ---------------- scenario data (hand-computed expectations in the tests) ----------------
JULY_EARLY = "2026-01-05"          # joined long before: July 2026 has 23 working days
SCENARIO_A = [
    rec("EMP0001", "2026-06-30", late=5, ot=5),                        # other month: excluded
    rec("EMP0001", "2026-07-01"),                                      # first day of the month: included
    rec("EMP0001", "2026-07-02", "WFH", late=12, half=True),           # half day -> 0.5
    rec("EMP0001", "2026-07-03", "ON_DUTY", punch_in=None, punch_out=None),   # open record -> full day
    rec("EMP0001", "2026-07-04", "PRESENT", late=20, ot=45),           # Saturday: totals only
    rec("EMP0001", "2026-07-05", "LEAVE"),                             # Sunday LEAVE still counts as leave
    rec("EMP0001", "2026-07-06", "ABSENT"),
    rec("EMP0001", "2026-07-07", "LEAVE"),
    rec("EMP0001", "2026-07-31", "PRESENT", late=31, ot=30),           # last day: included
    rec("EMP0001", "2026-08-01", "PRESENT", late=77, ot=77),           # other month: excluded
    rec("EMP0002", "2026-07-01", "PRESENT", late=99, ot=99),           # other employee: excluded
]
SCENARIO_A_EXPECTED = dict(emp_code="EMP0001", month="2026-07", working_days=23, present_days=3.5, leave_days=2,
                           late_count=3, total_late_minutes=63, total_overtime_minutes=75, attendance_pct=15.22)


# ======================================================================== 1. pure
class TestMonthParsing(unittest.TestCase):
    def test_valid_months(self):
        for value, expected in (("2026-07", (2026, 7)), ("2026-12", (2026, 12)), ("2026-01", (2026, 1)),
                                ("0001-01", (1, 1)), ("9999-12", (9999, 12)), ("2028-02", (2028, 2))):
            with self.subTest(value=value):
                self.assertEqual(m.parse_month(value), expected)

    def test_invalid_months(self):
        for bad in ("2026-13", "2026-00", "2026-7", "202607", "2026-07-01", "", "0000-05", " 2026-07", "2026-07 ",
                    "2026-07\n", "26-07", "2026/07", "abcd-ef", "\uff12\uff10\uff12\uff16-07", None, 202607, 7):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                m.parse_month(bad)

    def test_pattern_constant_matches_the_contract(self):
        import re
        self.assertEqual(m.MONTH_PATTERN, r"^[0-9]{4}-(0[1-9]|1[0-2])$")
        self.assertIsNotNone(re.fullmatch(m.MONTH_PATTERN, "2026-07"))
        self.assertIsNone(re.fullmatch(m.MONTH_PATTERN, "2026-13"))
        self.assertIsNotNone(re.fullmatch(m.MONTH_PATTERN, "0000-05"))      # pattern alone lets it through ...
        with self.assertRaises(ValueError):
            m.parse_month("0000-05")                                        # ... parse_month does not

    def test_bounds_and_weekends(self):
        self.assertEqual(m.month_bounds(2026, 7), ("2026-07-01", "2026-07-31"))
        self.assertEqual(m.month_bounds(2026, 2), ("2026-02-01", "2026-02-28"))
        self.assertEqual(m.month_bounds(2028, 2), ("2028-02-01", "2028-02-29"))
        self.assertEqual(m.month_bounds(1900, 2), ("1900-02-01", "1900-02-28"))      # not a leap year
        self.assertEqual(m.month_bounds(2000, 2), ("2000-02-01", "2000-02-29"))      # leap year
        self.assertEqual(m.weekend_dates(2026, 7), ["2026-07-04", "2026-07-05", "2026-07-11", "2026-07-12",
                                                    "2026-07-18", "2026-07-19", "2026-07-25", "2026-07-26"])
        for y, mo in ((2026, 7), (2026, 2), (2028, 2), (2026, 1), (2026, 8)):
            for d in m.weekend_dates(y, mo):
                self.assertGreaterEqual(calendar.weekday(*map(int, d.split("-"))), 5)
            self.assertEqual(len(m.weekend_dates(y, mo)) + oracle_working_days("0001-01-01", y, mo),
                             calendar.monthrange(y, mo)[1])


class TestWorkingDays(unittest.TestCase):
    def test_literal_cases(self):
        wd = m.count_working_days
        self.assertEqual(wd(JULY_EARLY, 2026, 7), 23)            # joined before the month
        self.assertEqual(wd("2026-07-01", 2026, 7), 23)          # joined on the first day (Wed)
        self.assertEqual(wd("2026-07-10", 2026, 7), 16)          # joins mid-month (Fri)
        self.assertEqual(wd("2026-07-31", 2026, 7), 1)           # joins on the final weekday (Fri)
        self.assertEqual(wd("2026-07-04", 2026, 7), 20)          # joins on a Saturday
        self.assertEqual(wd("2026-07-05", 2026, 7), 20)          # joins on a Sunday
        self.assertEqual(wd("2026-08-03", 2026, 7), 0)           # joins after the month
        self.assertEqual(wd("2026-08-01", 2026, 7), 0)           # ... the day after it ends
        self.assertEqual(wd("2026-01-31", 2026, 1), 0)           # joins on the last day, a Saturday
        self.assertEqual(wd("2026-01-30", 2026, 1), 1)           # ... the Friday before
        self.assertEqual(wd("2020-01-01", 2026, 2), 20)          # February 2026
        self.assertEqual(wd("2020-01-01", 2027, 2), 20)          # February 2027 (4 exact weeks)
        self.assertEqual(wd("2020-01-01", 2028, 2), 21)          # leap-year February 2028
        self.assertEqual(wd("2020-01-01", 2024, 2), 21)          # leap-year February 2024
        self.assertEqual(wd("2020-01-01", 2026, 12), 23)

    def test_agrees_with_an_independent_calendar_oracle_for_every_joiner(self):
        from datetime import date, timedelta
        d, end, checked = date(2026, 5, 20), date(2026, 9, 10), 0
        while d <= end:
            for year, month in ((2026, 6), (2026, 7), (2026, 8), (2026, 9)):
                with self.subTest(joined=d.isoformat(), month=f"{year}-{month:02d}"):
                    self.assertEqual(m.count_working_days(d.isoformat(), year, month),
                                     oracle_working_days(d.isoformat(), year, month))
                checked += 1
            d += timedelta(days=1)
        self.assertGreater(checked, 400)

    def test_leap_years_and_century_rules(self):
        for year, days in ((2024, 29), (2028, 29), (2000, 29), (1900, 28), (2100, 28), (2026, 28)):
            self.assertEqual(m.month_bounds(year, 2)[1], f"{year}-02-{days}")

    def test_invalid_joined_on_is_an_error_not_a_guess(self):
        for bad in ("2026-02-30", "yesterday", "", None):
            with self.assertRaises(ValueError):
                m.count_working_days(bad, 2026, 7)


class TestRounding(unittest.TestCase):
    def test_half_up_ties_that_naive_rounding_gets_wrong(self):
        self.assertEqual(m.attendance_pct_half_up(1, 16), 3.13)      # 0.5/16*100 = 3.125 -> 3.13
        self.assertEqual(m.attendance_pct_half_up(5, 16), 15.63)     # 2.5/16*100 = 15.625 -> 15.63
        # the shortcut the contract warns about disagrees on exactly these inputs (half-to-even):
        self.assertEqual(round(0.5 / 16 * 100, 2), 3.12)
        self.assertEqual(round(2.5 / 16 * 100, 2), 15.62)
        self.assertNotEqual(m.attendance_pct_half_up(1, 16), round(0.5 / 16 * 100, 2))
        self.assertNotEqual(m.attendance_pct_half_up(5, 16), round(2.5 / 16 * 100, 2))

    def test_ordinary_values(self):
        self.assertEqual(m.attendance_pct_half_up(7, 23), 15.22)     # 3.5/23*100 = 15.2174
        self.assertEqual(m.attendance_pct_half_up(4, 23), 8.7)       # 2/23*100 = 8.6957
        self.assertEqual(m.attendance_pct_half_up(2, 15), 6.67)
        self.assertEqual(m.attendance_pct_half_up(46, 23), 100.0)
        self.assertEqual(m.attendance_pct_half_up(1, 23), 2.17)      # 0.5/23*100 = 2.1739

    def test_zero_cases(self):
        self.assertIsNone(m.attendance_pct_half_up(0, 0))
        self.assertIsNone(m.attendance_pct_half_up(5, 0))
        v = m.attendance_pct_half_up(0, 23)
        self.assertEqual(v, 0.0)
        self.assertIsInstance(v, float)

    def test_exhaustive_against_an_exact_fraction_oracle(self):
        for wd in range(1, 24):
            for halves in range(0, 2 * wd + 8):                     # includes values above 100%
                exact = Fraction(halves, 2) * 100 / wd
                expected = float(Decimal((exact * 100 + Fraction(1, 2)).__floor__()) / 100)
                self.assertEqual(m.attendance_pct_half_up(halves, wd), expected, (halves, wd))

    def test_uses_no_python_round_and_no_mongo_round(self):
        import ast
        from tests._loader import MAIN
        fn = next(n for n in ast.walk(ast.parse(MAIN.read_text()))
                  if isinstance(n, ast.FunctionDef) and n.name == "attendance_pct_half_up")
        calls = [c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        self.assertNotIn("round", calls)                       # no built-in round() in the rounding helper
        self.assertNotIn("$round", json.dumps(m.employee_monthly_pipeline("EMP0001", 2026, 7)))   # nor Mongo's


class TestBuildResponse(unittest.TestCase):
    def test_empty_month_is_all_zeros_not_an_error(self):
        for row in (None, {}):
            r = m.build_employee_monthly("EMP0001", "2026-07", 23, row)
            self.assertEqual(r, dict(emp_code="EMP0001", month="2026-07", working_days=23, present_days=0.0,
                                     leave_days=0, late_count=0, total_late_minutes=0, total_overtime_minutes=0,
                                     attendance_pct=0.0))

    def test_zero_working_days_gives_null_pct(self):
        self.assertIsNone(m.build_employee_monthly("E", "2026-07", 0, None)["attendance_pct"])
        self.assertIsNone(m.build_employee_monthly("E", "2026-07", 0, {"present_halves": 4})["attendance_pct"])

    def test_field_order_names_and_types(self):
        r = m.build_employee_monthly("EMP0001", "2026-07", 23, {"present_halves": 7, "leave_days": 2, "late_count": 3,
                                                                "total_late_minutes": 63, "total_overtime_minutes": 75})
        self.assertEqual(list(r), MONTHLY_KEYS)
        self.assertEqual(r["present_days"], 3.5)
        self.assertIsInstance(r["present_days"], float)
        for k in ("working_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"):
            self.assertIs(type(r[k]), int, k)
        self.assertIsInstance(r["attendance_pct"], float)
        self.assertNotIn("_id", r)

    def test_float_valued_sums_from_mongo_become_ints(self):
        r = m.build_employee_monthly("E", "2026-07", 23, {"present_halves": 2.0, "late_count": 1.0,
                                                          "total_late_minutes": 15.0, "total_overtime_minutes": 7.0})
        self.assertIs(type(r["total_late_minutes"]), int)
        self.assertEqual(r["present_days"], 1.0)


class TestPipelineShape(unittest.TestCase):
    def test_two_stages_match_first_then_group(self):
        p = m.employee_monthly_pipeline("EMP0001", 2026, 7)
        self.assertEqual([next(iter(s)) for s in p], ["$match", "$group"])
        self.assertEqual(p[0]["$match"], {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}})
        self.assertIsNone(p[1]["$group"]["_id"])

    def test_match_uses_exactly_the_indexed_fields(self):
        # unique (emp_code, date): equality on the first key, range on the second -> a single IXSCAN
        self.assertEqual(set(m.employee_monthly_pipeline("E", 2026, 7)[0]["$match"]), {"emp_code", "date"})
        self.assertIn([("emp_code", 1), ("date", 1)], [k for k, o in m.INDEXES["attendance_logs"] if o.get("unique")])

    def test_no_forbidden_stages_or_operators(self):
        text = json.dumps(m.employee_monthly_pipeline("EMP0001", 2026, 7))
        for forbidden in ("$round", "$sort", "$lookup", "$unwind", "$divide", "$multiply", "$function", "$where"):
            self.assertNotIn(forbidden, text)

    def test_legacy_defaults_are_expressed_with_ifnull(self):
        text = json.dumps(m.employee_monthly_pipeline("EMP0001", 2026, 7))
        for field in ("late_minutes", "overtime_minutes", "half_day"):
            self.assertIn('{"$ifNull": ["$%s"' % field, text)

    def test_builder_is_deterministic_and_month_specific(self):
        a = m.employee_monthly_pipeline("EMP0001", 2026, 7)
        self.assertEqual(a, m.employee_monthly_pipeline("EMP0001", 2026, 7))
        self.assertNotEqual(a, m.employee_monthly_pipeline("EMP0001", 2026, 8))
        self.assertEqual(m.employee_monthly_pipeline("EMP0001", 2028, 2)[0]["$match"]["date"]["$lte"], "2028-02-29")
        self.assertIn("2026-07-04", json.dumps(a))

    def test_emp_code_is_passed_verbatim_and_case_sensitive(self):
        self.assertEqual(m.employee_monthly_pipeline("emp0001", 2026, 7)[0]["$match"]["emp_code"], "emp0001")


# ======================================================================== 2. emulated pipeline semantics
def run_pipeline(docs, emp, year, month, joined=JULY_EARLY):
    rows = mini_aggregate(docs, m.employee_monthly_pipeline(emp, year, month))
    return m.build_employee_monthly(emp, f"{year:04d}-{month:02d}", m.count_working_days(joined, year, month),
                                    rows[0] if rows else None)


class TestPipelineSemanticsEmulated(unittest.TestCase):
    """The real pipeline run by an EMULATION of MongoDB's operators (see tests/_agg_emulator.py)."""

    def test_scenario_a_hand_computed(self):
        self.assertEqual(run_pipeline(SCENARIO_A, "EMP0001", 2026, 7), SCENARIO_A_EXPECTED)

    def test_presence_statuses_count_and_absent_leave_do_not(self):
        docs = [rec("E", "2026-07-01", "PRESENT"), rec("E", "2026-07-02", "WFH"), rec("E", "2026-07-03", "ON_DUTY"),
                rec("E", "2026-07-06", "ABSENT"), rec("E", "2026-07-07", "LEAVE")]
        r = run_pipeline(docs, "E", 2026, 7)
        self.assertEqual((r["present_days"], r["leave_days"], r["attendance_pct"]), (3.0, 1, 13.04))

    def test_unknown_status_strings_are_not_presence(self):
        docs = [rec("E", "2026-07-01", "present"), rec("E", "2026-07-02", "HOLIDAY"), rec("E", "2026-07-03", None)]
        self.assertEqual(run_pipeline(docs, "E", 2026, 7)["present_days"], 0.0)

    def test_half_day_and_full_day(self):
        docs = [rec("E", "2026-07-01", half=True), rec("E", "2026-07-02", half=False), rec("E", "2026-07-03", "WFH", half=True)]
        self.assertEqual(run_pipeline(docs, "E", 2026, 7)["present_days"], 2.0)    # 0.5 + 1 + 0.5

    def test_open_presence_record_counts_as_a_full_day(self):
        self.assertEqual(run_pipeline([rec("E", "2026-07-01", punch_in=None, punch_out=None)], "E", 2026, 7)["present_days"], 1.0)

    def test_weekend_presence_adds_nothing_to_present_days_but_totals_count(self):
        docs = [rec("E", "2026-07-04", "PRESENT", late=20, ot=45), rec("E", "2026-07-05", "WFH", late=5, ot=5, half=True),
                rec("E", "2026-07-11", "ON_DUTY", late=0, ot=10)]
        r = run_pipeline(docs, "E", 2026, 7)
        self.assertEqual((r["present_days"], r["late_count"], r["total_late_minutes"], r["total_overtime_minutes"],
                          r["attendance_pct"]), (0.0, 2, 25, 60, 0.0))

    def test_weekend_leave_counts_in_leave_days(self):
        docs = [rec("E", "2026-07-04", "LEAVE"), rec("E", "2026-07-12", "LEAVE"), rec("E", "2026-07-13", "LEAVE")]
        self.assertEqual(run_pipeline(docs, "E", 2026, 7)["leave_days"], 3)

    def test_late_and_overtime_are_summed_from_stored_values_for_any_status(self):
        docs = [rec("E", "2026-07-01", "ABSENT", late=3, ot=4), rec("E", "2026-07-02", "LEAVE", late=5, ot=6),
                rec("E", "2026-07-03", "WFH", late=7, ot=8)]
        r = run_pipeline(docs, "E", 2026, 7)
        self.assertEqual((r["late_count"], r["total_late_minutes"], r["total_overtime_minutes"]), (3, 15, 18))

    def test_late_count_only_counts_strictly_positive(self):
        docs = [rec("E", "2026-07-01", late=0), rec("E", "2026-07-02", late=1), rec("E", "2026-07-03", late=10)]
        self.assertEqual(run_pipeline(docs, "E", 2026, 7)["late_count"], 2)

    def test_stored_values_are_not_recomputed_from_punch_times(self):
        # punch_in 09:41:01 IST would be 11 minutes late, but the STORED value (0) is what is summed
        docs = [rec("E", "2026-07-01", late=0, ot=0, punch_in=datetime(2026, 7, 1, 4, 11, 1, tzinfo=timezone.utc),
                    punch_out=datetime(2026, 7, 1, 13, 40, tzinfo=timezone.utc))]
        r = run_pipeline(docs, "E", 2026, 7)
        self.assertEqual((r["late_count"], r["total_late_minutes"], r["total_overtime_minutes"]), (0, 0, 0))

    def test_records_before_joined_on_inside_the_month_are_included(self):          # approved C1
        docs = [rec("E", "2026-07-13", late=10), rec("E", "2026-07-15")]
        r = run_pipeline(docs, "E", 2026, 7, joined="2026-07-15")
        self.assertEqual((r["working_days"], r["present_days"], r["late_count"], r["total_late_minutes"],
                          r["attendance_pct"]), (13, 2.0, 1, 10, 15.38))

    def test_tie_cases_end_to_end(self):
        docs = [rec("E", "2026-07-13", "WFH", half=True)]
        r = run_pipeline(docs, "E", 2026, 7, joined="2026-07-10")
        self.assertEqual((r["working_days"], r["present_days"], r["attendance_pct"]), (16, 0.5, 3.13))
        docs += [rec("E", "2026-07-14"), rec("E", "2026-07-15")]
        self.assertEqual(run_pipeline(docs, "E", 2026, 7, joined="2026-07-10")["attendance_pct"], 15.63)

    def test_zero_working_days_null_pct_even_with_records(self):
        r = run_pipeline([rec("E", "2026-07-06")], "E", 2026, 7, joined="2026-08-03")
        self.assertEqual((r["working_days"], r["present_days"], r["attendance_pct"]), (0, 1.0, None))

    def test_no_records_with_working_days_is_a_zero_summary_with_0_0_pct(self):
        r = run_pipeline([], "E", 2026, 7)
        self.assertEqual((r["present_days"], r["leave_days"], r["late_count"], r["attendance_pct"]), (0.0, 0, 0, 0.0))
        self.assertEqual(mini_aggregate([], m.employee_monthly_pipeline("E", 2026, 7)), [])   # empty $group -> no row

    def test_legacy_documents_do_not_break_the_aggregation(self):
        docs = [without(rec("E", "2026-07-01"), "half_day", "late_minutes", "overtime_minutes"),
                rec("E", "2026-07-02", half=None, late=None, ot=None),
                without(rec("E", "2026-07-03", "WFH", half=True, ot=7), "late_minutes"),
                without(rec("E", "2026-07-06", late=15), "overtime_minutes"),
                without(rec("E", "2026-07-07", "LEAVE"), "half_day", "late_minutes", "overtime_minutes", "history")]
        r = run_pipeline(docs, "E", 2026, 7)
        self.assertEqual((r["present_days"], r["leave_days"], r["late_count"], r["total_late_minutes"],
                          r["total_overtime_minutes"], r["attendance_pct"]), (3.5, 1, 1, 15, 7, 15.22))

    def test_other_employees_and_other_months_are_excluded_first_and_last_day_included(self):
        docs = [rec("E", "2026-06-30", late=1), rec("E", "2026-07-01", late=2), rec("E", "2026-07-31", late=4),
                rec("E", "2026-08-01", late=8), rec("OTHER", "2026-07-15", late=16), rec("e", "2026-07-15", late=32)]
        r = run_pipeline(docs, "E", 2026, 7)
        self.assertEqual((r["total_late_minutes"], r["late_count"]), (6, 2))       # exact, case-sensitive emp_code

    def test_matches_the_independent_oracle_on_random_data(self):
        rng = random.Random(2026)
        statuses = ["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
        docs = []
        for emp in ("A", "B", "C"):
            for month in (6, 7, 8):
                for day in range(1, calendar.monthrange(2026, month)[1] + 1):
                    if rng.random() < 0.8:
                        d = rec(emp, f"2026-{month:02d}-{day:02d}", rng.choice(statuses), late=rng.choice([0, 0, 5, 10, 11, 45]),
                                ot=rng.choice([0, 0, 30, 45]), half=rng.random() < 0.15)
                        for k in ("half_day", "late_minutes", "overtime_minutes"):
                            roll = rng.random()
                            if roll < 0.1:
                                d.pop(k)
                            elif roll < 0.2:
                                d[k] = None
                        docs.append(d)
        for emp, joined in (("A", "2026-01-05"), ("B", "2026-07-10"), ("C", "2026-08-03")):
            for month in (6, 7, 8):
                with self.subTest(emp=emp, month=month):
                    self.assertEqual(run_pipeline(docs, emp, 2026, month, joined), oracle_monthly(docs, emp, joined, 2026, month))


# ======================================================================== 3. endpoint on a fake DB
if FULL:
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from tests._fakes_p51 import FakeDBP51


@unittest.skipUnless(FULL, "needs fastapi/pydantic/pymongo installed: NOT EXECUTED")
class FakeDbCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDBP51()
        self._orig_db = m.db
        m.db = self.fake
        self.add_emp("EMP0001", JULY_EARLY)

    def tearDown(self):
        m.db = self._orig_db

    def add_emp(self, code, joined):
        self.fake.employees.docs.append({"_id": "e-" + code, "emp_code": code, "name": "N", "email": "n@x.io",
                                         "department": "D", "shift_start": "09:30", "shift_end": "18:30",
                                         "joined_on": joined, "created_at": datetime(2026, 1, 5, tzinfo=timezone.utc)})

    def load(self, docs):
        for i, d in enumerate(docs):
            self.fake.attendance_logs.docs.append({"_id": f"a{i}", **d})


class TestMonthlyEndpointFake(FakeDbCase):
    def test_scenario_a_through_the_endpoint(self):
        self.load(SCENARIO_A)
        r = m.employee_monthly("EMP0001", "2026-07")
        self.assertEqual(r, SCENARIO_A_EXPECTED)
        self.assertEqual(list(r), MONTHLY_KEYS)
        self.assertNotIn("_id", r)

    def test_uses_the_pipeline_builder_and_never_loads_documents(self):
        self.load(SCENARIO_A)
        m.employee_monthly("EMP0001", "2026-07")
        self.assertEqual(self.fake.attendance_logs.calls, ["aggregate"])             # no find / find_one / count
        self.assertEqual(self.fake.attendance_logs.last_pipeline, m.employee_monthly_pipeline("EMP0001", 2026, 7))
        self.assertEqual(self.fake.employees.calls, ["find_one"])                    # one lookup, nothing else

    def test_unknown_employee_is_404_and_runs_no_aggregation(self):
        with self.assertRaises(HTTPException) as cm:
            m.employee_monthly("EMP9999", "2026-07")
        self.assertEqual((cm.exception.status_code, cm.exception.detail), (404, "employee not found"))
        self.assertEqual(self.fake.attendance_logs.calls, [])

    def test_emp_code_is_exact_and_case_sensitive(self):
        self.load(SCENARIO_A)
        for bad in ("emp0001", "Emp0001", "EMP0001 ", "EMP000"):
            with self.subTest(code=bad), self.assertRaises(HTTPException) as cm:
                m.employee_monthly(bad, "2026-07")
            self.assertEqual(cm.exception.status_code, 404)

    def test_invalid_months_are_422_before_any_lookup(self):
        for bad in ("2026-13", "2026-00", "2026-7", "202607", "2026-07-01", "", "0000-05", "garbage"):
            for code in ("EMP0001", "EMP9999"):                                       # unknown employee too
                with self.subTest(month=bad, code=code), self.assertRaises(RequestValidationError) as cm:
                    m.employee_monthly(code, bad)
                self.assertEqual(cm.exception.errors()[0]["loc"], ("query", "month"))
        self.assertEqual(self.fake.employees.calls, [])
        self.assertEqual(self.fake.attendance_logs.calls, [])

    def test_known_employee_with_no_records_is_a_valid_zero_summary(self):
        r = m.employee_monthly("EMP0001", "2026-07")
        self.assertEqual(r, dict(emp_code="EMP0001", month="2026-07", working_days=23, present_days=0.0, leave_days=0,
                                 late_count=0, total_late_minutes=0, total_overtime_minutes=0, attendance_pct=0.0))

    def test_employee_who_joins_after_the_month(self):
        self.add_emp("EMP0003", "2026-08-03")
        r = m.employee_monthly("EMP0003", "2026-07")
        self.assertEqual((r["working_days"], r["attendance_pct"], r["present_days"]), (0, None, 0.0))

    def test_mid_month_joiner_and_tie_rounding(self):
        self.add_emp("EMP0004", "2026-07-10")
        self.load([rec("EMP0004", "2026-07-13", "WFH", half=True)])
        r = m.employee_monthly("EMP0004", "2026-07")
        self.assertEqual((r["working_days"], r["present_days"], r["attendance_pct"]), (16, 0.5, 3.13))

    def test_pre_joining_records_are_included(self):                                    # approved C1
        self.add_emp("EMP0005", "2026-07-15")
        self.load([rec("EMP0005", "2026-07-13", late=10), rec("EMP0005", "2026-07-15")])
        r = m.employee_monthly("EMP0005", "2026-07")
        self.assertEqual((r["working_days"], r["present_days"], r["late_count"], r["attendance_pct"]), (13, 2.0, 1, 15.38))

    def test_other_months_and_employees_excluded(self):
        self.add_emp("EMP0002", JULY_EARLY)
        self.load(SCENARIO_A)
        self.assertEqual(m.employee_monthly("EMP0002", "2026-07")["total_late_minutes"], 99)
        self.assertEqual(m.employee_monthly("EMP0001", "2026-08")["total_late_minutes"], 77)
        self.assertEqual(m.employee_monthly("EMP0001", "2026-06")["total_late_minutes"], 5)

    def test_legacy_documents_through_the_endpoint(self):
        self.load([without(rec("EMP0001", "2026-07-01"), "half_day", "late_minutes", "overtime_minutes"),
                   rec("EMP0001", "2026-07-02", half=None, late=None, ot=None),
                   without(rec("EMP0001", "2026-07-03", "WFH", half=True, ot=7), "late_minutes"),
                   without(rec("EMP0001", "2026-07-06", late=15), "overtime_minutes")])
        r = m.employee_monthly("EMP0001", "2026-07")
        self.assertEqual((r["present_days"], r["late_count"], r["total_late_minutes"], r["total_overtime_minutes"]),
                         (3.5, 1, 15, 7))

    def test_february_2028_uses_the_leap_day(self):
        self.load([rec("EMP0001", "2028-02-29", late=3), rec("EMP0001", "2028-03-01", late=9)])
        r = m.employee_monthly("EMP0001", "2028-02")
        self.assertEqual((r["working_days"], r["total_late_minutes"], r["present_days"]), (21, 3, 1.0))   # Feb 29 2028 = Tue

    def test_a_database_error_is_not_swallowed(self):
        self.fake.attendance_logs.force_error = RuntimeError("boom")

        class Boom:
            def aggregate(self, pipeline):
                raise RuntimeError("boom")
        self.fake.attendance_logs = Boom()
        with self.assertRaises(RuntimeError):
            m.employee_monthly("EMP0001", "2026-07")


@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestMonthlyHttp(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)

    def get(self, code="EMP0001", qs="month=2026-07"):
        return self.c.get(f"/analytics/employees/{code}/monthly" + (f"?{qs}" if qs is not None else ""))

    def test_200_exact_schema_and_json_types(self):
        self.load(SCENARIO_A)
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), SCENARIO_A_EXPECTED)
        self.assertEqual(list(r.json()), MONTHLY_KEYS)
        self.assertNotIn("_id", r.text)
        self.assertNotIn('"id"', r.text)

    def test_422_missing_and_invalid_month(self):
        for qs in (None, "", "month=", "month=2026-13", "month=2026-00", "month=2026-7", "month=202607",
                   "month=2026-07-01", "month=0000-05", "month=abc", "other=2026-07"):
            with self.subTest(qs=qs):
                r = self.get(qs=qs)
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)

    def test_invalid_month_beats_unknown_employee(self):
        self.assertEqual(self.get("EMP9999", "month=2026-13").status_code, 422)
        self.assertEqual(self.get("EMP9999", "month=0000-05").status_code, 422)
        self.assertEqual(self.get("EMP9999", None).status_code, 422)

    def test_404_unknown_and_case_sensitive(self):
        self.assertEqual((self.get("EMP9999").status_code, self.get("EMP9999").json()), (404, {"detail": "employee not found"}))
        self.assertEqual(self.get("emp0001").status_code, 404)

    def test_null_pct_and_zero_pct_serialize_as_json(self):
        self.add_emp("EMP0003", "2026-08-03")
        self.assertIsNone(self.get("EMP0003").json()["attendance_pct"])
        body = self.get("EMP0001").json()                      # no records, 23 working days
        self.assertEqual((body["attendance_pct"], body["present_days"]), (0.0, 0.0))

    def test_openapi_lists_the_route_with_the_month_parameter(self):
        op = self.c.get("/openapi.json").json()["paths"]["/analytics/employees/{emp_code}/monthly"]["get"]
        self.assertIn("month", [p["name"] for p in op["parameters"]])
        self.assertTrue(next(p for p in op["parameters"] if p["name"] == "month")["required"])


# ======================================================================== 4. REAL MongoDB (opt-in)
@unittest.skipUnless(FULL and HAVE_HTTP and TEST_URI,
                     "set TEST_MONGO_URI to run the real-MongoDB integration tests: NOT EXECUTED")
class TestMongoMonthly(unittest.TestCase):
    """REAL MongoDB in a throwaway database (attendance_test_p51_<random>), dropped at the end."""
    PREFIX = "attendance_test_p51_"

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

    def add_emp(self, code, joined=JULY_EARLY):
        self.tdb.employees.insert_one({"emp_code": code, "name": "N", "email": "n@x.io", "department": "D",
                                       "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
                                       "created_at": datetime(2026, 1, 5, tzinfo=timezone.utc)})

    def seed(self, docs):
        if docs:
            self.tdb.attendance_logs.insert_many([dict(d) for d in docs])

    def test_scenario_a_matches_hand_computation_and_the_oracle(self):
        self.add_emp("EMP0001")
        self.add_emp("EMP0002")
        self.seed(SCENARIO_A)
        r = m.employee_monthly("EMP0001", "2026-07")
        self.assertEqual(r, SCENARIO_A_EXPECTED)
        self.assertEqual(r, oracle_monthly(SCENARIO_A, "EMP0001", JULY_EARLY, 2026, 7))
        for k in ("working_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"):
            self.assertIs(type(r[k]), int, k)
        self.assertIsInstance(r["present_days"], float)

    def test_status_weekend_halfday_and_open_record_rules(self):
        self.add_emp("EMP0001")
        docs = [rec("EMP0001", "2026-07-01", "PRESENT"), rec("EMP0001", "2026-07-02", "WFH", half=True),
                rec("EMP0001", "2026-07-03", "ON_DUTY"), rec("EMP0001", "2026-07-06", "ABSENT", late=3, ot=4),
                rec("EMP0001", "2026-07-07", "LEAVE"), rec("EMP0001", "2026-07-04", "PRESENT", late=20, ot=45),
                rec("EMP0001", "2026-07-05", "LEAVE")]
        self.seed(docs)
        self.assertEqual(m.employee_monthly("EMP0001", "2026-07"), oracle_monthly(docs, "EMP0001", JULY_EARLY, 2026, 7))
        r = m.employee_monthly("EMP0001", "2026-07")
        self.assertEqual((r["present_days"], r["leave_days"], r["late_count"], r["total_late_minutes"],
                          r["total_overtime_minutes"]), (2.5, 2, 2, 23, 49))

    def test_half_up_ties_against_real_mongo(self):
        self.add_emp("EMP0004", "2026-07-10")
        self.seed([rec("EMP0004", "2026-07-13", "WFH", half=True)])
        r = m.employee_monthly("EMP0004", "2026-07")
        self.assertEqual((r["working_days"], r["present_days"], r["attendance_pct"]), (16, 0.5, 3.13))
        self.seed([rec("EMP0004", "2026-07-14"), rec("EMP0004", "2026-07-15")])
        self.assertEqual(m.employee_monthly("EMP0004", "2026-07")["attendance_pct"], 15.63)

    def test_pre_joining_records_and_zero_working_days(self):
        self.add_emp("EMP0005", "2026-07-15")
        self.seed([rec("EMP0005", "2026-07-13", late=10), rec("EMP0005", "2026-07-15")])
        r = m.employee_monthly("EMP0005", "2026-07")
        self.assertEqual((r["working_days"], r["present_days"], r["late_count"], r["attendance_pct"]), (13, 2.0, 1, 15.38))
        self.add_emp("EMP0006", "2026-08-03")
        self.seed([rec("EMP0006", "2026-07-06")])
        r = m.employee_monthly("EMP0006", "2026-07")
        self.assertEqual((r["working_days"], r["present_days"], r["attendance_pct"]), (0, 1.0, None))

    def test_empty_month_is_a_zero_summary_not_404(self):
        self.add_emp("EMP0001")
        r = m.employee_monthly("EMP0001", "2026-09")
        self.assertEqual((r["working_days"], r["present_days"], r["late_count"], r["total_late_minutes"],
                          r["total_overtime_minutes"], r["leave_days"], r["attendance_pct"]), (22, 0.0, 0, 0, 0, 0, 0.0))
        self.assertEqual(list(self.tdb.attendance_logs.aggregate(m.employee_monthly_pipeline("EMP0001", 2026, 9))), [])

    def test_legacy_documents_with_missing_and_null_fields(self):
        self.add_emp("EMP0001")
        docs = [without(rec("EMP0001", "2026-07-01"), "half_day", "late_minutes", "overtime_minutes"),
                rec("EMP0001", "2026-07-02", half=None, late=None, ot=None),
                without(rec("EMP0001", "2026-07-03", "WFH", half=True, ot=7), "late_minutes"),
                without(rec("EMP0001", "2026-07-06", late=15), "overtime_minutes"),
                without(rec("EMP0001", "2026-07-07", "LEAVE"), "half_day", "late_minutes", "overtime_minutes", "history")]
        self.seed(docs)
        r = m.employee_monthly("EMP0001", "2026-07")
        self.assertEqual((r["present_days"], r["leave_days"], r["late_count"], r["total_late_minutes"],
                          r["total_overtime_minutes"], r["attendance_pct"]), (3.5, 1, 1, 15, 7, 15.22))

    def test_matches_the_oracle_on_random_data_across_months(self):
        rng = random.Random(51)
        statuses = ["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
        docs, joined = [], {"A": "2026-01-05", "B": "2026-07-10", "C": "2026-08-03", "D": "2026-06-30", "E": "2026-07-04"}
        for emp, j in joined.items():
            self.add_emp(emp, j)
            for month in (6, 7, 8):
                for day in range(1, calendar.monthrange(2026, month)[1] + 1):
                    if rng.random() < 0.85:
                        d = rec(emp, f"2026-{month:02d}-{day:02d}", rng.choice(statuses), late=rng.choice([0, 0, 5, 10, 11, 45]),
                                ot=rng.choice([0, 0, 30, 45]), half=rng.random() < 0.15)
                        for k in ("half_day", "late_minutes", "overtime_minutes"):
                            roll = rng.random()
                            if roll < 0.1:
                                d.pop(k)
                            elif roll < 0.2:
                                d[k] = None
                        docs.append(d)
        self.seed(docs)
        for emp, j in joined.items():
            for month in (6, 7, 8, 9):
                with self.subTest(emp=emp, month=month):
                    self.assertEqual(m.employee_monthly(emp, f"2026-{month:02d}"), oracle_monthly(docs, emp, j, 2026, month))

    def test_other_employees_other_months_and_first_last_day(self):
        self.add_emp("EMP0001")
        self.add_emp("EMP0010")
        self.seed([rec("EMP0001", "2026-06-30", late=1), rec("EMP0001", "2026-07-01", late=2),
                   rec("EMP0001", "2026-07-31", late=4), rec("EMP0001", "2026-08-01", late=8),
                   rec("EMP0010", "2026-07-15", late=16)])
        r = m.employee_monthly("EMP0001", "2026-07")
        self.assertEqual((r["total_late_minutes"], r["late_count"]), (6, 2))

    def test_explain_of_the_real_pipeline_uses_ixscan_and_no_collscan(self):
        docs = []
        for e in range(1, 201):                                         # 200 employees x 100 days = 20,000 records
            for d in range(100):
                day = datetime(2026, 4, 1) + (datetime(2026, 4, 2) - datetime(2026, 4, 1)) * d
                docs.append(rec(f"EMP{e:04d}", day.strftime("%Y-%m-%d"), "PRESENT", late=d % 12, ot=d % 50))
        self.tdb.attendance_logs.insert_many(docs)
        pipeline = m.employee_monthly_pipeline("EMP0107", 2026, 5)
        out = self.tdb.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                                "verbosity": "executionStats"})
        text = json.dumps(out, default=str)
        self.assertIn("IXSCAN", text)
        self.assertNotIn("COLLSCAN", text)
        row = list(self.tdb.attendance_logs.aggregate(pipeline))[0]
        self.assertEqual(row["total_late_minutes"], sum((d % 12) for d in range(100) if (datetime(2026, 4, 1).toordinal() + d) and
                                                         (datetime(2026, 4, 1) + (datetime(2026, 4, 2) - datetime(2026, 4, 1)) * d).month == 5))

    def test_http_flow_with_real_mongo(self):
        from fastapi.testclient import TestClient
        self.add_emp("EMP0001")
        self.seed(SCENARIO_A)
        c = TestClient(m.app)
        r = c.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        self.assertEqual((r.status_code, r.json()), (200, SCENARIO_A_EXPECTED))
        self.assertEqual(c.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code, 404)
        self.assertEqual(c.get("/analytics/employees/EMP0001/monthly?month=0000-05").status_code, 422)
        self.assertEqual(c.get("/analytics/employees/emp0001/monthly?month=2026-07").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
