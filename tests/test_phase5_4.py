"""Phase 5.4 tests: GET /analytics/departments/{department}/trend?from=YYYY-MM-DD&to=YYYY-MM-DD
and GET /admin/explain/department_trend

Layers (each says what it proves):
  1. TestRangeValidation / TestPipelineShape / TestBuildResponse
                          pure; no server, no emulator.
  2. TestPipelineSemanticsEmulated
                          the REAL pipeline object run by tests/_agg_emulator_p54.py - an EMULATION of the MongoDB
                          operators it uses (NOT MongoDB) - compared with an independent plain-Python oracle that uses
                          exact Fractions. Hand-computed expectations are asserted as well.
  3. TestTrendHttp / TestExplainHttp
                          the endpoints on an in-memory fake database whose aggregate() uses the emulator. MOCKED:
                          proves routing, 422/404 precedence, response shape, "one find_one + one aggregate, nothing
                          loaded into Python", and that explain reuses the endpoint's builder.
  4. TestMongoTrend       REAL MongoDB, opt-in via TEST_MONGO_URI; disposable database attendance_test_p54_<random>,
                          dropped afterwards; never touches MONGO_DB. Date spine, boundaries, aggregation semantics, the
                          oracle comparison and the strict EXPLAIN checks on 100,000 attendance records.
                          If TEST_MONGO_URI is not set these tests are SKIPPED - a skip is NOT a pass.
Self-contained: needs only app/main.py, tests/_agg_emulator_p54.py and tests/_explain_util.py (and fastapi/httpx for
layers 3-4). Run:  python -m unittest tests.test_phase5_4 -v"""
import calendar
import json
import os
import random
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

from app import main as m
from tests._agg_emulator_p54 import match_docs, mini_aggregate
from tests._explain_util import describe, summarize

try:
    import httpx  # noqa: F401
    HAVE_HTTP = True
except ImportError:                                                   # pragma: no cover
    HAVE_HTTP = False
TEST_URI = os.environ.get("TEST_MONGO_URI")

ITEM_KEYS = ["date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"]
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


# ------------------------------------------------------------------ data helpers
def emp(code, dept="Eng", joined="2026-01-05", **extra):
    d = {"emp_code": code, "department": dept, "name": f"Name {code}", "email": "n@x.io", "joined_on": joined,
         "shift_start": "09:30", "shift_end": "18:30"}
    d.update(extra)
    return d


def lg(code, day, status="PRESENT", late=0, half=False, **extra):
    d = {"emp_code": code, "date": day, "status": status, "late_minutes": late, "half_day": half,
         "work_hours": None, "overtime_minutes": 0, "history": []}
    d.update(extra)
    return d


def drop(doc, key):
    doc = dict(doc)
    del doc[key]
    return doc


def codes(n, prefix="E"):
    return [f"{prefix}{i:03d}" for i in range(1, n + 1)]


def day_range(first, last):
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


# ------------------------------------------------------------------ the independent oracle
def half_up(fr: Fraction) -> int:
    """floor(x + 1/2) for a non-negative Fraction: round-half-UP, never banker's rounding."""
    return (fr.numerator * 2 + fr.denominator) // (2 * fr.denominator)


def oracle(emps, logs, department, date_from, date_to):
    """Plain loops and exact Fractions; shares no code with the pipeline."""
    in_dept = [e for e in emps if isinstance(e.get("department"), str) and e["department"] == department]
    eligible = {e["emp_code"]: e for e in in_dept if e["joined_on"] <= date_to}
    days = day_range(date_from, date_to)
    rows, rates_t = [], []
    for d in days:
        hc = sum(1 for e in eligible.values() if e["joined_on"] <= d)
        present, late = Fraction(0), 0
        for l in logs:
            if l["date"] != d or l["emp_code"] not in eligible:          # orphans and other departments are out
                continue
            if l["status"] in PRESENCE:
                present += Fraction(1, 2) if l.get("half_day") is True else 1
            v = l.get("late_minutes")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                late += 1
        working = date.fromisoformat(d).weekday() < 5
        t = half_up(present / hc * 10000) if working and hc > 0 else None
        rates_t.append(t)
        rows.append([d, working, hc, present, late, t])
    out = []
    for i, (d, working, hc, present, late, t) in enumerate(rows):
        window = [x for x in rates_t[max(0, i - 6): i + 1] if x is not None]
        avg = half_up(Fraction(sum(window), len(window))) if window else None
        out.append({"date": d, "is_working_day": working, "headcount": hc, "present_count": float(present),
                    "late_count": late, "attendance_rate": None if t is None else t / 10000,
                    "moving_avg_7d": None if avg is None else avg / 10000})
    return {"department": department, "items": out}


def run(emps, logs, department, date_from, date_to):
    """The real pipeline on the emulator -> the real response builder."""
    rows = mini_aggregate(emps, m.department_trend_pipeline(date_from, date_to, department),
                          {"attendance_logs": logs})
    return m.build_department_trend(department, rows)


def rates(body):
    return [i["attendance_rate"] for i in body["items"]]


def avgs(body):
    return [i["moving_avg_7d"] for i in body["items"]]


def random_world(rng, n_emps=60, per=12, start="2026-05-20", span=70):
    depts = ["Eng", "eng", "Sales", "", "Ops"]
    base = date.fromisoformat(start)
    emps, logs = [], []
    for i in range(n_emps):
        dept = rng.choice(depts)
        if i % 17 == 0:
            dept = rng.choice([None, 5, ["Eng"]])
        joined = (base + timedelta(days=rng.randint(-60, span + 20))).isoformat()
        e = emp(f"E{i:03d}", dept, joined)
        emps.append(e)
        for day in rng.sample(range(-5, span + 5), per):
            d = lg(e["emp_code"], (base + timedelta(days=day)).isoformat(),
                   status=rng.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE", "PRESENT"]),
                   late=rng.choice([0, 0, 1, 12, 45, -5, None, "x"]), half=rng.choice([False, True, None, "yes"]))
            if rng.random() < 0.1:
                d = drop(d, "late_minutes")
            if rng.random() < 0.1:
                d = drop(d, "half_day")
            logs.append(d)
    for k in range(15):                                                       # orphans
        logs.append(lg(f"GHOST{k}", (base + timedelta(days=rng.randint(0, span))).isoformat()))
    return emps, logs


# ======================================================================== 1. pure
class TestRangeValidation(unittest.TestCase):
    def test_counts_inclusive_dates(self):
        self.assertEqual(m.validate_trend_range("2026-07-01", "2026-07-01"), 1)
        self.assertEqual(m.validate_trend_range("2026-07-01", "2026-07-02"), 2)
        self.assertEqual(m.validate_trend_range("2028-02-28", "2028-03-01"), 3)       # leap day in between

    def test_exactly_92_dates_allowed_and_93_rejected(self):
        self.assertEqual(m.validate_trend_range("2026-07-01", "2026-09-30"), 92)     # Jul 31 + Aug 31 + Sep 30
        with self.assertRaises(ValueError):
            m.validate_trend_range("2026-07-01", "2026-10-01")                       # 93 dates

    def test_reversed_range_rejected(self):
        with self.assertRaises(ValueError):
            m.validate_trend_range("2026-07-02", "2026-07-01")

    def test_max_is_92(self):
        self.assertEqual(m.MAX_TREND_DAYS, 92)


class TestPipelineShape(unittest.TestCase):
    def pipe(self, f="2026-07-01", t="2026-07-31", dept="Eng"):
        return m.department_trend_pipeline(f, t, dept)

    def names(self, p):
        return [next(iter(s)) for s in p]

    def test_stage_order(self):
        self.assertEqual(self.names(self.pipe()), [
            "$match", "$project", "$lookup", "$facet", "$project", "$unwind", "$replaceRoot", "$addFields",
            "$addFields", "$setWindowFields", "$sort", "$project"])

    def test_first_stage_is_exact_department_plus_joined_on_cutoff_and_string_type_check(self):
        match = self.pipe()[0]["$match"]
        self.assertEqual(match["department"], "Eng")                      # plain equality: the (department, emp_code) index
        self.assertEqual(match["joined_on"], {"$lte": "2026-07-31"})
        self.assertEqual(match["$expr"], {"$eq": [{"$type": "$department"}, "string"]})

    def test_lookup_uses_the_unique_emp_code_date_index_shape(self):
        lk = self.pipe()[2]["$lookup"]
        self.assertEqual((lk["from"], lk["localField"], lk["foreignField"]), ("attendance_logs", "emp_code", "emp_code"))
        self.assertEqual(lk["pipeline"][0], {"$match": {"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}}})

    def test_spine_is_built_in_the_database_from_a_bson_utc_date(self):
        span = self.pipe("2026-07-01", "2026-07-31")[4]["$project"]["rows"]["$map"]
        self.assertEqual(span["input"], {"$range": [0, 31]})
        start = span["in"]["$let"]["vars"]["d"]["$dateAdd"]["startDate"]
        self.assertEqual(start, datetime(2026, 7, 1, tzinfo=timezone.utc))
        self.assertEqual(span["in"]["$let"]["vars"]["d"]["$dateAdd"]["unit"], "day")

    def test_window_is_the_current_row_and_six_preceding_rows(self):
        sw = self.pipe()[9]["$setWindowFields"]
        self.assertEqual(sw["sortBy"], {"date": 1})
        self.assertEqual(sw["output"]["win_s"]["window"], {"documents": [-6, 0]})
        self.assertEqual(sw["output"]["win_n"]["window"], {"documents": [-6, 0]})
        self.assertNotIn("partitionBy", sw)

    def test_no_round_operator_and_no_python_side_work_in_the_pipeline(self):
        text = json.dumps(self.pipe(), default=str)
        self.assertNotIn("$round", text)                                   # MongoDB $round is half-to-even
        self.assertNotIn("$where", text)
        self.assertNotIn("$function", text)

    def test_every_employee_unwind_is_absent_so_no_employee_day_pairs(self):
        p = self.pipe()
        self.assertNotIn("$unwind", self.names(p)[:4])                     # the only $unwinds run on per-record / per-day rows
        facet_days = p[3]["$facet"]["days"]
        self.assertEqual(facet_days[0], {"$unwind": "$logs"})

    def test_builder_validates_the_range_itself(self):
        with self.assertRaises(ValueError):
            self.pipe("2026-07-02", "2026-07-01")
        with self.assertRaises(ValueError):
            self.pipe("2026-07-01", "2026-10-01")

    def test_pipeline_is_deterministic_and_embeds_the_requested_values(self):
        self.assertEqual(self.pipe(), self.pipe())
        self.assertEqual(self.pipe(dept="Sales")[0]["$match"]["department"], "Sales")
        self.assertEqual(self.pipe(dept="")[0]["$match"]["department"], "")

    def test_existing_index_definitions_are_unchanged(self):
        self.assertEqual(m.INDEXES, {
            "employees": [([("emp_code", 1)], {"unique": True}), ([("department", 1), ("emp_code", 1)], {})],
            "attendance_logs": [([("emp_code", 1), ("date", 1)], {"unique": True}), ([("date", -1), ("emp_code", 1)], {}),
                                ([("status", 1), ("date", -1), ("emp_code", 1)], {}),
                                ([("emp_code", 1), ("punch_in", -1)], {})]})


class TestBuildResponse(unittest.TestCase):
    def test_exact_keys_order_and_types(self):
        row = {"_id": "x", "date": "2026-07-06", "is_working_day": True, "headcount": 10, "present_count": 8.5,
               "late_count": 2, "attendance_rate": 0.85, "moving_avg_7d": None, "extra": 1}
        body = m.build_department_trend("Eng", [row])
        self.assertEqual(list(body), ["department", "items"])
        self.assertEqual(list(body["items"][0]), ITEM_KEYS)
        self.assertEqual(body["items"][0], {"date": "2026-07-06", "is_working_day": True, "headcount": 10,
                                            "present_count": 8.5, "late_count": 2, "attendance_rate": 0.85,
                                            "moving_avg_7d": None})
        self.assertEqual(m.build_department_trend("Eng", []), {"department": "Eng", "items": []})


# ======================================================================== 2. emulated semantics
JUL = "2026-07"


def week_world():
    """10 employees; Mon 2026-07-06 .. Tue 2026-07-14: presence 10, 9, 8, 7, 6 on Mon-Fri, 2 on Saturday, none Sunday,
    10 on the next Monday, none on the next Tuesday."""
    emps = [emp(c) for c in codes(10)]
    logs = []
    per_day = {"2026-07-06": 10, "2026-07-07": 9, "2026-07-08": 8, "2026-07-09": 7, "2026-07-10": 6,
               "2026-07-11": 2, "2026-07-13": 10}
    for d, n in per_day.items():
        logs += [lg(c, d) for c in codes(10)[:n]]
    return emps, logs


class TestPipelineSemanticsEmulated(unittest.TestCase):
    def check(self, emps, logs, dept, f, t):
        got = run(emps, logs, dept, f, t)
        self.assertEqual(got, oracle(emps, logs, dept, f, t))
        return got

    # ---- date spine
    def test_one_row_per_calendar_day_inclusive_including_weekends_and_empty_days(self):
        body = self.check([emp("E1")], [], "Eng", "2026-07-03", "2026-07-13")
        self.assertEqual([i["date"] for i in body["items"]], day_range("2026-07-03", "2026-07-13"))

    def test_one_day_range_and_inclusive_endpoints(self):
        emps, logs = week_world()
        one = self.check(emps, logs, "Eng", "2026-07-08", "2026-07-08")
        self.assertEqual([i["date"] for i in one["items"]], ["2026-07-08"])
        self.assertEqual(one["items"][0]["headcount"], 10)
        self.assertEqual(one["items"][0]["present_count"], 8.0)
        self.assertEqual(one["items"][0]["attendance_rate"], 0.8)
        self.assertEqual(one["items"][0]["moving_avg_7d"], 0.8)                # a window of one row

    def test_exactly_92_days_and_boundaries(self):
        body = self.check([emp("E1")], [lg("E1", "2026-07-01"), lg("E1", "2026-09-30")], "Eng", "2026-07-01", "2026-09-30")
        self.assertEqual(len(body["items"]), 92)
        self.assertEqual((body["items"][0]["date"], body["items"][-1]["date"]), ("2026-07-01", "2026-09-30"))
        self.assertEqual((body["items"][0]["present_count"], body["items"][-1]["present_count"]), (1.0, 1.0))

    def test_month_and_year_boundaries_and_leap_day(self):
        for f, t, n in (("2026-12-28", "2027-01-04", 8), ("2028-02-27", "2028-03-02", 5), ("2026-02-26", "2026-03-02", 5),
                        ("2027-12-31", "2027-12-31", 1), ("2028-12-30", "2029-01-02", 4)):
            with self.subTest(f=f, t=t):
                body = self.check([emp("E1", joined="2020-01-01")], [lg("E1", f), lg("E1", t)], "Eng", f, t)
                self.assertEqual(len(body["items"]), n)
        leap = self.check([emp("E1")], [], "Eng", "2028-02-28", "2028-03-01")
        self.assertEqual([i["date"] for i in leap["items"]], ["2028-02-28", "2028-02-29", "2028-03-01"])
        nonleap = self.check([emp("E1")], [], "Eng", "2026-02-27", "2026-03-01")
        self.assertEqual([i["date"] for i in nonleap["items"]], ["2026-02-27", "2026-02-28", "2026-03-01"])

    def test_weekend_working_day_flags(self):
        body = self.check([emp("E1")], [], "Eng", "2026-07-06", "2026-07-12")            # Monday .. Sunday
        self.assertEqual([i["is_working_day"] for i in body["items"]], [True] * 5 + [False] * 2)

    # ---- rates
    def test_hand_computed_week_rates_and_moving_averages(self):
        emps, logs = week_world()
        body = self.check(emps, logs, "Eng", "2026-07-06", "2026-07-14")
        self.assertEqual(rates(body), [1.0, 0.9, 0.8, 0.7, 0.6, None, None, 1.0, 0.0])
        self.assertEqual(avgs(body), [1.0, 0.95, 0.9, 0.85, 0.8, 0.8, 0.8, 0.8, 0.62])
        self.assertEqual([i["present_count"] for i in body["items"]], [10.0, 9.0, 8.0, 7.0, 6.0, 2.0, 0.0, 10.0, 0.0])

    def test_warm_up_window_lengths_1_to_6(self):
        """Mon..Fri carry rates; a constant-increasing series makes every window length visible."""
        emps = [emp(c) for c in codes(10)]
        logs = []
        for d, n in (("2026-07-06", 1), ("2026-07-07", 2), ("2026-07-08", 3), ("2026-07-09", 4), ("2026-07-10", 5)):
            logs += [lg(c, d) for c in codes(10)[:n]]
        body = self.check(emps, logs, "Eng", "2026-07-06", "2026-07-12")
        self.assertEqual(rates(body), [0.1, 0.2, 0.3, 0.4, 0.5, None, None])
        self.assertEqual(avgs(body), [0.1, 0.15, 0.2, 0.25, 0.3, 0.3, 0.3])          # windows of 1,2,3,4,5,5,5 non-null values
        self.assertEqual(body["items"][5]["moving_avg_7d"], 0.3)                       # Saturday: a window of 6 rows, 5 non-null

    def test_seven_calendar_day_window_not_seven_working_days(self):
        emps, logs = week_world()
        body = self.check(emps, logs, "Eng", "2026-07-06", "2026-07-14")
        # Mon 13th: rows Tue..Mon are .9 .8 .7 .6 null null 1.0 -> 5 values, mean .8 (a 7-WORKING-day window would differ)
        self.assertEqual(body["items"][7]["moving_avg_7d"], 0.8)
        # Tue 14th: rows Wed..Tue are .8 .7 .6 null null 1.0 0.0 -> 5 values, mean .62
        self.assertEqual(body["items"][8]["moving_avg_7d"], 0.62)

    def test_rows_before_from_are_not_considered(self):
        emps, logs = week_world()
        body = self.check(emps, logs, "Eng", "2026-07-08", "2026-07-10")
        self.assertEqual(avgs(body), [0.8, 0.75, 0.7])                                # Monday/Tuesday rates are outside

    def test_null_rates_are_skipped_and_all_null_window_is_null(self):
        weekend = self.check([emp("E1")], [lg("E1", "2026-07-11")], "Eng", "2026-07-11", "2026-07-12")
        self.assertEqual((rates(weekend), avgs(weekend)), ([None, None], [None, None]))
        # zero headcount for the first 9 days (everyone joins 2026-07-10) -> an all-null window of 7+ rows
        late_joiners = [emp(c, joined="2026-07-10") for c in codes(4)]
        body = self.check(late_joiners, [lg("E001", "2026-07-10")], "Eng", "2026-07-01", "2026-07-14")
        self.assertEqual(avgs(body)[:9], [None] * 9)
        self.assertEqual(rates(body)[:9], [None] * 9)
        self.assertEqual(rates(body)[9], 0.25)                                         # Fri 10th: 1 of 4
        self.assertEqual(avgs(body)[9], 0.25)

    def test_working_day_with_no_records_is_zero_not_null(self):
        body = self.check([emp("E1")], [], "Eng", "2026-07-06", "2026-07-07")
        self.assertEqual(rates(body), [0.0, 0.0])
        self.assertEqual(avgs(body), [0.0, 0.0])

    def test_zero_headcount_null_rate_and_weekend_null_rate(self):
        body = self.check([emp("E1", joined="2026-08-01")], [lg("E1", "2026-07-06")], "Eng", "2026-07-06", "2026-07-07")
        self.assertEqual([(i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in body["items"]],
                         [(0, None, None), (0, None, None)])

    def test_department_whose_employees_all_join_after_to_yields_zero_headcount_rows(self):
        emps = [emp("E1", joined="2026-09-01"), emp("E2", joined="2026-09-15")]
        body = self.check(emps, [lg("E1", "2026-07-06")], "Eng", "2026-07-06", "2026-07-08")
        self.assertEqual(body["items"], [
            {"date": d, "is_working_day": True, "headcount": 0, "present_count": 0.0, "late_count": 0,
             "attendance_rate": None, "moving_avg_7d": None} for d in day_range("2026-07-06", "2026-07-08")])

    def test_employees_joining_during_the_range_raise_the_headcount_from_their_day(self):
        emps = [emp("E1"), emp("E2", joined="2026-07-08"), emp("E3", joined="2026-07-08"), emp("E4", joined="2026-07-10")]
        body = self.check(emps, [], "Eng", "2026-07-06", "2026-07-12")
        self.assertEqual([i["headcount"] for i in body["items"]], [1, 1, 3, 3, 4, 4, 4])
        body = self.check(emps, [], "Eng", "2026-07-08", "2026-07-08")
        self.assertEqual(body["items"][0]["headcount"], 3)                              # joined_on <= day, inclusive

    # ---- counting rules
    def test_presence_statuses_half_days_and_absence_statuses(self):
        emps = [emp(c) for c in codes(6)]
        logs = [lg("E001", "2026-07-06", "PRESENT"), lg("E002", "2026-07-06", "WFH"), lg("E003", "2026-07-06", "ON_DUTY"),
                lg("E004", "2026-07-06", "ABSENT"), lg("E005", "2026-07-06", "LEAVE"),
                lg("E006", "2026-07-06", "PRESENT", half=True)]
        body = self.check(emps, logs, "Eng", "2026-07-06", "2026-07-06")
        self.assertEqual(body["items"][0]["present_count"], 3.5)
        self.assertEqual(body["items"][0]["attendance_rate"], 0.5833)                  # 3.5 / 6 = 0.58333..
        half = self.check([emp("E1"), emp("E2")], [lg("E1", "2026-07-06", "WFH", half=True),
                                                    lg("E2", "2026-07-06", "ON_DUTY", half=True)], "Eng", "2026-07-06", "2026-07-06")
        self.assertEqual(half["items"][0]["present_count"], 1.0)
        for status in ("ABSENT", "LEAVE"):                                            # half_day on an absence adds nothing
            r = self.check([emp("E1")], [lg("E1", "2026-07-06", status, half=True)], "Eng", "2026-07-06", "2026-07-06")
            self.assertEqual((r["items"][0]["present_count"], r["items"][0]["attendance_rate"]), (0.0, 0.0))

    def test_weekend_attendance_and_late_records_count(self):
        emps = [emp("E1"), emp("E2")]
        logs = [lg("E1", "2026-07-11", late=20), lg("E2", "2026-07-11", "WFH", half=True, late=5),
                lg("E1", "2026-07-12", "LEAVE", late=3)]
        body = self.check(emps, logs, "Eng", "2026-07-11", "2026-07-12")
        self.assertEqual([(i["present_count"], i["late_count"], i["attendance_rate"]) for i in body["items"]],
                         [(1.5, 2, None), (0.0, 1, None)])                              # a late LEAVE record still has late_minutes > 0

    def test_missing_and_null_half_day_and_late_minutes_default(self):
        logs = [drop(lg("E001", "2026-07-06"), "half_day"), lg("E002", "2026-07-06", half=None),
                drop(lg("E003", "2026-07-06"), "late_minutes"), lg("E004", "2026-07-06", late=None),
                lg("E005", "2026-07-06", late=0), lg("E006", "2026-07-06", late=-5), lg("E007", "2026-07-06", late=1)]
        body = self.check([emp(c) for c in codes(7)], logs, "Eng", "2026-07-06", "2026-07-06")
        self.assertEqual((body["items"][0]["present_count"], body["items"][0]["late_count"]), (7.0, 1))

    def test_stored_late_minutes_is_used_not_recomputed(self):
        body = self.check([emp("E1", shift_start="00:00")], [lg("E1", "2026-07-06", late=0, punch_in=datetime(2026, 7, 6, 12, tzinfo=timezone.utc))],
                          "Eng", "2026-07-06", "2026-07-06")
        self.assertEqual(body["items"][0]["late_count"], 0)

    def test_orphan_logs_other_departments_and_case_variants_are_excluded(self):
        emps = [emp("E1", "Sales"), emp("E2", "sales"), emp("E3", "Sales "), emp("E4", "Eng")]
        logs = [lg("E1", "2026-07-06"), lg("E2", "2026-07-06"), lg("E3", "2026-07-06"), lg("E4", "2026-07-06"),
                lg("GHOST", "2026-07-06", late=9)]
        sales = self.check(emps, logs, "Sales", "2026-07-06", "2026-07-06")
        self.assertEqual((sales["items"][0]["headcount"], sales["items"][0]["present_count"], sales["items"][0]["late_count"]), (1, 1.0, 0))
        for dept in ("SALES", "sales", "Sales ", "Eng", "eng", "", "Nope"):
            self.check(emps, logs, dept, "2026-07-06", "2026-07-06")
        self.assertEqual(run(emps, logs, "SALES", "2026-07-06", "2026-07-06")["items"][0]["headcount"], 0)

    def test_department_field_types_other_than_string_never_match(self):
        emps = [emp("E1", ["Eng"]), emp("E2", ["Sales", "Eng"]), emp("E3", 5), emp("E4", None), drop(emp("E5"), "department"),
                emp("E6", "Eng"), emp("E7", "")]
        logs = [lg(e["emp_code"], "2026-07-06", late=3) for e in emps]
        body = self.check(emps, logs, "Eng", "2026-07-06", "2026-07-06")
        self.assertEqual((body["items"][0]["headcount"], body["items"][0]["present_count"]), (1, 1.0))
        empty = self.check(emps, logs, "", "2026-07-06", "2026-07-06")
        self.assertEqual(empty["items"][0]["headcount"], 1)                            # an explicit "" matches exactly "" only

    def test_records_before_joined_on_still_count_for_eligible_employees(self):
        """DECISIONS.md 4: the contract's present_count has no joined_on filter, so such a record counts."""
        emps = [emp("E1", joined="2026-07-08"), emp("E2")]
        logs = [lg("E1", "2026-07-06"), lg("E2", "2026-07-06")]
        body = self.check(emps, logs, "Eng", "2026-07-06", "2026-07-08")
        first = body["items"][0]
        self.assertEqual((first["headcount"], first["present_count"], first["attendance_rate"]), (1, 2.0, 2.0))   # rate may exceed 1
        # ... but an employee who joins AFTER `to` is excluded entirely (their early record is not counted)
        gone = self.check([emp("E1", joined="2026-07-09"), emp("E2")], [lg("E1", "2026-07-06"), lg("E2", "2026-07-06")],
                          "Eng", "2026-07-06", "2026-07-08")
        self.assertEqual(gone["items"][0]["present_count"], 1.0)

    def test_logs_outside_the_range_are_ignored(self):
        logs = [lg("E1", d) for d in ("2026-07-05", "2026-07-09", "2026-06-30")] + [lg("E1", "2026-07-07")]
        body = self.check([emp("E1")], logs, "Eng", "2026-07-06", "2026-07-08")
        self.assertEqual([i["present_count"] for i in body["items"]], [0.0, 1.0, 0.0])

    # ---- rounding
    def test_attendance_rate_rounds_half_up_not_half_even(self):
        """16 employees, one half-day record: 0.5 / 16 = 0.03125 exactly -> 0.0313 half-up (half-even would give 0.0312)."""
        emps = [emp(c) for c in codes(16)]
        body = self.check(emps, [lg("E001", "2026-07-06", half=True)], "Eng", "2026-07-06", "2026-07-06")
        self.assertEqual(body["items"][0]["attendance_rate"], 0.0313)
        # 1/3 and 2/3 are not ties: 0.3333 and 0.6667
        three = self.check([emp(c) for c in codes(3)], [lg("E001", "2026-07-06"), lg("E001", "2026-07-07"), lg("E002", "2026-07-07")],
                           "Eng", "2026-07-06", "2026-07-07")
        self.assertEqual(rates(three), [0.3333, 0.6667])

    def test_moving_average_rounds_half_up_over_the_rounded_daily_rates(self):
        """Mon 0.0313 (rounded from 0.03125) and Tue 0.0: mean of 313 and 0 ten-thousandths = 156.5 -> 157 (half-even: 156)."""
        emps = [emp(c) for c in codes(16)]
        body = self.check(emps, [lg("E001", "2026-07-06", half=True)], "Eng", "2026-07-06", "2026-07-07")
        self.assertEqual(rates(body), [0.0313, 0.0])
        self.assertEqual(avgs(body), [0.0313, 0.0157])
        # averaging the ROUNDED rates (assumption 3): the raw-ratio mean would be 0.015625 -> 0.0156
        self.assertNotEqual(avgs(body)[1], 0.0156)

    def test_rates_and_averages_never_overflow_into_float_noise(self):
        emps = [emp(c) for c in codes(7)]
        logs = [lg(c, "2026-07-06") for c in codes(7)[:3]]
        body = self.check(emps, logs, "Eng", "2026-07-06", "2026-07-06")
        self.assertEqual(body["items"][0]["attendance_rate"], 0.4286)
        self.assertEqual(repr(body["items"][0]["attendance_rate"]), "0.4286")

    # ---- randomised comparison with the independent oracle
    def test_random_worlds_match_the_oracle(self):
        for seed in range(25):
            rng = random.Random(seed)
            emps, logs = random_world(rng)
            first = date(2026, 5, 20) + timedelta(days=rng.randint(-3, 10))
            span = rng.choice([1, 2, 7, 8, 20, 45, 70])
            f, t = first.isoformat(), (first + timedelta(days=span - 1)).isoformat()
            for dept in ("Eng", "eng", "Sales", "", "Ops", "Nope"):
                with self.subTest(seed=seed, dept=dept, f=f, t=t):
                    self.check(emps, logs, dept, f, t)


# ======================================================================== 3. HTTP over a fake database
class FakeCollection:
    def __init__(self, docs=None):
        self.docs, self.calls, self.last_pipeline = list(docs or []), [], None
        self.resolver = None

    def find_one(self, flt=None, projection=None):
        self.calls.append("find_one")
        hit = match_docs([dict(d) for d in self.docs], flt or {})
        return hit[0] if hit else None

    def find(self, *a, **k):
        self.calls.append("find")
        raise AssertionError("the trend must not load documents with find()")

    def aggregate(self, pipeline):
        self.calls.append("aggregate")
        self.last_pipeline = pipeline
        return iter(mini_aggregate(self.docs, pipeline, {"attendance_logs": self.resolver()}))


class FakeDB:
    def __init__(self):
        self.employees, self.attendance_logs, self.commands = FakeCollection(), FakeCollection(), []
        self.employees.resolver = lambda: self.attendance_logs.docs

    def command(self, cmd):
        self.commands.append(cmd)
        return {"queryPlanner": {"winningPlan": {"stage": "FAKE"}}, "executionStats": {"nReturned": 0}}


class FakeDbCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDB()
        self._orig = m.db
        m.db = self.fake

    def tearDown(self):
        m.db = self._orig

    def load(self, emps, logs):
        self.fake.employees.docs = [dict(e) for e in emps]
        self.fake.attendance_logs.docs = [dict(l) for l in logs]


@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestTrendHttp(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)

    def get(self, dept="Eng", qs="from=2026-07-06&to=2026-07-14"):
        return self.c.get(f"/analytics/departments/{dept}/trend" + (f"?{qs}" if qs is not None else ""))

    def test_200_envelope_item_schema_and_no_id(self):
        self.load(*week_world())
        r = self.get()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(list(body), ["department", "items"])
        self.assertEqual(body["department"], "Eng")
        self.assertEqual(len(body["items"]), 9)
        self.assertTrue(all(list(i) == ITEM_KEYS for i in body["items"]))
        self.assertNotIn("_id", r.text)
        self.assertEqual(body["items"][0], {"date": "2026-07-06", "is_working_day": True, "headcount": 10,
                                            "present_count": 10.0, "late_count": 0, "attendance_rate": 1.0,
                                            "moving_avg_7d": 1.0})
        self.assertIsNone(body["items"][5]["attendance_rate"])                   # Saturday
        self.assertEqual(body["items"][5]["present_count"], 2.0)

    def test_matches_the_oracle_over_http(self):
        emps, logs = week_world()
        self.load(emps, logs)
        self.assertEqual(self.get().json(), oracle(emps, logs, "Eng", "2026-07-06", "2026-07-14"))

    def test_query_parameters_are_required_and_validated(self):
        self.load(*week_world())
        bad = (None, "", "from=2026-07-06", "to=2026-07-14", "from=&to=2026-07-14", "from=2026-07-06&to=",
               "from=2026-7-6&to=2026-07-14", "from=20260706&to=20260714", "from=2026-07-06T00:00:00&to=2026-07-14",
               "from=2026-02-30&to=2026-03-02", "from=2026-07-06&to=2026-02-30", "from=2025-02-29&to=2025-03-02",
               "from=2026-13-01&to=2026-13-02", "from=2026-00-10&to=2026-07-14", "from=2026-07-00&to=2026-07-14",
               "from=abc&to=def", "from=2026-07-06&to=2026-07-14x", "from=%202026-07-06&to=2026-07-14",
               "from=2026-07-06&to=2026-07-14&to=bad", "date_from=2026-07-06&date_to=2026-07-14",
               "from=٢٠٢٦-٠٧-٠٦&to=2026-07-14")
        for qs in bad:
            with self.subTest(qs=qs):
                r = self.get(qs=qs)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertIsInstance(r.json()["detail"], list)
        self.assertEqual((self.fake.employees.calls, self.fake.attendance_logs.calls), ([], []))   # 422 before any DB work

    def test_leap_day_is_a_valid_date_only_in_a_leap_year(self):
        self.load([emp("E1")], [])
        self.assertEqual(self.get(qs="from=2028-02-29&to=2028-02-29").status_code, 200)
        self.assertEqual(self.get(qs="from=2026-02-29&to=2026-02-29").status_code, 422)

    def test_reversed_range_is_422_and_one_day_is_200(self):
        self.load(*week_world())
        r = self.get(qs="from=2026-07-14&to=2026-07-06")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"][0]["loc"], ["query", "to"])
        self.assertEqual(len(self.get(qs="from=2026-07-06&to=2026-07-06").json()["items"]), 1)

    def test_92_dates_ok_93_dates_422(self):
        self.load(*week_world())
        ok = self.get(qs="from=2026-07-01&to=2026-09-30")
        self.assertEqual((ok.status_code, len(ok.json()["items"])), (200, 92))
        self.assertEqual(self.get(qs="from=2026-07-01&to=2026-10-01").status_code, 422)
        self.assertEqual(self.get(qs="from=2026-01-01&to=2026-12-31").status_code, 422)
        leap = self.get(qs="from=2028-01-01&to=2028-04-01")                                   # 31 + 29 + 31 + 1 = 92
        self.assertEqual((leap.status_code, len(leap.json()["items"])), (200, 92))
        self.assertEqual(self.get(qs="from=2028-01-01&to=2028-04-02").status_code, 422)

    def test_unknown_department_is_404_with_detail_string(self):
        self.load(*week_world())
        for dept in ("Nope", "ENG", "eng", "Eng%20"):
            with self.subTest(dept=dept):
                r = self.get(dept)
                self.assertEqual(r.status_code, 404)
                self.assertIsInstance(r.json()["detail"], str)

    def test_422_beats_404_validation_precedes_department_lookup(self):
        self.load(*week_world())
        for qs in ("from=bad&to=2026-07-14", "from=2026-07-14&to=2026-07-06", "from=2026-01-01&to=2026-12-31", None):
            with self.subTest(qs=qs):
                self.assertEqual(self.get("Nope", qs).status_code, 422)
        self.assertEqual(self.fake.employees.calls, [])

    def test_existing_department_without_logs_is_200_with_zero_rates(self):
        self.load([emp("E1")], [])
        body = self.get(qs="from=2026-07-06&to=2026-07-12").json()
        self.assertEqual(rates(body), [0.0] * 5 + [None, None])
        self.assertEqual([i["headcount"] for i in body["items"]], [1] * 7)

    def test_department_whose_employees_all_join_after_to_is_200_not_404(self):
        self.load([emp("E1", joined="2026-12-01")], [lg("E1", "2026-07-06")])
        r = self.get(qs="from=2026-07-06&to=2026-07-08")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([(i["headcount"], i["attendance_rate"], i["moving_avg_7d"], i["present_count"])
                          for i in r.json()["items"]], [(0, None, None, 0.0)] * 3)

    def test_case_sensitive_department_matching_over_http(self):
        self.load([emp("E1", "Sales"), emp("E2", "sales")], [lg("E1", "2026-07-06"), lg("E2", "2026-07-06"), lg("E2", "2026-07-07")])
        self.assertEqual(self.get("Sales", "from=2026-07-06&to=2026-07-07").json()["items"][0]["present_count"], 1.0)
        self.assertEqual(self.get("sales", "from=2026-07-06&to=2026-07-07").json()["items"][1]["present_count"], 1.0)
        self.assertEqual(self.get("SALES").status_code, 404)

    def test_department_with_spaces_unicode_and_slash_like_text(self):
        self.load([emp("E1", "R&D Lab"), emp("E2", "Ünïcode Dept")], [])
        self.assertEqual(self.get("R%26D%20Lab").status_code, 200)
        self.assertEqual(self.get("%C3%9Cn%C3%AFcode%20Dept").json()["department"], "Ünïcode Dept")
        self.assertEqual(self.get("summary").status_code, 404)                 # /departments/summary/trend is a department named 'summary'

    def test_array_department_does_not_make_a_department_exist(self):
        self.load([emp("E1", ["Eng"])], [])
        self.assertEqual(self.get("Eng").status_code, 404)

    def test_one_existence_lookup_and_one_aggregation_nothing_loaded_into_python(self):
        self.load(*week_world())
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.fake.employees.calls, ["find_one", "aggregate"])
        self.assertEqual(self.fake.attendance_logs.calls, [])                   # the logs are only reached by $lookup

    def test_openapi_lists_from_and_to_and_only_200_404_422(self):
        op = self.c.get("/openapi.json").json()["paths"]["/analytics/departments/{department}/trend"]["get"]
        params = {p["name"]: p for p in op["parameters"]}
        self.assertEqual(set(params), {"department", "from", "to"})
        self.assertTrue(all(p["required"] for p in params.values()))
        self.assertEqual(set(op["responses"]) - {"200", "404", "422"}, set())

    def test_existing_routes_are_still_registered(self):
        paths = set(self.c.get("/openapi.json").json()["paths"])
        for p in ("/analytics/departments/summary", "/analytics/leaderboard/late", "/analytics/employees/{emp_code}/monthly",
                  "/admin/explain/{endpoint}", "/analytics/departments/{department}/trend"):
            self.assertIn(p, paths)
        self.assertEqual(self.c.get("/analytics/departments/summary").status_code, 422)     # still routed to the summary, not 'trend'


@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestExplainHttp(FakeDbCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)

    def test_explain_reuses_the_endpoints_pipeline_with_execution_stats(self):
        self.load(*week_world())
        self.c.get("/analytics/departments/Eng/trend?from=2026-07-06&to=2026-07-14")
        endpoint_pipeline = self.fake.employees.last_pipeline
        r = self.c.get("/admin/explain/department_trend?department=Eng&from=2026-07-06&to=2026-07-14")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(sorted(body), ["collection", "endpoint", "explain"])
        self.assertEqual((body["endpoint"], body["collection"]), ("department_trend", "employees"))
        (cmd,) = self.fake.commands
        self.assertEqual(cmd["verbosity"], "executionStats")
        self.assertEqual(cmd["explain"]["aggregate"], "employees")
        self.assertEqual(cmd["explain"]["cursor"], {})
        self.assertEqual(json.dumps(cmd["explain"]["pipeline"], default=str), json.dumps(endpoint_pipeline, default=str))
        self.assertEqual(cmd["explain"]["pipeline"], m.department_trend_pipeline("2026-07-06", "2026-07-14", "Eng"))

    def test_explain_calls_the_one_builder(self):
        calls = []
        real = m.department_trend_pipeline

        def spy(*a, **k):
            calls.append(a)
            return real(*a, **k)
        m.department_trend_pipeline = spy
        try:
            self.load(*week_world())
            self.c.get("/analytics/departments/Eng/trend?from=2026-07-06&to=2026-07-14")
            self.c.get("/admin/explain/department_trend?department=Eng&from=2026-07-06&to=2026-07-14")
        finally:
            m.department_trend_pipeline = real
        self.assertEqual(calls, [("2026-07-06", "2026-07-14", "Eng")] * 2)

    def test_department_from_and_to_are_required_and_validated(self):
        base = "/admin/explain/department_trend?"
        for qs in ("", "department=Eng", "department=Eng&from=2026-07-06", "department=Eng&to=2026-07-14",
                   "from=2026-07-06&to=2026-07-14", "department=Eng&from=bad&to=2026-07-14",
                   "department=Eng&from=2026-07-14&to=2026-07-06", "department=Eng&from=2026-01-01&to=2026-12-31",
                   "department=Eng&from=2026-02-30&to=2026-03-01", "department=Eng&date_from=2026-07-06&date_to=2026-07-14"):
            with self.subTest(qs=qs):
                r = self.c.get(base + qs)
                self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.fake.commands, [])

    def test_missing_parameters_are_all_reported(self):
        r = self.c.get("/admin/explain/department_trend")
        self.assertEqual(sorted(e["loc"][-1] for e in r.json()["detail"]), ["department", "from", "to"])

    def test_explain_of_an_unknown_department_is_still_an_explain(self):
        r = self.c.get("/admin/explain/department_trend?department=Nobody&from=2026-07-06&to=2026-07-07")
        self.assertEqual(r.status_code, 200)

    def test_leaderboard_explain_and_other_targets_are_unchanged(self):
        self.assertEqual(self.c.get("/admin/explain/late_leaderboard?month=2026-07").status_code, 200)
        self.assertEqual(self.c.get("/admin/explain/late_leaderboard").status_code, 422)
        for other in ("attendance_list", "employee_monthly", "department_summary"):     # registered in Phase 5.5 (tests/test_phase5_5.py)
            self.assertNotEqual(self.c.get(f"/admin/explain/{other}").status_code, 501)
        self.assertEqual(self.c.get("/admin/explain/nope").status_code, 422)


# ======================================================================== explain-plan checks (shared)
def plan_problems(explain_output) -> list:
    """Problems that fail the grader's explain condition; an empty list means the plan passes: IXSCAN present, COLLSCAN
    absent anywhere in the output, every exposed collectionScans counter == 0."""
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


def plan_totals(explain_output) -> dict:
    """Execution numbers worth reporting: the $cursor stage's own stats plus every $lookup's counters."""
    out = {"cursor_keys": 0, "cursor_docs": 0, "lookup_keys": 0, "lookup_docs": 0, "lookup_collscans": 0, "millis": None}
    for stage in explain_output.get("stages", []):
        cur = stage.get("$cursor")
        if cur:
            es = cur.get("executionStats", {})
            out["cursor_keys"] += es.get("totalKeysExamined", 0)
            out["cursor_docs"] += es.get("totalDocsExamined", 0)
            out["millis"] = es.get("executionTimeMillis")
        if "$lookup" in stage:
            out["lookup_keys"] += stage.get("totalKeysExamined", 0)
            out["lookup_docs"] += stage.get("totalDocsExamined", 0)
            out["lookup_collscans"] += stage.get("collectionScans", 0)
    return out


class TestPlanChecks(unittest.TestCase):
    """The checker itself, on hand-written explain documents shaped like MongoDB 7.0 output (NOT real explain output)."""
    GOOD = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "FETCH", "inputStage": {
        "stage": "IXSCAN", "indexName": "department_1_emp_code_1"}}}}},
        {"$lookup": {"from": "attendance_logs"}, "totalDocsExamined": 5, "totalKeysExamined": 5, "collectionScans": 0,
         "indexesUsed": ["emp_code_1_date_1"]}]}

    def test_good_plan_passes(self):
        self.assertEqual(plan_problems(self.GOOD), [])
        self.assertTrue(plan_exposes_collection_scans(self.GOOD))

    def test_missing_ixscan_collscan_and_lookup_scans_fail(self):
        self.assertIn("no IXSCAN in the plan", plan_problems({"stages": [{"$cursor": {"x": {"stage": "FETCH"}}}]}))
        bad = json.loads(json.dumps(self.GOOD))
        bad["stages"][0]["$cursor"]["queryPlanner"]["rejectedPlans"] = [{"stage": "COLLSCAN"}]
        self.assertIn("COLLSCAN present in the plan", plan_problems(bad))
        bad = json.loads(json.dumps(self.GOOD))
        bad["stages"][1]["collectionScans"] = 3
        self.assertIn("$lookup collectionScans=3", plan_problems(bad))


# ======================================================================== 4. REAL MongoDB (opt-in)
def seed_big(rng, n_emps=5000, per=20, months=(4, 5, 6, 7, 8, 9)):
    """~n_emps*per = 100,000 attendance records over six months, so the 92-day range (Jul-Sep) holds about half."""
    depts = ["Engineering", "Sales", "Support", "engineering", "HR", "Ops", "Legal", "Data"]
    emps, logs = [], []
    for i in range(n_emps):
        dept = "BigDept" if i % 2 == 0 else rng.choice(depts)
        e = emp(f"E{i:05d}", dept, joined=f"2026-0{rng.choice([1, 2, 3, 4, 5, 6, 7, 8])}-01")
        emps.append(e)
        used = set()
        for k in range(per):
            month = months[k % len(months)]
            day = 1 + (k * 7 + i) % calendar.monthrange(2026, month)[1]
            if (month, day) in used:
                continue
            used.add((month, day))
            logs.append(lg(e["emp_code"], f"2026-{month:02d}-{day:02d}", late=rng.choice([0, 0, 1, 10, 45, -5, None]),
                           status=rng.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]), half=rng.choice([False, False, True])))
    return emps, logs


@unittest.skipUnless(HAVE_HTTP and TEST_URI, "set TEST_MONGO_URI to run the real-MongoDB integration tests: NOT EXECUTED")
class TestMongoTrend(unittest.TestCase):
    """REAL MongoDB in a throwaway database (attendance_test_p54_<random>), dropped at the end."""
    PREFIX = "attendance_test_p54_"

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
                if docs[i:i + 5000]:
                    self.tdb[coll].insert_many([dict(d) for d in docs[i:i + 5000]])

    def trend(self, dept, f, t):
        return m.department_trend(department=dept, date_from=f, date_to=t)

    def test_server_version_is_reported(self):
        print(f"\n[real MongoDB server version: {self.server_version}]")
        self.assertTrue(self.server_version)

    def test_existing_index_definitions_are_unchanged(self):
        e = {tuple(v["key"]) for k, v in self.tdb.employees.index_information().items() if k != "_id_"}
        a = {tuple(v["key"]) for k, v in self.tdb.attendance_logs.index_information().items() if k != "_id_"}
        self.assertEqual(e, {(("emp_code", 1),), (("department", 1), ("emp_code", 1))})
        self.assertEqual(a, {(("emp_code", 1), ("date", 1)), (("date", -1), ("emp_code", 1)),
                             (("status", 1), ("date", -1), ("emp_code", 1)), (("emp_code", 1), ("punch_in", -1))})

    def test_real_date_spine_boundaries_and_flags(self):
        self.seed([emp("E1", joined="2020-01-01")], [])
        for f, t, n in (("2026-07-03", "2026-07-13", 11), ("2026-12-28", "2027-01-04", 8), ("2028-02-27", "2028-03-02", 5),
                        ("2026-02-26", "2026-03-02", 5), ("2026-07-01", "2026-09-30", 92), ("2028-01-01", "2028-04-01", 92),
                        ("2026-07-06", "2026-07-06", 1), ("2026-03-28", "2026-03-30", 3), ("2026-10-24", "2026-11-02", 10)):
            with self.subTest(f=f, t=t):
                body = self.trend("Eng", f, t)
                self.assertEqual([i["date"] for i in body["items"]], day_range(f, t))
                self.assertEqual(len(body["items"]), n)
                self.assertEqual([i["is_working_day"] for i in body["items"]],
                                 [date.fromisoformat(d).weekday() < 5 for d in day_range(f, t)])

    def test_real_hand_computed_week(self):
        emps, logs = week_world()
        self.seed(emps, logs)
        body = self.trend("Eng", "2026-07-06", "2026-07-14")
        self.assertEqual(rates(body), [1.0, 0.9, 0.8, 0.7, 0.6, None, None, 1.0, 0.0])
        self.assertEqual(avgs(body), [1.0, 0.95, 0.9, 0.85, 0.8, 0.8, 0.8, 0.8, 0.62])

    def test_real_half_up_boundaries(self):
        self.seed([emp(c) for c in codes(16)], [lg("E001", "2026-07-06", half=True)])
        body = self.trend("Eng", "2026-07-06", "2026-07-07")
        self.assertEqual(rates(body), [0.0313, 0.0])
        self.assertEqual(avgs(body), [0.0313, 0.0157])

    def test_real_semantics_edge_cases_match_the_oracle(self):
        no_dept = drop(emp("E9"), "department")
        emps = [emp("E1"), emp("E2", joined="2026-07-08"), emp("E3", "eng"), emp("E4", ["Eng"]), emp("E5", 5), emp("E6", None),
                no_dept, emp("E7", "Eng", joined="2026-12-01"), emp("E8", "")]
        logs = [lg("E1", "2026-07-06", late=5), lg("E1", "2026-07-07", "ABSENT", late=9), drop(lg("E1", "2026-07-08"), "half_day"),
                lg("E1", "2026-07-11", "WFH", half=True, late=2), lg("E2", "2026-07-06"), lg("E2", "2026-07-09", "ON_DUTY"),
                lg("E3", "2026-07-06"), lg("E4", "2026-07-06"), lg("E5", "2026-07-06"), lg("E6", "2026-07-06"),
                lg("E9", "2026-07-06"), lg("E7", "2026-07-06"), lg("GHOST", "2026-07-06", late=9), lg("E8", "2026-07-06"),
                drop(lg("E1", "2026-07-09"), "late_minutes"), lg("E1", "2026-07-10", late=None), lg("E1", "2026-07-12", "LEAVE", late=-3)]
        self.seed(emps, logs)
        for dept in ("Eng", "eng", "ENG", "", "Nope"):
            with self.subTest(dept=dept):
                if dept in ("ENG", "Nope"):
                    from fastapi import HTTPException
                    with self.assertRaises(HTTPException):
                        self.trend(dept, "2026-07-06", "2026-07-14")
                else:
                    self.assertEqual(self.trend(dept, "2026-07-06", "2026-07-14"), oracle(emps, logs, dept, "2026-07-06", "2026-07-14"))

    def test_real_empty_and_all_joined_later_departments(self):
        self.seed([emp("E1")], [])
        self.assertEqual(rates(self.trend("Eng", "2026-07-06", "2026-07-12")), [0.0] * 5 + [None, None])
        self.tdb.employees.delete_many({})
        self.seed([emp("E1", joined="2026-12-01")], [lg("E1", "2026-07-06")])
        body = self.trend("Eng", "2026-07-06", "2026-07-08")
        self.assertEqual(rates(body) + avgs(body), [None] * 6)
        self.assertEqual([i["headcount"] for i in body["items"]], [0, 0, 0])

    def test_real_random_worlds_match_the_oracle(self):
        for seed in range(6):
            rng = random.Random(seed)
            emps, logs = random_world(rng)
            self.tdb.employees.delete_many({})
            self.tdb.attendance_logs.delete_many({})
            self.seed(emps, logs)
            for dept in ("Eng", "eng", "Sales", "Ops"):
                for f, t in (("2026-05-20", "2026-06-15"), ("2026-06-01", "2026-08-31"), ("2026-07-04", "2026-07-04")):
                    with self.subTest(seed=seed, dept=dept, f=f, t=t):
                        if not self.tdb.employees.find_one({"department": dept}):
                            continue
                        self.assertEqual(self.trend(dept, f, t), oracle(emps, logs, dept, f, t))

    # ---------------- the release-blocker checks: real EXPLAIN on ~100,000 attendance records ----------------
    def seed_large(self):
        emps, logs = seed_big(random.Random(7))
        self.seed(emps, logs)
        n = self.tdb.attendance_logs.count_documents({})
        self.assertGreaterEqual(n, 95_000)
        return emps, logs

    def explain(self, dept, f, t):
        return self.tdb.command({"explain": {"aggregate": m.TREND_COLLECTION, "pipeline": m.department_trend_pipeline(f, t, dept),
                                             "cursor": {}}, "verbosity": "executionStats"})

    def check_plan(self, out, label):
        print(f"\n[explain, {label}] server={self.server_version} {describe(out)} totals={plan_totals(out)} "
              f"collectionScans_exposed={plan_exposes_collection_scans(out)}")
        self.assertEqual(plan_problems(out), [], f"{label} plan: {describe(out)}")

    def test_explain_every_shape_uses_an_index_and_no_collection_scan(self):
        self.seed_large()
        for label, dept, f, t in (("wide range, ~2500-employee department", "BigDept", "2026-07-01", "2026-09-30"),
                                  ("wide range, small department", "Legal", "2026-07-01", "2026-09-30"),
                                  ("one week, big department", "BigDept", "2026-07-06", "2026-07-12"),
                                  ("one day", "Engineering", "2026-07-06", "2026-07-06"),
                                  ("unknown department", "Nobody", "2026-07-01", "2026-09-30")):
            with self.subTest(label=label):
                self.check_plan(self.explain(dept, f, t), label)

    def test_large_dataset_results_match_the_oracle(self):
        emps, logs = self.seed_large()
        for dept, f, t in (("Engineering", "2026-07-01", "2026-09-30"), ("BigDept", "2026-08-10", "2026-09-05")):
            with self.subTest(dept=dept):
                self.assertEqual(self.trend(dept, f, t), oracle(emps, logs, dept, f, t))

    def test_http_routes_with_real_mongo(self):
        from fastapi.testclient import TestClient
        self.seed(*week_world())
        c = TestClient(m.app)
        r = c.get("/analytics/departments/Eng/trend?from=2026-07-06&to=2026-07-14")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(avgs(r.json())[-1], 0.62)
        self.assertNotIn("_id", r.text)
        self.assertEqual(c.get("/analytics/departments/Nope/trend?from=2026-07-06&to=2026-07-14").status_code, 404)
        r = c.get("/admin/explain/department_trend?department=Eng&from=2026-07-06&to=2026-07-14")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["endpoint"], body["collection"]), ("department_trend", "employees"))
        self.assertIn("executionStats", json.dumps(body["explain"]))
        self.assertEqual(c.get("/admin/explain/department_trend?department=Eng").status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
