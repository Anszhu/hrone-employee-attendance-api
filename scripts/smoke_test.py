"""End-to-end HTTP smoke test for the Employee Attendance & Analytics API.

Standard library only. Run it AFTER starting the server yourself:

    $env:MONGO_DB = "attendance_smoke"            # PowerShell: a throw-away database
    uvicorn app.main:app --port 8000
    # second terminal:
    python scripts/smoke_test.py --db-name attendance_smoke --yes

What it does: creates two uniquely named employees in a uniquely named department (EMP9xxxxx,
"Smoke-xxxxxx") and records one September-2026 attendance day for each, then exercises every endpoint.
It never deletes anything, so the records stay in the database: point the server at a disposable
MONGO_DB. The script cannot see the server's database name; --db-name only makes you state it, and
--yes is required before any data is written.

Exit code 0 only if every check passed. The /admin/explain index check is reported as INFO (a tiny
dataset says little about plans); the 100k-record check belongs to the TEST_MONGO_URI explain tests.
"""
import argparse
import json
import random
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
failures = []
checks = 0


def ms(y, mo, d, h, mi, s=0):
    """Epoch milliseconds of an IST wall-clock time."""
    dt = datetime(y, mo, d, h, mi, s, tzinfo=IST)
    return int(dt.timestamp() * 1000)


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode(errors="replace")


def check(label, condition, detail=""):
    global checks
    checks += 1
    print(("  PASS  " if condition else "  FAIL  ") + label + ("" if condition else f"   -> {detail}"))
    if not condition:
        failures.append(label)


def expect(base, label, method, path, status, body=None):
    code, payload = call(base, method, path, body)
    check(f"{label}: {method} {path.split('?')[0]} -> {status}", code == status, f"got {code}: {str(payload)[:200]}")
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--db-name", required=True, help="the MONGO_DB the server was started with")
    ap.add_argument("--yes", action="store_true", help="confirm the database is disposable")
    args = ap.parse_args()
    if not args.yes:
        sys.exit(f"Refusing to write data. Re-run with --yes to confirm that database '{args.db_name}' is disposable.")
    base = args.base.rstrip("/")
    print(f"Target {base}, database (as stated by you): {args.db_name}")

    n = random.randint(900000, 999999)
    code_a, code_b = f"EMP{n}", f"EMP{n - 1}"
    dept = f"Smoke-{n}"
    first_day, last_day = date(2026, 9, 1), date(2026, 9, 30)
    working_days = sum(1 for i in range(30) if (first_day + timedelta(days=i)).weekday() < 5)   # 22
    print(f"Employees {code_a}, {code_b}; department {dept}\n")

    print("1. health")
    check("GET /health -> 200 ok", call(base, "GET", "/health") == (200, {"status": "ok"}))

    print("2-4. employees: create, list, duplicate")
    emp = {"emp_code": code_a, "name": "Smoke A", "email": "a@example.com", "department": dept,
           "joined_on": "2026-09-01"}
    created = expect(base, "create A", "POST", "/employees", 201, emp)
    check("response has defaults and epoch-ms created_at",
          isinstance(created, dict) and created.get("shift_start") == "09:30" and created.get("shift_end") == "18:30"
          and isinstance(created.get("created_at"), int) and created["created_at"] > 100_000_000_000
          and "_id" not in created, created)
    expect(base, "create B", "POST", "/employees", 201, {**emp, "emp_code": code_b, "name": "Smoke B"})
    page = expect(base, "list by department", "GET", f"/employees?department={dept}&page=1&page_size=10", 200)
    check("list total 2, sorted by emp_code",
          isinstance(page, dict) and page.get("total") == 2 and [i["emp_code"] for i in page["items"]] == sorted([code_a, code_b]),
          page)
    expect(base, "duplicate emp_code", "POST", "/employees", 409, emp)

    print("5-7. punch-in, retrieval, punch-out (Mon 2026-09-07, shift 09:30-18:30)")
    pin = expect(base, "punch-in A 09:45:30", "POST", "/attendance/punch-in", 201,
                 {"emp_code": code_a, "punched_at": ms(2026, 9, 7, 9, 45, 30)})
    check("date 2026-09-07, late 15, punch_out/work_hours null, history []",
          isinstance(pin, dict) and pin.get("date") == "2026-09-07" and pin.get("late_minutes") == 15
          and pin.get("punch_out") is None and pin.get("work_hours") is None and pin.get("history") == []
          and pin.get("status") == "PRESENT" and "_id" not in pin, pin)
    expect(base, "duplicate punch-in", "POST", "/attendance/punch-in", 409,
           {"emp_code": code_a, "punched_at": ms(2026, 9, 7, 9, 50)})
    expect(base, "punch-in B 10:00:00 (late 30, left open)", "POST", "/attendance/punch-in", 201,
           {"emp_code": code_b, "punched_at": ms(2026, 9, 7, 10, 0)})
    listed = expect(base, "attendance for A", "GET", f"/attendance?emp_code={code_a}&date_from=2026-09-01&date_to=2026-09-30", 200)
    check("one record, total 1", isinstance(listed, dict) and listed.get("total") == 1 and len(listed["items"]) == 1, listed)
    pout = expect(base, "punch-out A 19:05:00", "POST", "/attendance/punch-out", 200,
                  {"emp_code": code_a, "punched_at": ms(2026, 9, 7, 19, 5)})
    check("work_hours 9.33 (9h19m30s, exact half-up), overtime 35, half_day false",
          isinstance(pout, dict) and pout.get("work_hours") == 9.33 and pout.get("overtime_minutes") == 35
          and pout.get("half_day") is False and pout.get("late_minutes") == 15, pout)
    expect(base, "second punch-out", "POST", "/attendance/punch-out", 409, {"emp_code": code_a, "punched_at": ms(2026, 9, 7, 19, 10)})

    print("8-9. regularization and history")
    reg = expect(base, "PATCH punch_in -> 09:35:00", "PATCH", f"/attendance/{code_a}/2026-09-07", 200,
                 {"punch_in": ms(2026, 9, 7, 9, 35), "reason": "biometric glitch", "regularized_by": "smoke.test"})
    ch = reg.get("history", [{}])[-1].get("changes", {}) if isinstance(reg, dict) else {}
    check("late 0, work_hours 9.5, one history entry with exactly punch_in / late_minutes / work_hours",
          isinstance(reg, dict) and reg.get("late_minutes") == 0 and reg.get("work_hours") == 9.5
          and len(reg.get("history", [])) == 1 and sorted(ch) == ["late_minutes", "punch_in", "work_hours"]
          and ch["late_minutes"] == {"from": 15, "to": 0}
          and ch["punch_in"] == {"from": ms(2026, 9, 7, 9, 45, 30), "to": ms(2026, 9, 7, 9, 35)}, reg)
    expect(base, "PATCH that changes nothing", "PATCH", f"/attendance/{code_a}/2026-09-07", 422,
           {"punch_in": ms(2026, 9, 7, 9, 35), "reason": "same again", "regularized_by": "smoke.test"})
    expect(base, "PATCH unknown date", "PATCH", f"/attendance/{code_a}/2026-09-08", 404,
           {"status": "LEAVE", "reason": "no such record", "regularized_by": "smoke.test"})
    again = expect(base, "history persisted", "GET", f"/attendance?emp_code={code_a}", 200)
    check("history still has 1 entry after re-read", isinstance(again, dict) and len(again["items"][0]["history"]) == 1, again)

    print("10-13. analytics (month 2026-09)")
    mon = expect(base, "employee monthly A", "GET", f"/analytics/employees/{code_a}/monthly?month=2026-09", 200)
    check(f"working_days {working_days}, present_days 1.0, late 0, overtime 35, pct 4.55",
          isinstance(mon, dict) and mon.get("working_days") == working_days and mon.get("present_days") == 1.0
          and mon.get("late_count") == 0 and mon.get("total_overtime_minutes") == 35
          and mon.get("attendance_pct") == 4.55 and mon.get("leave_days") == 0, mon)
    summ = expect(base, "department summary", "GET", f"/analytics/departments/summary?month=2026-09&department={dept}", 200)
    row = (summ.get("items") or [{}])[0] if isinstance(summ, dict) else {}
    check("headcount 2, present_days 2.0, avg_work_hours 9.5, late_count 1 / 30 min",
          row.get("department") == dept and row.get("headcount") == 2 and row.get("present_days") == 2.0
          and row.get("avg_work_hours") == 9.5 and row.get("late_count") == 1 and row.get("total_late_minutes") == 30
          and row.get("leave_count") == 0 and row.get("on_duty_count") == 0, summ)
    lb = expect(base, "late leaderboard", "GET", f"/analytics/leaderboard/late?month=2026-09&department={dept}", 200)
    check("only B (30 min, rank 1); A corrected to 0 is excluded",
          isinstance(lb, dict) and lb.get("items") == [{"rank": 1, "emp_code": code_b, "name": "Smoke B", "department": dept,
                                                          "total_late_minutes": 30, "late_count": 1}], lb)
    tr = expect(base, "department trend 09-01..09-14", "GET", f"/analytics/departments/{dept}/trend?from=2026-09-01&to=2026-09-14", 200)
    items = tr.get("items", []) if isinstance(tr, dict) else []
    by = {i["date"]: i for i in items}
    check("exactly 14 rows, one per calendar day", len(items) == 14 and [i["date"] for i in items] == sorted(by), len(items))
    check("weekend 09-05: not working day, rate null",
          by.get("2026-09-05", {}).get("is_working_day") is False and by["2026-09-05"]["attendance_rate"] is None, by.get("2026-09-05"))
    check("09-01 working day, headcount 2, no records -> rate 0.0",
          by.get("2026-09-01", {}).get("headcount") == 2 and by["2026-09-01"]["attendance_rate"] == 0.0, by.get("2026-09-01"))
    d7 = by.get("2026-09-07", {})
    check("09-07: present_count 2.0, late_count 1, rate 1.0, moving_avg_7d 0.2",
          d7.get("present_count") == 2.0 and d7.get("late_count") == 1 and d7.get("attendance_rate") == 1.0
          and d7.get("moving_avg_7d") == 0.2, d7)

    print("14. validation and error responses")
    expect(base, "seconds-sized punched_at", "POST", "/attendance/punch-in", 422, {"emp_code": code_a, "punched_at": 1783312500})
    expect(base, "ABSENT via punch-in", "POST", "/attendance/punch-in", 422, {"emp_code": code_a, "status": "ABSENT"})
    expect(base, "unknown employee punch-in", "POST", "/attendance/punch-in", 404, {"emp_code": "EMP000001"})
    expect(base, "bad emp_code pattern", "POST", "/employees", 422, {**emp, "emp_code": "X1"})
    expect(base, "equal shift times", "POST", "/employees", 422, {**emp, "emp_code": f"EMP{n - 2}", "shift_start": "09:00", "shift_end": "09:00"})
    expect(base, "page_size 101", "GET", "/employees?page_size=101", 422)
    expect(base, "page 0", "GET", "/attendance?page=0", 422)
    expect(base, "date_from after date_to", "GET", "/attendance?date_from=2026-09-30&date_to=2026-09-01", 422)
    expect(base, "invalid month", "GET", f"/analytics/employees/{code_a}/monthly?month=2026-13", 422)
    expect(base, "monthly unknown employee", "GET", "/analytics/employees/EMP000001/monthly?month=2026-09", 404)
    expect(base, "trend range of 93 dates", "GET", f"/analytics/departments/{dept}/trend?from=2026-01-01&to=2026-04-03", 422)
    expect(base, "trend unknown department", "GET", "/analytics/departments/NoSuchDept/trend?from=2026-09-01&to=2026-09-07", 404)
    expect(base, "leaderboard limit 51", "GET", "/analytics/leaderboard/late?month=2026-09&limit=51", 422)

    print("INFO: /admin/explain (index use on this tiny dataset; not a pass/fail check)")
    for name, q in [("attendance_list", "date_from=2026-09-01&date_to=2026-09-30"),
                    ("employee_monthly", f"emp_code={code_a}&month=2026-09"),
                    ("department_summary", f"month=2026-09&department={dept}"),
                    ("late_leaderboard", "month=2026-09"),
                    ("department_trend", f"department={dept}&from=2026-09-01&to=2026-09-14")]:
        code, payload = call(base, "GET", f"/admin/explain/{name}?{q}")
        text = json.dumps(payload)
        print(f"  INFO  {name}: HTTP {code}, IXSCAN={'IXSCAN' in text}, COLLSCAN={'COLLSCAN' in text}")
        if code != 200:
            failures.append(f"explain {name} -> {code}")

    print(f"\n{checks - len([f for f in failures if not f.startswith('explain')])}/{checks} checks passed; "
          f"{len(failures)} failure(s)")
    for f in failures:
        print("  FAILED:", f)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
