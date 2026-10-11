# DECISIONS.md

1. **Indexes.** employees: unique `emp_code` (uniqueness, lookups, ordering) and `(department, emp_code)` (department filter, analytics start). attendance_logs: unique `(emp_code, date)` (one record per day, race-safe punch-in, PATCH address, analytics lookups); `(date -1, emp_code)` (default list order, month ranges); `(status, date -1, emp_code)` (status filter); `(emp_code, punch_in -1)` (punch-out lookup). Not added: `(department, joined_on)`, since `joined_on` only filters employees already narrowed by department.

2. **Punch-in race.** Both requests find the employee, build the record and call `insert_one`; there is no check-then-insert. The unique `(emp_code, date)` index lets one insert succeed (201); the other raises `DuplicateKeyError`, answered 409.

3. **Ties.** Ranks come from `$setWindowFields` `$rank` on total late minutes, descending (1, 2, 2, 4). `limit` filters `rank <= limit` after ranking, so everyone tied at the cutoff is returned; rows are ordered by total, then `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on` on or before the month end), joins each employee's logs and groups by department without unwinding, so employees with no logs count once.

5. **At 100x.** I would benchmark the list endpoints, optimize expensive total-count queries, and consider keyset pagination for deep-page access if the API contract allowed cursor-based navigation. This would reduce the cost of accessing later pages without silently changing the existing page-number contract.

Phase-specific notes: `PHASE_NOTES.md`.
