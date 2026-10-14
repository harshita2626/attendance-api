# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run

    pip install -r requirements.txt
    export MONGO_URI=mongodb://localhost:27017
    export MONGO_DB=attendance_db
    uvicorn app.main:app --port 8000

On Windows PowerShell, use `$env:MONGO_URI="mongodb://localhost:27017"` instead of `export`.
Indexes are created automatically at startup. Open http://localhost:8000/docs to try the API.

## Notes

- Python 3.11+ and MongoDB 6.0+ (tested on MongoDB 8.0).
- The analytics endpoints are MongoDB aggregation pipelines
  (`$lookup`, `$setWindowFields`, `$densify`).
- `GET /admin/explain/{endpoint}` returns explain output with executionStats.
- Review notes are in `REVIEW.md` and design decisions are in `DECISIONS.md`.
- All endpoints are implemented. Nothing is missing.
