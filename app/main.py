"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real env vars take priority)

Part A: starter reviewed and fixed (see REVIEW.md).
Part B: punch-out, regularize, 4 analytics endpoints (MongoDB aggregation pipelines).
Part C: /admin/explain/{endpoint} (executionStats) and DECISIONS.md.
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # python-dotenv does NOT override real environment variables

# tz_aware=True: datetimes read from MongoDB carry UTC instead of being naive
client = MongoClient(
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.environ.get("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def create_indexes():
    emp = db.employees
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # list employees by department, sorted by emp_code
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # department summary: "joined on or before the end of the month" (R9), with / without department
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")

    logs = db.attendance_logs
    # one record per (employee, date): this is what makes punch-in race-safe; also serves the
    # employee monthly summary, the trend ($in emp_code + date range) and the $lookup joins
    logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    # list endpoint sorts by date desc, emp_code asc; the late leaderboard filters by date range
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="status_date_emp",
    )
    # punch-out: latest record of an employee with punch_in <= punched_at
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")


@asynccontextmanager
async def lifespan(_app):
    create_indexes()  # idempotent: same spec + name is a no-op
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers (R1 - R5)
# --------------------------------------------------------------------------- #
EPOCH_MIN_MS = 100_000_000_000      # below this it "looks like seconds" -> 422
EPOCH_MAX_MS = 4_102_444_800_000    # 2100-01-01
EpochMs = Annotated[StrictInt, Field(ge=EPOCH_MIN_MS, le=EPOCH_MAX_MS)]  # no floats/strings/bools


def ms_to_dt(ms) -> datetime:
    """Epoch millis (int only) -> aware UTC datetime, truncated to whole seconds (R1)."""
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise ValueError("timestamp must be an integer in epoch milliseconds")
    if not EPOCH_MIN_MS <= ms <= EPOCH_MAX_MS:
        raise ValueError("timestamp out of range (epoch milliseconds expected)")
    return datetime.fromtimestamp(ms // 1000, tz=UTC)  # // truncates the sub-second part


def check_date(value: Optional[str], name: str):
    """Real calendar date YYYY-MM-DD or 422 (pattern alone accepts 2026-02-31)."""
    if value is None:
        return
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid YYYY-MM-DD date")


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # defensive: treat naive as UTC
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: IST calendar date of punch-in; overnight early punch-ins belong to the previous day."""
    local = punch_in.astimezone(IST)
    overnight = shift_end <= shift_start  # "HH:MM" strings compare correctly
    if overnight and local.strftime("%H:%M") < shift_end:
        return (local - timedelta(days=1)).date()
    return local.date()


def shift_bounds(d: date_cls, shift_start: str, shift_end: str):
    sh, sm = _hm(shift_start)
    eh, em = _hm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)  # overnight shift ends the next calendar day
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; floor minutes from start."""
    secs = int((punch_in - shift_start_dt).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: floor minutes after shift end, counted only if >= 30."""
    mins = int((punch_out - shift_end_dt).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal avoids float/banker's rounding)."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def is_half_day(work_hours: float) -> bool:
    """R5: uses the rounded work_hours."""
    return Decimal(str(work_hours)) < Decimal("4.50")


def to_api(obj):
    """Mongo -> API: drop _id, datetimes -> epoch millis (recursive, covers history)."""
    if isinstance(obj, datetime):
        return dt_to_ms(obj)
    if isinstance(obj, dict):
        return {k: to_api(v) for k, v in obj.items() if k != "_id"}
    if isinstance(obj, list):
        return [to_api(v) for v in obj]
    return obj


def attendance_to_api(doc: dict) -> dict:
    # seeded/legacy records may omit these (DATA_MODEL.md): missing = null / 0 / false / []
    for key, default in (("punch_in", None), ("punch_out", None), ("work_hours", None),
                         ("late_minutes", 0), ("overtime_minutes", 0), ("half_day", False)):
        doc.setdefault(key, default)
    doc["history"] = doc.get("history") or []
    return to_api(doc)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
YMD = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"   # month 01-12 is enforced by FastAPI -> 422


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=YMD)

    @field_validator("joined_on")
    @classmethod
    def must_be_real_date(cls, v):
        date_cls.fromisoformat(v)  # ValueError -> 422 (e.g. 2026-02-31)
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6


class RegularizeIn(BaseModel):
    # derived fields (work_hours, late_minutes, ...) are not declared, so a client's copy is ignored
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "database unavailable")


@app.post("/employees", status_code=201, responses={409: {"description": "emp_code already exists"}})
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index is the real duplicate guard
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)  # total reflects the filter (R10)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [to_api(e) for e in cursor], "total": total,
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201,
          responses={404: {"description": "employee not found"},
                     409: {"description": "already punched in for this date"}})
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    try:
        ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    except ValueError as e:
        raise HTTPException(422, str(e))

    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    start, _ = shift_bounds(d, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) -> exactly one 201
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_to_api(doc)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]  # emp_code = stable tiebreak


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """The filter of GET /attendance. Shared with /admin/explain so both build the same query."""
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=YMD),
    date_to: Optional[str] = Query(None, pattern=YMD),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    check_date(date_from, "date_from")
    check_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = attendance_filter(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [attendance_to_api(d) for d in cursor], "total": total,
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-out",
          responses={404: {"description": "employee or punch-in not found"},
                     409: {"description": "already punched out"}})
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in <= punched_at (works across midnight: we match on the
    # punch_in instant, not on the calendar date). Null punch_in (ABSENT/LEAVE) never matches $lte.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    p_in = rec["punch_in"]
    if ts <= p_in or ts - p_in > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    _, end = shift_bounds(date_cls.fromisoformat(rec["date"]), emp["shift_start"], emp["shift_end"])
    hours = compute_work_hours(p_in, ts)
    updates = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(ts, end),
        "half_day": is_half_day(hours),
    }
    # compare-and-set: the filter includes punch_out == null, so only ONE of two simultaneous
    # punch-outs can match; the loser sees matched_count == 0 -> 409.
    res = db.attendance_logs.update_one(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_out": None},
        {"$set": updates},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(updates)
    return attendance_to_api(rec)


# --------------------------------------------------------------------------- #
# PATCH /attendance/{emp_code}/{date}  (regularization)
# --------------------------------------------------------------------------- #
def plan_regularization(emp: dict, rec: dict, body: RegularizeIn):
    """Pure function (no DB): validate the request against the stored record, recompute the
    derived fields and work out which fields really changed. Returns (new_values, changes)."""
    for f in ("status", "punch_in", "punch_out"):          # "omitted" is fine, explicit null is not
        if f in body.model_fields_set and getattr(body, f) is None:
            raise HTTPException(422, f"{f} must not be null")

    new_status = body.status or rec["status"]
    if new_status not in PRESENT_STATUSES:                  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"{new_status} cannot carry punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if body.punch_in is not None:                       # must stay on this attendance date (R1)
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != rec["date"]:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if (body.punch_in is not None or body.punch_out is not None) and new_out is not None:
            if new_out <= new_in or new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # recompute derived fields from the FINAL values (R2 - R5)
    late, hours, overtime, half = 0, None, 0, False
    if new_status in PRESENT_STATUSES:
        start, end = shift_bounds(date_cls.fromisoformat(rec["date"]),
                                  emp["shift_start"], emp["shift_end"])
        late = compute_late_minutes(new_in, start)
        if new_out is not None:
            hours = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, end)
            half = is_half_day(hours)

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": hours,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    old = {"status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes", 0),
           "overtime_minutes": rec.get("overtime_minutes", 0), "half_day": rec.get("half_day", False)}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")
    return new, changes


@app.patch("/attendance/{emp_code}/{date}",
           responses={404: {"description": "employee or record not found"},
                      409: {"description": "record modified concurrently"}})
def regularize(body: RegularizeIn, emp_code: str, date: str = Path(pattern=YMD)):
    check_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    new, changes = plan_regularization(emp, rec, body)
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    old_history = rec.get("history") or []

    # Optimistic concurrency, enforced by ONE atomic update on ONE document: it only applies if the
    # record is still exactly what we read (same status, punches and history length). Otherwise
    # someone else changed it in between -> matched_count == 0 -> 409, and nothing is lost.
    res = db.attendance_logs.update_one(
        {
            "emp_code": emp_code, "date": date,
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),      # None also matches a missing field
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(old_history)]},
        },
        {"$set": new, "$push": {"history": entry}},   # fields + audit entry in the same atomic write
    )
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    rec.update(new)
    rec["history"] = old_history + [entry]
    return attendance_to_api(rec)


# --------------------------------------------------------------------------- #
# Analytics: shared helpers
# --------------------------------------------------------------------------- #
WEEKDAYS = [2, 3, 4, 5, 6]  # MongoDB $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6


def half_up(x, places: int = 2) -> float:
    """R8: half-up rounding. round(.., 9) first removes float noise such as 27.130000000000003."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(round(float(x), 9))).quantize(q, ROUND_HALF_UP))


def month_range(month: str):
    """'2026-07' -> (2026-07-01, 2026-08-01): first day of the month and first day of the next."""
    y, m = int(month[:4]), int(month[5:7])
    first = date_cls(y, m, 1)
    nxt = date_cls(y + 1, 1, 1) if m == 12 else date_cls(y, m + 1, 1)
    return first, nxt


def working_days_between(start: date_cls, end_excl: date_cls) -> int:
    """Mon-Fri days in [start, end_excl). No holiday calendar (R7)."""
    n, d = 0, start
    while d < end_excl:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# Pipeline building blocks (expressions evaluated per attendance record)
IS_PRESENT = {"$in": ["$status", list(PRESENT_STATUSES)]}                          # R6
HALF_WEIGHT = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}                   # R5 (missing = false)
PRESENT_ANY_DAY = {"$cond": [IS_PRESENT, HALF_WEIGHT, 0]}                          # trend: any weekday
PRESENT_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, {"$in": ["$_dow", WEEKDAYS]}]}, HALF_WEIGHT, 0]}  # R7
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
ADD_DOW = {"$addFields": {"_dow": {"$dayOfWeek": {
    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}}}


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_employee_monthly(emp_code: str, month: str) -> list:
    first, nxt = month_range(month)
    return [
        # equality on emp_code + range on date -> index uniq_emp_date
        {"$match": {"emp_code": emp_code,
                    "date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
        ADD_DOW,
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_WEEKDAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary(month: str, department: Optional[str]):
    """Starts from employees (so people with zero logs still count, R9) and joins each
    employee's logs for the month. Returns (pipeline, index hint for employees)."""
    first, nxt = month_range(month)
    emp_match = {"joined_on": {"$lt": nxt.isoformat()}}   # joined on/before the last day (R9)
    if department:
        emp_match["department"] = department
    hint = "dept_joined_on" if department else "joined_on"
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code", "foreignField": "emp_code",   # equality uses uniq_emp_date
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
                ADD_DOW,
                # one summary document per employee (or none when the employee has no logs)
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": PRESENT_WEEKDAY},
                    "wh_sum": {"$sum": {"$cond": [HAS_HOURS, "$work_hours", 0]}},
                    "wh_cnt": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "m",
        }},
        # keep employees whose "m" is empty -> they still add 1 to headcount
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$m.present_days"},
            "wh_sum": {"$sum": "$m.wh_sum"},
            "wh_cnt": {"$sum": "$m.wh_cnt"},      # average over RECORDS, not per-employee averages
            "late_count": {"$sum": "$m.late_count"},
            "total_late_minutes": {"$sum": "$m.total_late_minutes"},
            "leave_count": {"$sum": "$m.leave_count"},
            "on_duty_count": {"$sum": "$m.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]
    return pipeline, hint


def build_leaderboard(month: str, limit: int, department: Optional[str]) -> list:
    first, nxt = month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                      # logs of unknown employees are dropped
    ]
    if department:                                # rank inside the department only
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4 (ties share, next rank skipped)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},    # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_roster(department: str):
    """(emp_codes, sorted joined_on strings) of a department."""
    emps = list(db.employees.find({"department": department},
                                  {"_id": 0, "emp_code": 1, "joined_on": 1}))
    return [e["emp_code"] for e in emps], sorted(e["joined_on"] for e in emps)


def build_trend(codes: list, joined: list, d_from: date_cls, d_to: date_cls) -> list:
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)  # bound is exclusive
    return [
        {"$match": {"emp_code": {"$in": codes},
                    "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": PRESENT_ANY_DAY},
                    "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one document per calendar day in [from, to]
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "dow": {"$dayOfWeek": "$d"},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {
            "is_working_day": {"$in": ["$dow", WEEKDAYS]},
            # R9 per day: employees with joined_on <= this day (ISO strings compare correctly)
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j",
                                                "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        # attendance_rate x 10000 as an integer, half-up: floor((2*present*10000 + h) / (2*h))
        # (integer arithmetic avoids float/banker's-rounding surprises; $round is banker's)
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": [{"$multiply": ["$present_count", 2]}, 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        # 7-day window = current row + up to 6 preceding rows inside the range; nulls are ignored
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "w_sum": {"$sum": "$rate_int", "window": {"documents": [-6, 0]}},
            "w_cnt": {"$sum": {"$cond": [{"$isNumber": "$rate_int"}, 1, 0]},
                      "window": {"documents": [-6, 0]}},
        }}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_int"},
                                          {"$divide": ["$rate_int", 10000]}, None]},
            # mean of the (rounded) rates, half-up to 4 decimals: floor((2*sum + n) / (2*n)) / 10000
            "moving_avg_7d": {"$cond": [{"$gt": ["$w_cnt", 0]}, {"$divide": [
                {"$floor": {"$divide": [
                    {"$add": [{"$multiply": ["$w_sum", 2]}, "$w_cnt"]},
                    {"$multiply": [2, "$w_cnt"]}]}}, 10000]}, None]},
        }},
    ]


def parse_trend_range(date_from: str, date_to: str):
    check_date(date_from, "from")
    check_date(date_to, "to")
    d_from, d_to = date_cls.fromisoformat(date_from), date_cls.fromisoformat(date_to)
    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    if (d_to - d_from).days + 1 > 92:          # at most 92 calendar days (rows), inclusive
        raise HTTPException(422, "range must not exceed 92 days")
    return d_from, d_to


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly",
         responses={404: {"description": "employee not found"}})
def employee_monthly_summary(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    agg = next(iter(db.attendance_logs.aggregate(build_employee_monthly(emp_code, month))), None) or {}

    first, nxt = month_range(month)
    start = max(first, date_cls.fromisoformat(emp["joined_on"]))     # mid-month joiner (R7)
    working_days = working_days_between(start, nxt)
    present_days = float(agg.get("present_days", 0))
    pct = None
    if working_days > 0:
        pct = float((Decimal(str(present_days)) / Decimal(working_days) * 100)
                    .quantize(Decimal("0.01"), ROUND_HALF_UP))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def departments_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    pipeline, hint = build_department_summary(month, department)
    items = []
    for r in db.employees.aggregate(pipeline, hint=hint):
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": half_up(r["wh_sum"] / r["wh_cnt"]) if r["wh_cnt"] else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def leaderboard_late(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(build_leaderboard(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend",
         responses={404: {"description": "unknown department"}})
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from", pattern=YMD),
    date_to: str = Query(..., alias="to", pattern=YMD),
):
    d_from, d_to = parse_trend_range(date_from, date_to)
    codes, joined = department_roster(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    items = list(db.attendance_logs.aggregate(build_trend(codes, joined, d_from, d_to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C: explain
# --------------------------------------------------------------------------- #
def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, "missing required query parameter(s): " + ", ".join(missing))


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=YMD),
    date_to: Optional[str] = Query(None, pattern=YMD),
    status: Optional[Status] = None,
    trend_from: Optional[str] = Query(None, alias="from", pattern=YMD),
    trend_to: Optional[str] = Query(None, alias="to", pattern=YMD),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs explain(executionStats) on the same query/pipeline the real endpoint builds."""
    if endpoint == "attendance_list":
        check_date(date_from, "date_from")
        check_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": attendance_filter(emp_code, date_from, date_to, status),
               "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        coll = "attendance_logs"
        cmd = {"aggregate": coll, "pipeline": build_employee_monthly(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        _need(month=month)
        coll = "employees"
        pipeline, hint = build_department_summary(month, department)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
    elif endpoint == "late_leaderboard":
        _need(month=month)
        coll = "attendance_logs"
        cmd = {"aggregate": coll, "pipeline": build_leaderboard(month, limit, department), "cursor": {}}
    else:  # department_trend
        _need(department=department, **{"from": trend_from, "to": trend_to})
        d_from, d_to = parse_trend_range(trend_from, trend_to)
        codes, joined = department_roster(department)
        if not codes:
            raise HTTPException(404, "unknown department")
        coll = "attendance_logs"
        cmd = {"aggregate": coll, "pipeline": build_trend(codes, joined, d_from, d_to), "cursor": {}}

    raw = db.command("explain", cmd, verbosity="executionStats")
    # explain output contains BSON types (Timestamp, ...): round-trip through extended JSON
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(raw))}