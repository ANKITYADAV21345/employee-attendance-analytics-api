# DECISIONS.md

1. **Indexes.**
   - `employees`: `{ emp_code: 1 }` (unique) enforces business key uniqueness; `{ department: 1, joined_on: 1 }` accelerates department lookups and headcount filtering.
   - `attendance_logs`: `{ emp_code: 1, date: 1 }` (unique) guarantees single record per day and race-safe punch-ins; `{ date: -1, emp_code: 1 }` serves list pagination/sorting without in-memory sorts; `{ date: 1, status: 1, late_minutes: 1 }` optimizes aggregation pipelines.
   - **Rejected**: Index on `history.at` because queries never filter by audit history timestamps.

2. **Punch-in race.**
   - When two identical punch-in requests for the same `emp_code` and attendance date arrive simultaneously, both pass application validation and issue `db.attendance_logs.insert_one()`.
   - MongoDB's atomic unique index on `("emp_code", "date")` accepts the first insert and immediately raises `DuplicateKeyError` for the second.
   - The application catches `DuplicateKeyError` and returns a `409 Conflict` response. No check-then-insert race window exists.

3. **Ties.**
   - The leaderboard uses standard competition ranking (`$rank`), so tied total late minutes share equal ranks (e.g., 1, 2, 2, 4).
   - Filtering `{ rank: { $lte: limit } }` occurs after ranking. If two employees tie at rank `limit`, both are included, returning more than `limit` rows ordered by `total_late_minutes` descending then `emp_code` ascending.

4. **Headcount.**
   - The department summary aggregation pipeline starts from the `employees` collection (filtered by `joined_on <= end_of_month`), grouping by department to establish exact headcount.
   - It then `$lookup`s attendance logs for the month. Employees with zero logs still contribute to the department's headcount and report zero/null attendance metrics.

5. **100x Data Scalability.**
   - At 100x scale (~10 million records), partition `attendance_logs` by month/date range or shard by `emp_code` + `date`.
   - Pre-aggregate daily/monthly metrics asynchronously via change streams into summary collections rather than running full pipeline aggregations on demand.
