# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` and `compute_overtime` | The starter logic used naive local-time assumptions and did not correctly handle the IST date boundary and overnight-shift rules. | Punch-in near midnight or overnight shifts gave the wrong attendance date and wrong late/overtime values. | I normalized all timestamps to UTC and converted them to IST before calculation, and I implemented overnight-shift logic according to R1-R3. |
| 2 | `compute_work_hours` | The starter used an unrounded float calculation and did not follow the assignment's half-up rounding rule. | Work-hour totals showed inconsistent decimals and failed boundary checks around 4.50 hours or 8.50 hours. | I used Decimal with ROUND_HALF_UP and rounded exactly to 2 decimals. |
| 3 | `list_employees` | The starter used `skip = page * page_size` instead of `(page - 1) * page_size`, so page 1 was skipped incorrectly. | Page 1 returned the wrong slice or missing records. | I corrected pagination to start at page 1 and return the proper offset. |
| 4 | `punch_in` | The starter did not validate employee existence and did not handle the same-day duplicate check cleanly. | Unknown employee or duplicate same-day punch-in could produce inconsistent outcomes. | I added explicit employee checks and duplicate detection with atomic insert protection. |
| 5 | `punch_out` | The starter did not enforce the 24-hour max and did not choose the correct most recent open punch-in for overnight shifts. | Punch-out after a long gap or across shifts produced invalid data. | I validated the interval and picked the latest valid open punch-in before computing the final derived values. |
| 6 | `regularize_attendance` | The starter did not recompute all derived fields after a correction and did not append a proper audit trail. | Correcting a record left stale values and no history. | I recomputed late_minutes, work_hours, overtime_minutes, and half_day after every change and appended a single history entry with actual changes only. |
| 7 | `/analytics/...` routes | The starter had no MongoDB aggregation or monthly summary logic. | The assignment's analytics endpoints were missing entirely. | I implemented employee monthly, department summary, leaderboard, and department-trend endpoints using database-driven logic. |
| 8 | `/admin/explain/{endpoint}` | No query-plan endpoint existed. | The grading checks require explain output for the main query. | I added an explain endpoint that returns the raw executionStats output for the named route. |
| 9 | Startup state | There were no database indexes defined. | Query plans would scan too much data and fail grader performance checks. | I create the required unique and filtered indexes at startup. |

Not a defect: I did not treat MongoDB `_id` as the external identifier; the assignment explicitly defines `emp_code` as the business key. I also did not recompute analytics from raw punch times in the monthly and department routes, because the rules say the stored derived values are authoritative.

## Verification

The final implementation was checked against a local MongoDB 8.0.12 server using temporary test databases. The checks covered monthly aggregation totals and rounding, department headcount including employees with no logs, leaderboard competition ties and cutoff behavior, trend gap-filling and the seven-calendar-row window, IST regularization and legacy history, input validation, and all five explain operations. Each explain plan contained `IXSCAN` and no `COLLSCAN` on the synthetic validation dataset.

The assignment's hidden dataset and grader were not available, so this local verification cannot guarantee grader results.
