# DECISIONS.md

1. **Indexes.** Unique indexes on `emp_code` and `(emp_code, date)` stop duplicates and serve the monthly, join and trend queries. `(date, emp_code)` serves the attendance list and the leaderboard date range. I also have department, status and punch-in indexes for list and punch-out. I rejected an index on `late_minutes`: the date range already narrows the data.

2. **Punch-in race.** Both requests call `insert_one` with the same `(emp_code, date)`. The unique index lets only one succeed. The winner gets 201, and the loser's duplicate-key error becomes 409. There is no check before the insert.

3. **Ties.** `$rank` runs before the limit, so tied employees share a rank and the next rank is skipped. The leaderboard returns everyone whose rank is within `limit`, so it can return more rows than `limit`.

4. **Headcount.** The pipeline starts from `employees` and joins logs with `$lookup`. `$unwind` keeps employees without logs, so they still count once. Only people who joined by the month's end are included.

5. **At 100x the data.** I would keep monthly totals per employee in a summary collection, updated on punch-in, punch-out and regularize, so analytics read small documents instead of every raw log.
