"""
Employee Attendance & Analytics API - Main Application

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""
import calendar
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Body
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument, errors

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def init_indexes():
    # employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def validate_epoch_ms(val: Any) -> int:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise HTTPException(422, "Timestamp must be an integer count of epoch milliseconds")
    if val < 100000000000 or val > 4102444800000:
        raise HTTPException(422, f"Epoch milliseconds out of range: {val}")
    return val


def epoch_ms_to_datetime(ts_ms: int) -> datetime:
    ts_sec = ts_ms // 1000
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def parse_time_str(time_str: str) -> tuple[int, int]:
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    h_start, m_start = parse_time_str(shift_start)
    h_end, m_end = parse_time_str(shift_end)
    return (h_end, m_end) <= (h_start, m_start)


def get_attendance_date(dt_utc: datetime, shift_start: str, shift_end: str) -> str:
    dt_ist = dt_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        h_end, m_end = parse_time_str(shift_end)
        if (dt_ist.hour, dt_ist.minute, dt_ist.second) < (h_end, m_end, 0):
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(dt_utc: datetime, attendance_date: str, shift_start: str) -> int:
    dt_ist = dt_utc.astimezone(IST)
    h_start, m_start = parse_time_str(shift_start)
    d_obj = datetime.fromisoformat(attendance_date).date()
    shift_start_dt = datetime.combine(d_obj, datetime.min.time()).replace(
        hour=h_start, minute=m_start, second=0, microsecond=0, tzinfo=IST
    )
    diff_sec = (dt_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(dt_out_utc: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    dt_out_ist = dt_out_utc.astimezone(IST)
    h_start, m_start = parse_time_str(shift_start)
    h_end, m_end = parse_time_str(shift_end)
    d_obj = datetime.fromisoformat(attendance_date).date()
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date = d_obj + timedelta(days=1)
    else:
        shift_end_date = d_obj

    shift_end_dt = datetime.combine(shift_end_date, datetime.min.time()).replace(
        hour=h_end, minute=m_end, second=0, microsecond=0, tzinfo=IST
    )
    diff_sec = (dt_out_ist - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(dt_in_utc: datetime, dt_out_utc: datetime) -> float:
    diff_sec = (dt_out_utc - dt_in_utc).total_seconds()
    hours_dec = Decimal(str(diff_sec)) / Decimal("3600")
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_employee(doc: dict) -> dict:
    if not doc:
        return doc
    d = dict(doc)
    d.pop("_id", None)
    d["created_at"] = datetime_to_epoch_ms(d.get("created_at"))
    return d


def format_attendance_record(doc: dict) -> dict:
    if not doc:
        return doc
    d = dict(doc)
    d.pop("_id", None)
    d["punch_in"] = datetime_to_epoch_ms(d.get("punch_in"))
    d["punch_out"] = datetime_to_epoch_ms(d.get("punch_out"))
    d["work_hours"] = d.get("work_hours")
    d["late_minutes"] = d.get("late_minutes", 0)
    d["overtime_minutes"] = d.get("overtime_minutes", 0)
    d["half_day"] = bool(d.get("half_day", False))

    formatted_history = []
    for h in d.get("history", []):
        entry = dict(h)
        entry["at"] = datetime_to_epoch_ms(entry.get("at"))
        changes = dict(entry.get("changes", {}))
        formatted_changes = {}
        for k, v in changes.items():
            if isinstance(v, dict) and ("from" in v or "to" in v):
                from_val = v.get("from")
                to_val = v.get("to")
                if k in ("punch_in", "punch_out"):
                    if isinstance(from_val, datetime):
                        from_val = datetime_to_epoch_ms(from_val)
                    if isinstance(to_val, datetime):
                        to_val = datetime_to_epoch_ms(to_val)
                formatted_changes[k] = {"from": from_val, "to": to_val}
            else:
                formatted_changes[k] = v
        entry["changes"] = formatted_changes
        formatted_history.append(entry)
    d["history"] = formatted_history
    return d


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @field_validator("shift_end")
    def validate_shift_times(cls, v, info):
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: str = "PRESENT"

    @field_validator("status")
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("Status for punch-in must be PRESENT, WFH, or ON_DUTY")
        return v

    @field_validator("punched_at")
    def validate_punched_at(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("punched_at")
    def validate_punched_at(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    def validate_punch_times(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")

    doc = body.model_dump()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except errors.DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        dt_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        dt_utc = datetime.now(timezone.utc).replace(microsecond=0)

    date_str = get_attendance_date(dt_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(dt_utc, date_str, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except errors.DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        dt_out_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        dt_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": dt_out_utc},
            "punch_out": None
        },
        sort=[("punch_in", -1)]
    )

    if not record:
        raise HTTPException(404, "No open punch-in found")

    dt_in_utc = record["punch_in"]
    if dt_in_utc.tzinfo is None:
        dt_in_utc = dt_in_utc.replace(tzinfo=timezone.utc)

    if dt_out_utc <= dt_in_utc:
        raise HTTPException(422, "punched_at must be strictly greater than punch_in")

    if (dt_out_utc - dt_in_utc).total_seconds() > 86400:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    work_hrs = compute_work_hours(dt_in_utc, dt_out_utc)
    ot_mins = compute_overtime(dt_out_utc, record["date"], emp["shift_start"], emp["shift_end"])
    is_half = compute_half_day(work_hrs)

    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": dt_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": is_half
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "already punched out")

    return format_attendance_record(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = Body(...)
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for date")

    target_status = body.status if body.status is not None else record["status"]

    if body.punch_in is not None:
        target_punch_in = epoch_ms_to_datetime(body.punch_in)
    else:
        target_punch_in = record["punch_in"]

    if body.punch_out is not None:
        target_punch_out = epoch_ms_to_datetime(body.punch_out)
    else:
        target_punch_out = record["punch_out"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times not allowed when setting ABSENT or LEAVE status")
        target_punch_in = None
        target_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        if target_punch_in is None:
            raise HTTPException(422, "punch_in required for presence status")

        r1_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if r1_date != date:
            raise HTTPException(422, f"punch_in instant does not fall on record attendance date {date}")

        new_late_minutes = compute_late_minutes(target_punch_in, date, emp["shift_start"])

        if target_punch_out is not None:
            dt_in = target_punch_in
            dt_out = target_punch_out
            if dt_in.tzinfo is None:
                dt_in = dt_in.replace(tzinfo=timezone.utc)
            if dt_out.tzinfo is None:
                dt_out = dt_out.replace(tzinfo=timezone.utc)

            if dt_out <= dt_in:
                raise HTTPException(422, "punch_out must be strictly greater than punch_in")
            if (dt_out - dt_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

            new_work_hours = compute_work_hours(dt_in, dt_out)
            new_overtime_minutes = compute_overtime(dt_out, date, emp["shift_start"], emp["shift_end"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    changes = {}

    def check_change(field, old_val, new_val):
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}

    check_change("status", record.get("status"), target_status)
    check_change("punch_in", record.get("punch_in"), target_punch_in)
    check_change("punch_out", record.get("punch_out"), target_punch_out)
    check_change("work_hours", record.get("work_hours"), new_work_hours)
    check_change("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    check_change("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    check_change("half_day", bool(record.get("half_day", False)), new_half_day)

    if not changes:
        raise HTTPException(422, "request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    current_history = record.get("history", [])
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{len(current_history)}": {"$exists": False}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day
            },
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "concurrent regularization conflict")

    return format_attendance_record(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m_num = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    joined_on = emp["joined_on"]
    if joined_on > month_end:
        working_days = 0
    else:
        start_date_str = max(joined_on, month_start)
        cur = datetime.fromisoformat(start_date_str).date()
        end_d = datetime.fromisoformat(month_end).date()
        working_days = 0
        while cur <= end_d:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$project": {
            "status": 1,
            "late_minutes": 1,
            "overtime_minutes": 1,
            "half_day": 1,
            "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        row = res[0]
        present_days = row["present_days"]
        leave_days = row["leave_days"]
        late_count = row["late_count"]
        total_late_minutes = row["total_late_minutes"]
        total_overtime_minutes = row["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        att_pct = None
    else:
        att_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": att_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    year, m_num = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    match_emp = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"}
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"codes": "$emp_codes"},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$in": ["$emp_code", "$$codes"]},
                                {"$gte": ["$date", month_start]},
                                {"$lte": ["$date", month_end]}
                            ]
                        }
                    }
                }
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$_id",
            "headcount": {"$first": "$headcount"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {
                                    "$in": [
                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                        [2, 3, 4, 5, 6]
                                    ]
                                }
                            ]
                        },
                        {"$cond": ["$logs.half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "sum_work_hours": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]
                        },
                        "$logs.work_hours",
                        0.0
                    ]
                }
            },
            "count_work_hours": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]
                        },
                        1,
                        0
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$gt": ["$logs.late_minutes", 0]}
                            ]
                        },
                        1,
                        0
                    ]
                }
            },
            "total_late_minutes": {
                "$sum": {
                    "$cond": [
                        {"$ne": ["$logs", None]},
                        {"$ifNull": ["$logs.late_minutes", 0]},
                        0
                    ]
                }
            },
            "leave_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$eq": ["$logs.status", "LEAVE"]}
                            ]
                        },
                        1,
                        0
                    ]
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ne": ["$logs", None]},
                                {"$eq": ["$logs.status", "ON_DUTY"]}
                            ]
                        },
                        1,
                        0
                    ]
                }
            }
        }},
        {"$sort": {"_id": 1}}
    ]

    res = list(db.employees.aggregate(pipeline))
    items = []
    for r in res:
        count_wh = r["count_work_hours"]
        avg_wh = round_half_up(r["sum_work_hours"] / count_wh, 2) if count_wh > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"]
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    year, m_num = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, m_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{days_in_month:02d}"

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee"
        }},
        {"$unwind": "$employee"}
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"rank": 1, "total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
):
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(404, "Department not found")

    d_from = datetime.fromisoformat(from_date).date()
    d_to = datetime.fromisoformat(to_date).date()

    if d_to < d_from:
        raise HTTPException(422, "to date cannot be earlier than from date")

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    emp_codes = [e["emp_code"] for e in emps]

    dt_from_utc = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    dt_to_next_utc = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    pipeline = [
        {"$documents": [{"date": dt_from_utc}]},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from_utc, dt_to_next_utc]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "date_obj": "$date"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", emp_codes]},
                                    {"$eq": ["$date", "$$d"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": emps,
                            "as": "emp",
                            "cond": {"$lte": ["$$emp.joined_on", "$date"]}
                        }
                    }
                },
                "present_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]

    res = db.command({"aggregate": 1, "pipeline": pipeline, "cursor": {}})
    raw_docs = list(res["cursor"]["firstBatch"])

    items = []
    for d in raw_docs:
        rate = round_half_up(d["attendance_rate"], 4) if d["attendance_rate"] is not None else None
        m_avg = round_half_up(d["moving_avg_7d"], 4) if d["moving_avg_7d"] is not None else None
        items.append({
            "date": d["date"],
            "is_working_day": d["is_working_day"],
            "headcount": d["headcount"],
            "present_count": d["present_count"],
            "late_count": d["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": m_avg
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Endpoints
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"Unknown endpoint: {endpoint}")

    if endpoint == "attendance_list":
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
        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly explain")
        year, m_num = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, m_num)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{days_in_month:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
            }}
        ]
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary explain")
        year, m_num = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, m_num)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{days_in_month:02d}"

        match_emp = {"joined_on": {"$lte": month_end}}
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$in": ["$emp_code", "$$codes"]}, {"$gte": ["$date", month_start]}, {"$lte": ["$date", month_end]}]}}}
                ],
                "as": "logs"
            }},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$_id",
                "headcount": {"$first": "$headcount"},
                "present_days": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}]}, {"$cond": ["$logs.half_day", 0.5, 1.0]}, 0.0]}},
                "sum_work_hours": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0.0]}},
                "count_work_hours": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$cond": [{"$ne": ["$logs", None]}, {"$ifNull": ["$logs.late_minutes", 0]}, 0]}},
                "leave_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "LEAVE"]}]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "ON_DUTY"]}]}, 1, 0]}}
            }},
            {"$sort": {"_id": 1}}
        ]
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard explain")
        year, m_num = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, m_num)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{days_in_month:02d}"

        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"}
        ]
        if department:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"rank": 1, "total_late_minutes": -1, "_id": 1}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
        ])
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend explain")
        emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
        emp_codes = [e["emp_code"] for e in emps]
        dt_from_utc = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        dt_to_next_utc = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)

        pipeline = [
            {"$documents": [{"date": dt_from_utc}]},
            {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [dt_from_utc, dt_to_next_utc]}}},
            {"$project": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}, "date_obj": "$date"}},
            {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$in": ["$emp_code", emp_codes]}, {"$eq": ["$date", "$$d"]}]}}}], "as": "logs"}},
            {"$addFields": {
                "is_working_day": {"$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]},
                "headcount": {"$size": {"$filter": {"input": emps, "as": "emp", "cond": {"$lte": ["$$emp.joined_on", "$date"]}}}},
                "present_count": {"$reduce": {"input": "$logs", "initialValue": 0.0, "in": {"$add": ["$$value", {"$cond": [{"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": ["$$this.half_day", 0.5, 1.0]}, 0.0]}]}}},
                "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}}
            }},
            {"$addFields": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
            {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
            {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}
        ]
        cmd = {
            "explain": {
                "aggregate": 1,
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}
