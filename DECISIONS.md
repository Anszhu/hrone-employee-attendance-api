# DECISIONS.md

1. **Indexes.** The unique `emp_code` index identifies employees and prevents duplicate codes. The unique `(emp_code, date)` index prevents duplicate daily attendance records. The `(department, emp_code)` index supports department-filtered employee queries. The `(date, emp_code)` and `(status, date, emp_code)` indexes support attendance filtering and sorting, while `(emp_code, punch_in)` supports punch-out lookup. I avoided adding speculative indexes without evidence of a benefit.

2. **Punch-in race.** Concurrent requests may both attempt an insert, but the unique `(emp_code, date)` index permits only one daily record. The successful request returns 201; the conflicting request returns 409.

3. **Ties.** The leaderboard uses competition ranking. Employees tied at the cutoff rank are all returned because the limit applies to ranks, not row count. Employee code provides deterministic ordering afterward.

4. **Headcount.** The department summary starts from employees and joins attendance records. Eligible employees remain countable even when they have no attendance logs.

5. **Scaling.** At 100 times the data, I would benchmark realistic workloads, inspect execution statistics, and measure latency and memory use. I would tune indexes or introduce pre-aggregated daily summaries only where measurements demonstrate a benefit.
