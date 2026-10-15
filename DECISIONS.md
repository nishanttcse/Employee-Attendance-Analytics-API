# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index enforces employee identity. A unique `attendance_logs.emp_code + date` index prevents duplicate daily records. Supporting indexes cover department, joined date, attendance date, late minutes, and employee punch-in lookups. I considered a `department + joined_on` index; current explain checks show the existing indexes are used.

2. **Punch-in race.** Concurrent requests can both pass the initial existence check, but the unique attendance index allows only one insert. The other insert raises `DuplicateKeyError`, which the API returns as 409.

3. **Ties.** The leaderboard uses competition ranking, so tied totals share a rank and the next rank is skipped. The limit filters by rank after ranking, returning every employee tied at the cutoff.

4. **Headcount.** Department summary starts with employees and looks up logs, so employees without logs still count. Trend fills every requested date and counts only employees who have joined by that day.

5. **At 100x data.** I would benchmark materialized daily department rollups for trend queries, balancing their faster reads against refresh complexity and storage.
