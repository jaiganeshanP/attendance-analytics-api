# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` on logs: guards the punch-in race and serves per-employee queries and the analytics `$lookup`. `(date desc, emp_code)` serves the list sort and month scans. `(status, date desc, emp_code)` serves the status filter. `(emp_code, punch_in desc)` serves punch-out. On employees: unique `emp_code`, `(department, emp_code)` and `joined_on`. Rejected: copying `department` onto each log plus an index; it duplicates data that can go stale.
2. **Punch-in race.** Both requests read the employee and call `insert_one`. The unique index lets MongoDB accept exactly one; the other gets `DuplicateKeyError`, which I return as 409. There is no read before the write.
3. **Ties.** The pipeline ranks with `$rank`, so ties share a rank and the next is skipped. `limit` filters `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that exceeds `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s logs. An employee with no logs still counts once, with zeros.
5. **100x data.** Replace skip/limit paging with keyset paging, and precompute monthly aggregates incrementally instead of scanning all logs per request.
