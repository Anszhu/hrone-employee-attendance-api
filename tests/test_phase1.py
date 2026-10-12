"""Phase 1 tests. Run from the repo root:  python -m unittest discover -s tests -v
(stdlib unittest only; pytest also collects these.)"""
import json
import os
import pathlib
import re
import time
import unittest
import calendar
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

from tests._loader import load_main, have, ROOT

m, MODE = load_main()
FULL = MODE == "full"
HAVE_PYDANTIC = FULL and have("pydantic")
HAVE_HTTP = FULL and have("httpx")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ist(y, mo, d, h=0, mi=0, s=0, us=0):
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


def utc_ms(y, mo, d, h=0, mi=0, s=0, ms=0):
    """Independent oracle for epoch ms (calendar.timegm, not the code under test)."""
    return calendar.timegm((y, mo, d, h, mi, s)) * 1000 + ms


class TestModeBanner(unittest.TestCase):
    def test_banner(self):
        print(f"\n[main.py loaded in mode: {MODE}]")


# ---------------------------------------------------------------- strict epoch ms
class TestEpochValidation(unittest.TestCase):
    def test_accepts_range_edges_and_typical(self):
        for v in (m.EPOCH_MS_MIN, m.EPOCH_MS_MAX, 1783312500000, 1_700_000_000_000):
            with self.subTest(v=v):
                self.assertEqual(m.validate_epoch_ms(v), v)

    def test_rejects_seconds(self):
        for v in (1783312500, 99_999_999_999, 0, 1, -1, -1783312500000):
            with self.subTest(v=v), self.assertRaises(ValueError):
                m.validate_epoch_ms(v)

    def test_rejects_out_of_range_high(self):
        for v in (m.EPOCH_MS_MAX + 1, 4_102_444_800_001, 7_258_118_400_000, 10**30):
            with self.subTest(v=v), self.assertRaises(ValueError):
                m.validate_epoch_ms(v)

    def test_rejects_floats(self):
        for v in (1783312500000.0, 1783312500000.5, 1.7e12, float("nan"), float("inf")):
            with self.subTest(v=v), self.assertRaises(ValueError):
                m.validate_epoch_ms(v)

    def test_rejects_strings(self):
        for v in ("1783312500000", "", "banana", " 1783312500000"):
            with self.subTest(v=v), self.assertRaises(ValueError):
                m.validate_epoch_ms(v)

    def test_rejects_booleans(self):
        for v in (True, False):
            with self.subTest(v=v), self.assertRaises(ValueError):
                m.validate_epoch_ms(v)

    def test_rejects_other_types(self):
        for v in (None, [], {}, Decimal("1783312500000"), b"1783312500000"):
            with self.subTest(v=v), self.assertRaises(ValueError):
                m.validate_epoch_ms(v)


# ---------------------------------------------------------------- calendar dates
class TestCalendarDate(unittest.TestCase):
    def test_valid(self):
        for v in ("2026-07-06", "2028-02-29", "2026-12-31", "0001-01-01"):
            with self.subTest(v=v):
                self.assertEqual(m.validate_calendar_date(v), v)

    def test_invalid(self):
        bad = ["2026-02-30", "2027-02-29", "2026-13-01", "2026-00-10", "2026-07-00", "2026-7-6",
               "20260706", "2026-07-06T00:00", " 2026-07-06", "2026-07-06\n", "yesterday", "",
               "\uff12\uff10\uff12\uff16-07-06", "0000-01-01", None, 20260706]
        for v in bad:
            with self.subTest(v=v), self.assertRaises(ValueError):
                m.validate_calendar_date(v)


class TestPatterns(unittest.TestCase):
    """The contract regexes as constants, checked with Python's re (engine-independent cases)."""

    def check(self, pattern, good, bad):
        for v in good:
            with self.subTest(good=v):
                self.assertIsNotNone(re.fullmatch(pattern, v))
        for v in bad:
            with self.subTest(bad=v):
                self.assertIsNone(re.fullmatch(pattern, v))

    def test_emp_code(self):
        self.check(m.EMP_CODE_PATTERN, ["EMP0001", "EMP12345", "EMP123456"],
                   ["EMP1", "EMP123", "EMP1234567", "emp0001", "EMP00A1", "EMP0001\n", " EMP0001",
                    "EMP\u0660\u0660\u0660\u0661", ""])

    def test_email(self):
        self.check(m.EMAIL_PATTERN, ["a@b.co", "asha@example.com"],
                   ["nope", "a@b", "a b@c.de", "@b.co", "a@@b.co", "a@b.c\n"])

    def test_hhmm(self):
        self.check(m.HHMM_PATTERN, ["00:00", "09:30", "23:59", "18:30"],
                   ["24:00", "24:61", "9:30", "09:60", "09:5", "0930", "09:30:00", ""])


# ---------------------------------------------------------------- conversions / IST
class TestEpochConversion(unittest.TestCase):
    def test_known_value(self):
        ms = utc_ms(2026, 7, 6, 3, 58)
        dt = m.epoch_ms_to_datetime(ms)
        self.assertEqual(dt, datetime(2026, 7, 6, 3, 58, tzinfo=UTC))
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(m.datetime_to_epoch_ms(dt), ms)

    def test_roundtrip_with_millis(self):
        for ms in (m.EPOCH_MS_MIN, m.EPOCH_MS_MAX, 1783312500123, 1783312500999, 1783312500001):
            with self.subTest(ms=ms):
                self.assertEqual(m.datetime_to_epoch_ms(m.epoch_ms_to_datetime(ms)), ms)

    def test_aware_non_utc_input(self):
        self.assertEqual(m.datetime_to_epoch_ms(ist(2026, 7, 6, 9, 28)), utc_ms(2026, 7, 6, 3, 58))

    def test_truncation_keeps_whole_seconds_only(self):
        ms = utc_ms(2026, 7, 6, 4, 10, 40, 900)
        got = m.instant_from_epoch_ms(ms)
        self.assertEqual(got, datetime(2026, 7, 6, 4, 10, 40, tzinfo=UTC))
        self.assertEqual(got.microsecond, 0)
        self.assertEqual(m.datetime_to_epoch_ms(got), utc_ms(2026, 7, 6, 4, 10, 40))

    def test_epoch_ms_to_datetime_rejects_non_int(self):
        for v in (1.5e12, "1", True):
            with self.subTest(v=v), self.assertRaises(TypeError if v is not True else Exception):
                m.epoch_ms_to_datetime(v)

    def test_utc_now_is_aware_utc(self):
        n = m.utc_now()
        self.assertEqual(n.utcoffset(), timedelta(0))

    def test_naive_is_treated_as_utc_not_local(self):
        naive = datetime(2026, 7, 6, 3, 58)
        self.assertEqual(m.datetime_to_epoch_ms(naive), utc_ms(2026, 7, 6, 3, 58))


class TestISTAcrossUTCBoundaries(unittest.TestCase):
    CASES = [  # (UTC instant, expected IST date)
        (datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC), "2026-07-06"),   # 05:30 IST
        (datetime(2026, 7, 6, 18, 29, 59, tzinfo=UTC), "2026-07-06"),  # 23:59:59 IST
        (datetime(2026, 7, 6, 18, 30, 0, tzinfo=UTC), "2026-07-07"),   # 00:00:00 IST
        (datetime(2026, 7, 6, 23, 59, 59, tzinfo=UTC), "2026-07-07"),  # 05:29:59 IST
        (datetime(2026, 12, 31, 18, 30, 0, tzinfo=UTC), "2027-01-01"),  # year rollover
        (datetime(2028, 2, 28, 18, 30, 0, tzinfo=UTC), "2028-02-29"),   # leap day
    ]

    def test_ist_date(self):
        for dt, expected in self.CASES:
            with self.subTest(dt=dt.isoformat()):
                self.assertEqual(m.ist_date_str(dt), expected)

    def test_ist_date_from_epoch_ms(self):
        ms = utc_ms(2026, 7, 6, 18, 30, 0)
        self.assertEqual(m.ist_date_str(m.epoch_ms_to_datetime(ms)), "2026-07-07")
        self.assertEqual(m.ist_date_str(m.epoch_ms_to_datetime(ms - 1000)), "2026-07-06")

    def test_to_ist_wall_clock(self):
        t = m.to_ist(datetime(2026, 7, 6, 4, 11, 1, tzinfo=UTC))
        self.assertEqual((t.hour, t.minute, t.second), (9, 41, 1))
        self.assertEqual(t.utcoffset(), timedelta(hours=5, minutes=30))

    def test_millisecond_truncation_before_date_decision(self):
        # 23:59:59.900 IST stays on the same day; truncation never rounds up to midnight.
        dt = ist(2026, 7, 6, 23, 59, 59, 900_000)
        self.assertEqual(m.attendance_date(dt, "09:30", "18:30"), "2026-07-06")


class TestAttendanceDate(unittest.TestCase):
    def test_day_shift_uses_ist_date(self):
        self.assertEqual(m.attendance_date(ist(2026, 7, 6, 9, 28), "09:30", "18:30"), "2026-07-06")
        self.assertEqual(m.attendance_date(ist(2026, 7, 6, 0, 0, 0), "09:30", "18:30"), "2026-07-06")

    def test_overnight_before_shift_end_belongs_to_previous_day(self):
        s, e = "22:00", "06:00"
        self.assertEqual(m.attendance_date(ist(2026, 7, 7, 3, 0), s, e), "2026-07-06")
        self.assertEqual(m.attendance_date(ist(2026, 7, 7, 5, 59, 59), s, e), "2026-07-06")
        self.assertEqual(m.attendance_date(ist(2026, 7, 7, 0, 0, 0), s, e), "2026-07-06")

    def test_overnight_at_or_after_shift_end_is_same_day(self):
        s, e = "22:00", "06:00"
        self.assertEqual(m.attendance_date(ist(2026, 7, 7, 6, 0, 0), s, e), "2026-07-07")
        self.assertEqual(m.attendance_date(ist(2026, 7, 6, 21, 55), s, e), "2026-07-06")
        self.assertEqual(m.attendance_date(ist(2026, 7, 6, 22, 0), s, e), "2026-07-06")

    def test_overnight_boundary_uses_truncated_seconds(self):
        # 05:59:59.900 truncates to 05:59:59 (< 06:00) -> previous day.
        self.assertEqual(m.attendance_date(ist(2026, 7, 7, 5, 59, 59, 900_000), "22:00", "06:00"), "2026-07-06")

    def test_overnight_across_utc_midnight(self):
        # 2026-07-06 21:55 IST == 2026-07-06 16:25Z (sample #7)
        self.assertEqual(m.attendance_date(datetime(2026, 7, 6, 16, 25, tzinfo=UTC), "22:00", "06:00"), "2026-07-06")

    def test_shift_end_equal_to_start_is_overnight(self):
        self.assertTrue(m.is_overnight_shift("09:30", "09:30"))
        self.assertTrue(m.is_overnight_shift("22:00", "06:00"))
        self.assertFalse(m.is_overnight_shift("09:30", "18:30"))
        self.assertFalse(m.is_overnight_shift("00:00", "00:01"))


# ---------------------------------------------------------------- R2 late
class TestLateMinutes(unittest.TestCase):
    D = "2026-07-06"

    def late(self, h, mi, s=0, us=0, start="09:30"):
        return m.compute_late_minutes(ist(2026, 7, 6, h, mi, s, us), start, self.D)

    def test_contract_examples(self):
        self.assertEqual(self.late(9, 40, 0), 0)
        self.assertEqual(self.late(9, 40, 1), 10)
        self.assertEqual(self.late(10, 15, 59), 45)

    def test_audit_boundaries(self):
        self.assertEqual(self.late(9, 40, 59), 10)   # audit: was 0
        self.assertEqual(self.late(9, 41, 0), 11)
        self.assertEqual(self.late(9, 39, 59), 0)
        self.assertEqual(self.late(9, 30, 0), 0)
        self.assertEqual(self.late(8, 0, 0), 0)       # early
        self.assertEqual(self.late(9, 40, 0, 999_999), 0)  # 09:40:00.999999 truncates to 09:40:00

    def test_milliseconds_truncated_first(self):
        self.assertEqual(self.late(9, 40, 0, 900_000), 0)   # spec example 09:40:00.900 == 09:40:00
        self.assertEqual(self.late(9, 40, 1, 900_000), 10)

    def test_utc_input_is_converted_to_ist(self):
        # 09:41:01 IST == 04:11:01Z (audit: this gave 0)
        self.assertEqual(m.compute_late_minutes(datetime(2026, 7, 6, 4, 11, 1, tzinfo=UTC), "09:30", self.D), 11)

    def test_other_shift_start(self):
        self.assertEqual(self.late(14, 11, 0, start="14:00"), 11)
        self.assertEqual(self.late(14, 10, 0, start="14:00"), 0)

    def test_overnight_after_midnight_measured_from_previous_day(self):
        # punch-in 03:00 IST on 07-07 for the 22:00-06:00 shift of 07-06 -> 5h late (audit: gave 0)
        p = ist(2026, 7, 7, 3, 0)
        d = m.attendance_date(p, "22:00", "06:00")
        self.assertEqual(d, "2026-07-06")
        self.assertEqual(m.compute_late_minutes(p, "22:00", d), 300)

    def test_overnight_on_time_and_early(self):
        self.assertEqual(m.compute_late_minutes(ist(2026, 7, 6, 21, 55), "22:00", "2026-07-06"), 0)
        self.assertEqual(m.compute_late_minutes(ist(2026, 7, 6, 22, 10, 1), "22:00", "2026-07-06"), 10)

    def test_naive_datetime_is_utc_regardless_of_local_tz(self):
        naive_utc = datetime(2026, 7, 6, 4, 11, 1)  # == 09:41:01 IST
        self.assertEqual(m.compute_late_minutes(naive_utc, "09:30", self.D), 11)


# ---------------------------------------------------------------- R4 work hours / R5 half day
class TestWorkHours(unittest.TestCase):
    T0 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)

    def hours(self, seconds):
        return m.compute_work_hours(self.T0, self.T0 + timedelta(seconds=seconds))

    def test_audit_case_14850_seconds(self):
        self.assertEqual(self.hours(14850), 4.13)   # 4.125 is a tie -> up (was 4.12)

    def test_small_ties_and_neighbours(self):
        self.assertEqual(self.hours(54), 0.02)      # 0.015 -> up (was 0.01)
        self.assertEqual(self.hours(53), 0.01)
        self.assertEqual(self.hours(18), 0.01)      # 0.005 -> up
        self.assertEqual(self.hours(17), 0.0)
        self.assertEqual(self.hours(0), 0.0)

    def test_exact_values(self):
        self.assertEqual(self.hours(3600), 1.0)
        self.assertEqual(self.hours(86400), 24.0)
        self.assertEqual(self.hours(12600), 3.5)

    def test_sample_documents(self):
        t = lambda *a: datetime(*a, tzinfo=UTC)
        self.assertEqual(m.compute_work_hours(t(2026, 7, 6, 3, 58), t(2026, 7, 6, 13, 5)), 9.12)
        self.assertEqual(m.compute_work_hours(t(2026, 7, 6, 16, 25), t(2026, 7, 7, 1, 10)), 8.75)

    def test_exhaustive_half_up_over_24_hours(self):
        """Every whole-second duration 0..86400 against Decimal ROUND_HALF_UP. Includes all
        2400 exact ties (seconds % 36 == 18)."""
        q = Decimal("0.01")
        ties = 0
        for s in range(0, 86401):
            expected = (Decimal(s) / Decimal(3600)).quantize(q, rounding=ROUND_HALF_UP)
            got = Decimal(repr(self.hours(s)))
            if s % 36 == 18:
                ties += 1
            if got != expected:
                self.fail(f"{s}s: got {got}, expected {expected}")
        self.assertEqual(ties, 2400)

    def test_microseconds_are_truncated(self):
        a = self.T0.replace(microsecond=900_000)
        b = (self.T0 + timedelta(hours=8)).replace(microsecond=100_000)
        self.assertEqual(m.compute_work_hours(a, b), 8.0)

    def test_negative_duration_raises(self):
        with self.assertRaises(ValueError):
            m.compute_work_hours(self.T0, self.T0 - timedelta(seconds=1))

    def test_return_type_is_float(self):
        self.assertIsInstance(self.hours(100), float)


class TestHalfDay(unittest.TestCase):
    def test_threshold(self):
        self.assertTrue(m.compute_half_day(4.49))
        self.assertFalse(m.compute_half_day(4.5))
        self.assertFalse(m.compute_half_day(4.51))
        self.assertTrue(m.compute_half_day(0.0))
        self.assertTrue(m.compute_half_day(3.5))
        self.assertFalse(m.compute_half_day(9.12))
        self.assertFalse(m.compute_half_day(None))

    def test_uses_rounded_hours_end_to_end(self):
        t0 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        def hd(sec):
            return m.compute_half_day(m.compute_work_hours(t0, t0 + timedelta(seconds=sec)))
        self.assertFalse(hd(16182))  # 4.495 h ties up to 4.50 -> NOT half day
        self.assertTrue(hd(16181))   # 4.4947 h -> 4.49 -> half day
        self.assertFalse(hd(16200))  # exactly 4.5
        self.assertTrue(hd(12600))   # sample #4: 3.5 h


# ---------------------------------------------------------------- R3 overtime
class TestOvertime(unittest.TestCase):
    D = "2026-07-06"

    def ot(self, h, mi, s=0, d=6, start="09:30", end="18:30", date=None):
        return m.compute_overtime(ist(2026, 7, d, h, mi, s), start, end, date or self.D)

    def test_29_30_31_minute_boundaries(self):
        self.assertEqual(self.ot(18, 59, 59), 0)    # 29:59 -> 29 -> below minimum
        self.assertEqual(self.ot(18, 59, 0), 0)     # 29 min  (audit: gave 29)
        self.assertEqual(self.ot(19, 0, 0), 30)     # exactly 30
        self.assertEqual(self.ot(19, 0, 59), 30)    # floor
        self.assertEqual(self.ot(19, 1, 0), 31)
        self.assertEqual(self.ot(19, 10, 0), 40)    # sample #2

    def test_before_or_at_shift_end(self):
        self.assertEqual(self.ot(18, 30, 0), 0)
        self.assertEqual(self.ot(18, 0, 0), 0)
        self.assertEqual(self.ot(12, 0, 0), 0)

    def test_milliseconds_truncated(self):
        self.assertEqual(m.compute_overtime(ist(2026, 7, 6, 18, 59, 59, 900_000), "09:30", "18:30", self.D), 0)
        self.assertEqual(m.compute_overtime(ist(2026, 7, 6, 19, 0, 0, 100_000), "09:30", "18:30", self.D), 30)

    def test_overnight_example_from_audit(self):
        # 22:00-06:00 shift of 07-06, punch-out 06:40 IST on 07-07 -> 40 (audit: gave 1480)
        self.assertEqual(self.ot(6, 40, d=7, start="22:00", end="06:00"), 40)

    def test_overnight_boundaries(self):
        kw = dict(d=7, start="22:00", end="06:00")
        self.assertEqual(self.ot(6, 29, 59, **kw), 0)
        self.assertEqual(self.ot(6, 30, 0, **kw), 30)
        self.assertEqual(self.ot(6, 31, 0, **kw), 31)
        self.assertEqual(self.ot(6, 0, 0, **kw), 0)
        self.assertEqual(self.ot(2, 0, 0, **kw), 0)

    def test_overnight_punch_out_before_midnight_is_not_overtime(self):
        self.assertEqual(self.ot(23, 30, d=6, start="22:00", end="06:00"), 0)

    def test_utc_input(self):
        # 19:10 IST == 13:40Z (sample #2); 06:40 IST next day == 01:10Z (sample #7)
        self.assertEqual(m.compute_overtime(datetime(2026, 7, 6, 13, 40, tzinfo=UTC), "09:30", "18:30", "2026-07-06"), 40)
        self.assertEqual(m.compute_overtime(datetime(2026, 7, 7, 1, 10, tzinfo=UTC), "22:00", "06:00", "2026-07-06"), 40)


# ---------------------------------------------------------------- local time zone independence
@unittest.skipUnless(hasattr(time, "tzset"), "time.tzset not available on this platform")
class TestIndependentOfLocalTimeZone(unittest.TestCase):
    ZONES = ["UTC", "Asia/Kolkata", "America/Los_Angeles", "Pacific/Auckland", "Asia/Kathmandu"]

    def setUp(self):
        self._old = os.environ.get("TZ")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old
        time.tzset()

    def snapshot(self):
        naive = datetime(2026, 7, 6, 4, 11, 1)
        aware = datetime(2026, 7, 6, 18, 30, 0, tzinfo=UTC)
        out_dt = datetime(2026, 7, 7, 1, 10, tzinfo=UTC)
        return (
            m.compute_late_minutes(naive, "09:30", "2026-07-06"),
            m.ist_date_str(aware),
            m.datetime_to_epoch_ms(naive),
            m.datetime_to_epoch_ms(aware),
            m.attendance_date(aware, "22:00", "06:00"),
            m.compute_overtime(out_dt, "22:00", "06:00", "2026-07-06"),
            m.compute_work_hours(datetime(2026, 7, 6, 16, 25), out_dt),
            m.instant_from_epoch_ms(1783312500900),
        )

    def test_identical_results_in_every_zone(self):
        results = {}
        for z in self.ZONES:
            os.environ["TZ"] = z
            time.tzset()
            results[z] = self.snapshot()
        base = results["UTC"]
        for z, r in results.items():
            with self.subTest(zone=z):
                self.assertEqual(r, base)
        self.assertEqual(base[0], 11)
        self.assertEqual(base[1], "2026-07-07")
        self.assertEqual(base[5], 40)


# ---------------------------------------------------------------- indexes
class _FakeCollection:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def create_index(self, keys, **opts):
        self.log.append((self.name, list(keys), dict(opts)))
        return "_".join(f"{k}_{d}" for k, d in keys)


class _FakeDB:
    def __init__(self):
        self.log = []

    def __getitem__(self, name):
        return _FakeCollection(self.log, name)


class TestIndexSpecs(unittest.TestCase):
    """Specs only. Real idempotency and plan usage need MongoDB (not run here)."""

    def test_expected_specs(self):
        fake = _FakeDB()
        names = m.ensure_indexes(fake)
        self.assertEqual(fake.log, [
            ("employees", [("emp_code", 1)], {"unique": True}),
            ("employees", [("department", 1), ("emp_code", 1)], {}),
            ("attendance_logs", [("emp_code", 1), ("date", 1)], {"unique": True}),
            ("attendance_logs", [("date", -1), ("emp_code", 1)], {}),
            ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], {}),
            ("attendance_logs", [("emp_code", 1), ("punch_in", -1)], {}),
        ])
        self.assertEqual(len(names), 6)

    def test_unique_indexes_are_exactly_the_two_natural_keys(self):
        uniq = [(c, tuple(k)) for c, specs in m.INDEXES.items() for k, o in specs if o.get("unique")]
        self.assertEqual(sorted(uniq), [("attendance_logs", (("emp_code", 1), ("date", 1))),
                                        ("employees", (("emp_code", 1),))])

    def test_repeated_calls_issue_identical_specs_and_no_names(self):
        a, b = _FakeDB(), _FakeDB()
        m.ensure_indexes(a)
        m.ensure_indexes(a)
        m.ensure_indexes(b)
        self.assertEqual(a.log[:6], a.log[6:])
        self.assertEqual(a.log[:6], b.log)
        self.assertTrue(all("name" not in o for _, _, o in a.log))

    def test_list_sort_order_matches_an_index(self):
        # GET /attendance sorts date desc, emp_code asc -> same key pattern as an index.
        self.assertIn([("date", -1), ("emp_code", 1)], [k for k, _ in m.INDEXES["attendance_logs"]])


# ---------------------------------------------------------------- sample data vs helpers
def _dt(v):
    if v is None:
        return None
    return datetime.strptime(v["$date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class TestSampleDataAgainstHelpers(unittest.TestCase):
    def test_all_nine_sample_documents(self):
        base = ROOT / "sample_data"
        emps = {e["emp_code"]: e for e in json.loads((base / "employees.json").read_text())}
        logs = json.loads((base / "attendance_logs.json").read_text())
        self.assertEqual(len(logs), 9)
        for lg in logs:
            with self.subTest(emp=lg["emp_code"], date=lg["date"]):
                e = emps[lg["emp_code"]]
                pi, po = _dt(lg["punch_in"]), _dt(lg["punch_out"])
                if pi is None:
                    continue
                d = m.attendance_date(pi, e["shift_start"], e["shift_end"])
                self.assertEqual(d, lg["date"])
                self.assertEqual(m.compute_late_minutes(pi, e["shift_start"], d), lg["late_minutes"])
                if po is None:
                    self.assertIsNone(lg["work_hours"])
                    continue
                wh = m.compute_work_hours(pi, po)
                self.assertEqual(wh, lg["work_hours"])
                self.assertEqual(m.compute_overtime(po, e["shift_start"], e["shift_end"], d), lg["overtime_minutes"])
                self.assertEqual(m.compute_half_day(wh), lg.get("half_day", False))


# ---------------------------------------------------------------- pydantic models (need pydantic)
@unittest.skipUnless(HAVE_PYDANTIC, "pydantic/fastapi not installed: NOT EXECUTED")
class TestRequestModels(unittest.TestCase):
    GOOD_EMP = dict(emp_code="EMP0001", name="Asha Rao", email="asha@example.com",
                    department="Engineering", joined_on="2026-01-05")

    def emp(self, **over):
        d = dict(self.GOOD_EMP)
        d.update(over)
        return m.EmployeeIn(**d)

    def test_employee_valid_and_defaults(self):
        e = self.emp()
        self.assertEqual((e.shift_start, e.shift_end), ("09:30", "18:30"))
        self.assertEqual(self.emp(shift_start="22:00", shift_end="06:00").shift_end, "06:00")
        self.assertEqual(self.emp(emp_code="EMP123456").emp_code, "EMP123456")

    def test_employee_invalid_matrix(self):
        from pydantic import ValidationError
        bad = [
            dict(emp_code="EMP1"), dict(emp_code="EMP1234567"), dict(emp_code="emp0001"),
            dict(emp_code="EMP0001\n"), dict(emp_code=""), dict(emp_code=1234),
            dict(name=""), dict(name="x" * 101), dict(name=None),
            dict(email="nope"), dict(email="a@b"), dict(email="a" * 120 + "@b.co"),
            dict(department=""), dict(department="d" * 51),
            dict(shift_start="24:61"), dict(shift_start="9:30"), dict(shift_end="18:60"),
            dict(shift_start="09:30", shift_end="09:30"),
            dict(joined_on="2026-02-30"), dict(joined_on="yesterday"), dict(joined_on="20260105"),
            dict(joined_on=20260105),
        ]
        for over in bad:
            with self.subTest(over=over), self.assertRaises(ValidationError):
                self.emp(**over)

    def test_employee_missing_required(self):
        from pydantic import ValidationError
        for k in self.GOOD_EMP:
            d = dict(self.GOOD_EMP)
            d.pop(k)
            with self.subTest(missing=k), self.assertRaises(ValidationError):
                m.EmployeeIn(**d)

    def test_employee_ignores_derived_and_unknown_fields(self):
        e = self.emp(created_at=1, _id="x", extra="y")
        self.assertNotIn("created_at", e.model_dump())
        self.assertNotIn("extra", e.model_dump())

    def test_punch_in_valid(self):
        p = m.PunchInIn(emp_code="EMP0001", punched_at=1783312500000, status="WFH")
        self.assertEqual((p.punched_at, p.status), (1783312500000, "WFH"))
        d = m.PunchInIn(emp_code="EMP0001")
        self.assertIsNone(d.punched_at)
        self.assertEqual(d.status, "PRESENT")
        self.assertEqual(m.PunchInIn(emp_code="X", punched_at=m.EPOCH_MS_MIN).punched_at, m.EPOCH_MS_MIN)
        self.assertEqual(m.PunchInIn(emp_code="X", punched_at=m.EPOCH_MS_MAX).punched_at, m.EPOCH_MS_MAX)

    def test_punch_in_strict_epoch_matrix(self):
        from pydantic import ValidationError
        for v in (1783312500, 99_999_999_999, 0, -5, m.EPOCH_MS_MAX + 1, 1783312500000.0, 1.5e12,
                  "1783312500000", "banana", True, False, None, [], {}):
            with self.subTest(v=v), self.assertRaises(ValidationError):
                m.PunchInIn(emp_code="EMP0001", punched_at=v)

    def test_punch_in_status_matrix(self):
        from pydantic import ValidationError
        for s in ("ABSENT", "LEAVE", "present", "", None, 1, "HALF_DAY"):
            with self.subTest(s=s), self.assertRaises(ValidationError):
                m.PunchInIn(emp_code="EMP0001", status=s)
        for s in ("PRESENT", "WFH", "ON_DUTY"):
            self.assertEqual(m.PunchInIn(emp_code="EMP0001", status=s).status, s)

    def test_punch_in_emp_code_must_be_string(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            m.PunchInIn(emp_code=1)


# ---------------------------------------------------------------- HTTP-level 422 (need httpx; no MongoDB touched)
@unittest.skipUnless(HAVE_HTTP, "fastapi TestClient/httpx not installed: NOT EXECUTED")
class TestHttp422(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.c = TestClient(m.app)  # not used as a context manager -> lifespan (indexes) does not run

    def test_punch_in_bad_bodies_are_422_before_any_db_access(self):
        for body in ({"emp_code": "EMP0001", "punched_at": 1783312500},
                     {"emp_code": "EMP0001", "punched_at": 1783312500000.5},
                     {"emp_code": "EMP0001", "punched_at": "1783312500000"},
                     {"emp_code": "EMP0001", "punched_at": True},
                     {"emp_code": "EMP0001", "punched_at": 4102444800001},
                     {"emp_code": "EMP0001", "status": "ABSENT"},
                     {"emp_code": "EMP0001", "status": "LEAVE"}):
            with self.subTest(body=body):
                r = self.c.post("/attendance/punch-in", json=body)
                self.assertEqual(r.status_code, 422)
                self.assertIsInstance(r.json()["detail"], list)

    def test_create_employee_bad_bodies_are_422(self):
        good = dict(emp_code="EMP0001", name="A", email="a@b.co", department="D", joined_on="2026-01-05")
        for over in (dict(emp_code="EMP1"), dict(email="nope"), dict(joined_on="2026-02-30"),
                     dict(shift_start="10:00", shift_end="10:00"), dict(shift_start="24:61"), dict(name="")):
            with self.subTest(over=over):
                r = self.c.post("/employees", json={**good, **over})
                self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
