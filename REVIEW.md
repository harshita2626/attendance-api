# REVIEW.md

## Problems found in the starter `app/main.py`

### 1. Duplicate checks done in Python (`create_employee`, `punch_in`)

- **Wrong:** It looks for an existing record with `find_one`, then inserts. Two requests at the same moment can both pass the check. There were no indexes at all.
- **Notice:** Send 10 identical requests in parallel. More than one gets 201.
- **Fix:** Unique indexes on `emp_code` and on `(emp_code, date)`, created at startup. The code catches `DuplicateKeyError` and returns 409.

### 2. `punch_in` crashes for an unknown employee

- **Wrong:** `emp` can be `None`, so `emp["shift_start"]` raises an error.
- **Notice:** Punch in with `EMP9999` and get a 500.
- **Fix:** Return 404 when the employee does not exist.

### 3. Wrong time zone and attendance date (`punch_in`)

- **Wrong:** `datetime.fromtimestamp` uses the server's local time. The date should be the IST date (R1), and overnight shifts were not handled. Microseconds were kept.
- **Notice:** Run the server on a machine that is not in IST and the dates and late minutes are wrong.
- **Fix:** Use UTC-aware datetimes, convert to IST for the date, handle overnight shifts, and cut times to whole seconds. The client uses `tz_aware=True`.

### 4. `compute_late_minutes` breaks rule R2

- **Wrong:** The minutes are cut to a whole number before the check `> 10`, so 09:40:01 counts as 0 instead of 10. It also builds the shift start from the punch-in's own date.
- **Notice:** Shift 09:30, punch-in 09:40:01 gives 0.
- **Fix:** Work in seconds, count late only when over 600 seconds, and use the shift start of the attendance date.

### 5. `compute_work_hours` uses `round()` (R4)

- **Wrong:** Python's `round` on floats is not half-up.
- **Notice:** 7.125 hours (25650 seconds) gives 7.12, but half-up gives 7.13.
- **Fix:** Use `Decimal` with `ROUND_HALF_UP`.

### 6. `compute_overtime` has no 30-minute minimum (R3)

- **Wrong:** Any minute after shift end counts. Overnight shifts end the next day, which was not handled.
- **Notice:** Punch out 5 minutes after shift end and overtime is 5, not 0.
- **Fix:** Return 0 below 30 minutes, and move the shift end to the next day for overnight shifts.

### 7. `punched_at` is not validated

- **Wrong:** It accepts any integer, including seconds-looking values. `if body.punched_at` treats 0 as "not given".
- **Notice:** Send `punched_at: 1783312500` (seconds) and it is accepted with a wrong date.
- **Fix:** A strict integer within the millisecond range (422 otherwise), and checks against `None`.

### 8. No input validation (`EmployeeIn`, `PunchInIn`)

- **Wrong:** `emp_code`, email, shift times and `joined_on` accept anything. `status` can be any string.
- **Notice:** `joined_on: "2026-02-31"` or equal shift times are accepted.
- **Fix:** Patterns, a real-date check, a rule that the shifts differ, and a `Literal` for `status`.

### 9. Times and `_id` returned in the wrong form

- **Wrong:** Datetimes are returned as datetimes, not epoch milliseconds. `punch_in` and `list_attendance` return the MongoDB id as `id`.
- **Notice:** Compare the responses with `openapi.yaml`.
- **Fix:** A `to_api` function turns datetimes into milliseconds and drops `_id`.

### 10. `list_employees` pagination is wrong (R10)

- **Wrong:** `skip = page * page_size` skips the first page. `total` counts all employees, not the filtered ones. No sort and no limits on `page` or `page_size`.
- **Notice:** `page=1` returns the second page of results. `department=Sales` shows the total of all employees.
- **Fix:** `skip = (page-1) * page_size`, `count_documents` on the same filter, sort by `emp_code`, and limits on the parameters.

### 11. `list_attendance` loads everything into memory

- **Wrong:** It reads the whole collection, then sorts and pages in Python. That is far too slow at 100,000 records. The sort has no tiebreak, and dates are not validated.
- **Notice:** Time the endpoint on a large collection. `date_from` after `date_to` is accepted.
- **Fix:** Use the database for sort, skip, limit and count, and add indexes. Sort by date then `emp_code`. Validate dates and reject a reversed range with 422.

### 12. Seeded records can miss fields

- **Wrong:** Records may lack `half_day` or `history`, and this was not handled.
- **Notice:** Read a seeded record without those fields.
- **Fix:** `attendance_to_api` fills in null, 0, false and `[]`.

### 13. `/health` always says ok

- **Wrong:** It does not check MongoDB.
- **Notice:** Stop MongoDB and `/health` still returns 200.
- **Fix:** Ping MongoDB and return 503 if it fails.

### 14. Connection and startup

- **Wrong:** No indexes were created at startup, and the client returned naive datetimes.
- **Fix:** A startup function creates all indexes (it is safe to run again), and the client uses `tz_aware=True`.

## Examined and fine

- `load_dotenv()` does not override real environment variables, which is what the task asks.
- `MONGO_URI` and `MONGO_DB` are read from the environment, not hard-coded.
- The `{items, total, page, page_size}` list shape and the 201 on create match the contract.
