"""
Employee Attendance & Analytics API

Run:  python app/main.py  or  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
from contextlib import asynccontextmanager
import calendar
import os
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, errors

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
VALID_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def ensure_epoch_ms(value: Optional[int], field_name: str = "time") -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer epoch milliseconds value")
    if not (MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
        raise ValueError(f"{field_name} is out of range")
    return value


def parse_hhmm(value: str) -> time_cls:
    h, m = map(int, value.split(":"))
    return time_cls(hour=h, minute=m)


def parse_iso_date(value: str) -> date_cls:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("invalid date format; expected YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid date format; expected YYYY-MM-DD") from exc


def dt_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def convert_db_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return dt_to_epoch_ms(value)
    if isinstance(value, (int, float, str, type(None), bool)):
        return value
    return value


def round_half_up(value: float, digits: int) -> float:
    exponent = Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(exponent, rounding=ROUND_HALF_UP))


def normalize_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    dt = normalize_aware(dt)
    return dt.astimezone(IST)


def epoch_ms_to_dt(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def get_date_range_for_month(month: str) -> tuple[str, str]:
    try:
        year, mon = map(int, month.split("-"))
        if mon < 1 or mon > 12:
            raise ValueError
    except ValueError as exc:
        raise ValueError("month must be YYYY-MM") from exc
    last_day = calendar.monthrange(year, mon)[1]
    start = f"{year:04d}-{mon:02d}-01"
    end = f"{year:04d}-{mon:02d}-{last_day:02d}"
    return start, end


def working_days_in_period(start_date: date_cls, end_date: date_cls, joined_on: date_cls) -> int:
    actual_start = max(start_date, joined_on)
    total = 0
    current = actual_start
    while current <= end_date:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def is_weekday(day: date_cls) -> bool:
    return day.weekday() < 5


def compute_shift_start_for_punch(punch_in_ist: datetime, shift_start: str, shift_end: str) -> datetime:
    shift_start_time = parse_hhmm(shift_start)
    shift_end_time = parse_hhmm(shift_end)
    if shift_end_time <= shift_start_time:
        if punch_in_ist.time() < shift_end_time:
            base_date = punch_in_ist.date() - timedelta(days=1)
        else:
            base_date = punch_in_ist.date()
    else:
        base_date = punch_in_ist.date()
    return datetime.combine(base_date, shift_start_time, tzinfo=IST)


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = to_ist(normalize_aware(punch_in))
    shift_end_time = parse_hhmm(shift_end)
    shift_start_time = parse_hhmm(shift_start)
    if shift_end_time <= shift_start_time:
        if punch_in_ist.time() < shift_end_time:
            d = punch_in_ist.date() - timedelta(days=1)
        else:
            d = punch_in_ist.date()
    else:
        d = punch_in_ist.date()
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    punch_in_ist = to_ist(normalize_aware(punch_in))
    start = compute_shift_start_for_punch(punch_in_ist, shift_start, shift_end)
    minutes = int((punch_in_ist - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    diff = normalize_aware(punch_out) - normalize_aware(punch_in)
    hours = Decimal(str(diff.total_seconds())) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    punch_out = normalize_aware(punch_out)
    end_date = date_cls.fromisoformat(date_str)
    if shift_end <= shift_start:
        end_date += timedelta(days=1)
    end_dt = datetime.combine(end_date, parse_hhmm(shift_end), tzinfo=IST)
    diff_minutes = int((punch_out - end_dt).total_seconds() / 60)
    return max(0, diff_minutes) if diff_minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    rounded = round_half_up(float(work_hours), 2)
    return rounded < 4.50


def serialize_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in out and out["created_at"] is not None:
        out["created_at"] = dt_to_epoch_ms(out["created_at"])
    return out


def serialize_history_entry(entry: dict) -> dict:
    out = {
        "at": dt_to_epoch_ms(entry.get("at")) if isinstance(entry.get("at"), datetime) else entry.get("at"),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {},
    }
    for key, value in (entry.get("changes") or {}).items():
        normalized = {}
        for k, v in (value or {}).items():
            if k in {"from", "to"}:
                normalized[k] = convert_db_value(v)
            else:
                normalized[k] = v
        out["changes"][key] = normalized
    return out


def serialize_attendance_record(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    for field in ("punch_in", "punch_out"):
        if field in out and out[field] is not None:
            out[field] = dt_to_epoch_ms(out[field])
    if "history" in out:
        out["history"] = [serialize_history_entry(item) for item in out["history"]]
    if "created_at" in out:
        out["created_at"] = dt_to_epoch_ms(out["created_at"])
    return out


def ensure_employee_exists(emp_code: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def ensure_attendance_record(emp_code: str, date_str: str):
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not doc:
        raise HTTPException(404, "attendance record not found")
    return doc


def sanitize_punch_in_time(raw: Optional[int]) -> datetime:
    if raw is None:
        return datetime.now(IST).replace(microsecond=0)
    return epoch_ms_to_dt(raw).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=1, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        try:
            parse_hhmm(value)
        except ValueError as exc:
            raise ValueError("shift must be in HH:MM format") from exc
        return value

    @model_validator(mode="after")
    def validate_shift_bounds(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_iso_date(value)
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return ensure_epoch_ms(value, "punched_at")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"PRESENT", "WFH", "ON_DUTY"}:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return ensure_epoch_ms(value, "punched_at")


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value not in VALID_STATUSES:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        return ensure_epoch_ms(value, "timestamp")


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index("late_minutes")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}") from exc


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except errors.DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    items = [serialize_employee(item) for item in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = ensure_employee_exists(body.emp_code)
    status = body.status
    if status not in PRESENT_STATUSES:
        raise HTTPException(422, "status must be PRESENT, WFH, or ON_DUTY")
    punched_at = sanitize_punch_in_time(body.punched_at)
    punched_at_utc = punched_at.astimezone(timezone.utc)
    punch_date = attendance_date_for_punch(punched_at, emp["shift_start"], emp["shift_end"])
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": punch_date}):
        raise HTTPException(409, "already punched in for this date")
    late_minutes = compute_late_minutes(punched_at_utc, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": punch_date,
        "status": status,
        "punch_in": punched_at_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except errors.DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = ensure_employee_exists(body.emp_code)
    punched_at = sanitize_punch_in_time(body.punched_at)
    punched_utc = punched_at.astimezone(timezone.utc)
    candidates = list(db.attendance_logs.find({"emp_code": body.emp_code, "punch_in": {"$ne": None}}).sort("punch_in", 1))
    target = None
    for item in candidates:
        item_punch_in = item.get("punch_in")
        item_punch_in_dt = item_punch_in if isinstance(item_punch_in, datetime) else None
        if item_punch_in_dt is None:
            continue
        if item_punch_in_dt.tzinfo is None:
            item_punch_in_dt = item_punch_in_dt.replace(tzinfo=timezone.utc)
        if item_punch_in_dt <= punched_utc and (target is None or item_punch_in_dt > target["punch_in"]):
            target = item
    if target is None:
        raise HTTPException(404, "no punch-in found")
    if target.get("punch_out") is not None:
        raise HTTPException(409, "that record is already punched out")
    punch_in_dt = normalize_aware(target["punch_in"])
    if punched_utc <= punch_in_dt:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punched_utc - punch_in_dt > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    work_hours = compute_work_hours(punch_in_dt, punched_utc)
    overtime = compute_overtime(punched_utc, emp["shift_end"], target["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)
    update = {
        "punch_out": punched_utc,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    result = db.attendance_logs.update_one({"_id": target["_id"], "punch_out": None}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(409, "that record is already punched out")
    fresh = db.attendance_logs.find_one({"_id": target["_id"]})
    return serialize_attendance_record(fresh)


def build_attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict[str, Any]:
    try:
        start = parse_iso_date(date_from) if date_from is not None else None
        end = parse_iso_date(date_to) if date_to is not None else None
    except ValueError as exc:
        raise HTTPException(422, "dates must be YYYY-MM-DD") from exc
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "date_from must be <= date_to")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size))
    for item in items:
        item["punch_in"] = dt_to_epoch_ms(item["punch_in"]) if item.get("punch_in") is not None else None
        item["punch_out"] = dt_to_epoch_ms(item["punch_out"]) if item.get("punch_out") is not None else None
        if "history" in item:
            item["history"] = [serialize_history_entry(h) for h in item["history"]]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    try:
        parse_iso_date(date)
    except ValueError as exc:
        raise HTTPException(422, "date must be YYYY-MM-DD") from exc
    doc = ensure_attendance_record(emp_code, date)
    emp = ensure_employee_exists(emp_code)
    original_doc = {k: copy_value(v) for k, v in doc.items()}
    new_doc = {**doc}
    fields_to_update = {}
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(422, "request changes nothing")
    if any(field in updates and updates[field] is None for field in ("punch_in", "punch_out")):
        raise HTTPException(422, "punch timestamps cannot be null")
    if "reason" in updates:
        updates.pop("reason")
    if "regularized_by" in updates:
        updates.pop("regularized_by")
    if "status" in updates:
        status = updates["status"]
        if status in {"ABSENT", "LEAVE"}:
            if "punch_in" in updates or "punch_out" in updates:
                raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")
            new_doc["status"] = status
            new_doc["punch_in"] = None
            new_doc["punch_out"] = None
            fields_to_update["status"] = status
            fields_to_update["punch_in"] = None
            fields_to_update["punch_out"] = None
            updates.pop("status")
        else:
            new_doc["status"] = status
            fields_to_update["status"] = status
            updates.pop("status")
    if "punch_in" in updates:
        new_punch_in = updates["punch_in"]
        dt = epoch_ms_to_dt(new_punch_in).replace(microsecond=0)
        if attendance_date_for_punch(dt, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the record date")
        new_doc["punch_in"] = dt
        fields_to_update["punch_in"] = dt
    if "punch_out" in updates:
        new_punch_out = updates["punch_out"]
        dt = epoch_ms_to_dt(new_punch_out).replace(microsecond=0)
        new_doc["punch_out"] = dt
        fields_to_update["punch_out"] = dt
    if new_doc.get("status") in {"ABSENT", "LEAVE"}:
        if new_doc.get("punch_in") is not None or new_doc.get("punch_out") is not None:
            raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")
    if new_doc.get("status") in PRESENT_STATUSES:
        punch_in_value = new_doc.get("punch_in")
        if punch_in_value is None:
            if doc.get("punch_in") is None:
                raise HTTPException(422, "presence status requires a punch_in")
            punch_in_value = doc.get("punch_in")
            new_doc["punch_in"] = punch_in_value
        punch_out_value = new_doc.get("punch_out")
        if punch_out_value is not None and punch_in_value is not None:
            punch_in_value = normalize_aware(punch_in_value)
            punch_out_value = normalize_aware(punch_out_value)
            if punch_out_value <= punch_in_value:
                raise HTTPException(422, "punch_out must be after punch_in")
            if punch_out_value - punch_in_value > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    effective_punch_in = new_doc.get("punch_in")
    effective_punch_out = new_doc.get("punch_out")
    if effective_punch_in is not None and effective_punch_out is not None:
        effective_punch_in = normalize_aware(effective_punch_in)
        effective_punch_out = normalize_aware(effective_punch_out)
        if effective_punch_out <= effective_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if effective_punch_out - effective_punch_in > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    if effective_punch_in is not None and effective_punch_out is None:
        new_doc["late_minutes"] = compute_late_minutes(
            effective_punch_in, emp["shift_start"], emp["shift_end"]
        )
        new_doc["work_hours"] = None
        new_doc["overtime_minutes"] = 0
        new_doc["half_day"] = False
    elif effective_punch_in is not None and effective_punch_out is not None:
        new_doc["late_minutes"] = compute_late_minutes(effective_punch_in, emp["shift_start"], emp["shift_end"])
        new_doc["work_hours"] = compute_work_hours(effective_punch_in, effective_punch_out)
        new_doc["overtime_minutes"] = compute_overtime(effective_punch_out, emp["shift_end"], date, emp["shift_start"])
        new_doc["half_day"] = compute_half_day(new_doc["work_hours"])
    else:
        new_doc["late_minutes"] = 0
        new_doc["work_hours"] = None
        new_doc["overtime_minutes"] = 0
        new_doc["half_day"] = False
    changes: dict[str, Any] = {}
    for field in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        old_value = original_doc.get(field)
        new_value = new_doc.get(field)
        if field in {"punch_in", "punch_out"}:
            old_value = dt_to_epoch_ms(old_value) if isinstance(old_value, datetime) else old_value
            new_value = dt_to_epoch_ms(new_value) if isinstance(new_value, datetime) else new_value
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "request changes nothing")
    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {k: {"from": convert_db_value(v["from"]), "to": convert_db_value(v["to"])} for k, v in changes.items()},
    }
    updated_history = list(doc.get("history", []) or [])
    updated_history.append(history_entry)
    new_doc["history"] = updated_history
    concurrency_filter = {"_id": doc["_id"]}
    if "history" in doc:
        concurrency_filter["history"] = doc["history"]
    else:
        concurrency_filter["history"] = {"$exists": False}
    result = db.attendance_logs.update_one(
        concurrency_filter,
        {"$set": {k: v for k, v in new_doc.items() if k not in {"_id"}}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "attendance record was updated concurrently")
    fresh = db.attendance_logs.find_one({"_id": doc["_id"]})
    return serialize_attendance_record(fresh)


def build_employee_monthly_pipeline(emp_code: str, start_date: str, end_date: str) -> list[dict]:
    weekday_present = {
        "$and": [
            {"$in": ["$status", sorted(PRESENT_STATUSES)]},
            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
        ]
    }
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                weekday_present,
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
        {"$project": {"_id": 0}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = ensure_employee_exists(emp_code)
    start_date_str, end_date_str = get_date_range_for_month(month)
    start_date = date_cls.fromisoformat(start_date_str)
    end_date = date_cls.fromisoformat(end_date_str)
    joined_on = date_cls.fromisoformat(emp["joined_on"])
    totals = next(iter(db.attendance_logs.aggregate(
        build_employee_monthly_pipeline(emp_code, start_date_str, end_date_str)
    )), {})
    working_days = working_days_in_period(start_date, end_date, joined_on)
    present_days = float(totals.get("present_days", 0))
    attendance_pct = None if working_days == 0 else round_half_up((present_days / working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(totals.get("leave_days", 0)),
        "late_count": int(totals.get("late_count", 0)),
        "total_late_minutes": int(totals.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(start_date: str, end_date: str, department: Optional[str]) -> list[dict]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department is not None:
        employee_match["department"] = department
    present_status = {"$in": ["$logs.status", sorted(PRESENT_STATUSES)]}
    weekday_present = {
        "$cond": [
            present_status,
            {"$cond": [
                {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 5]},
                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                0,
            ]},
            0,
        ]
    }
    work_hours_present = {
        "$and": [
            present_status,
            {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
        ]
    }
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", start_date]},
                    {"$lte": ["$date", end_date]},
                ]}}},
                {"$project": {
                    "_id": 0, "date": 1, "status": 1, "half_day": 1,
                    "late_minutes": 1, "work_hours": 1,
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "present_days": {"$sum": weekday_present},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0,
            ]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [work_hours_present, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [work_hours_present, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start_date_str, end_date_str = get_date_range_for_month(month)
    rows = db.employees.aggregate(
        build_department_summary_pipeline(start_date_str, end_date_str, department)
    )
    items = []
    for row in rows:
        work_hours_count = row["work_hours_count"]
        avg_work_hours = None if not work_hours_count else round_half_up(
            row["work_hours_sum"] / work_hours_count, 2
        )
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": float(row["present_days"]),
            "avg_work_hours": avg_work_hours,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    start_date: str,
    end_date: str,
    limit: int,
    department: Optional[str],
) -> list[dict]:
    employee_match: dict[str, Any] = {"$expr": {"$eq": ["$emp_code", "$$employee_code"]}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": {
            "date": {"$gte": start_date, "$lte": end_date},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"employee_code": "$_id"},
            "pipeline": [
                {"$match": employee_match},
                {"$project": {"_id": 0, "name": 1, "department": 1}},
            ],
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$set": {
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": 1, "name": 1,
            "department": 1, "total_late_minutes": 1, "late_count": 1,
        }},
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start_date_str, end_date_str = get_date_range_for_month(month)
    items = list(db.attendance_logs.aggregate(
        build_late_leaderboard_pipeline(start_date_str, end_date_str, limit, department)
    ))
    return {"month": month, "items": items}


def mongo_round_half_up(value: Any, digits: int) -> dict:
    scale = 10 ** digits
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value, scale]}, 0.500000000001]}}, scale]}


def build_department_trend_pipeline(
    department: str,
    start: date_cls,
    end: date_cls,
) -> list[dict]:
    start_dt = datetime.combine(start, time_cls.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(end + timedelta(days=1), time_cls.min, tzinfo=timezone.utc)
    date_string = {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}
    return [
        {"$match": {"department": department}},
        {"$group": {
            "_id": None,
            "date": {"$min": {"$literal": start_dt}},
            "seed_employee": {"$first": "$emp_code"},
            "all_employee_codes": {"$addToSet": "$emp_code"},
        }},
        {"$project": {"seed_employee": 0}},
        {"$densify": {
            "field": "date",
            "range": {"bounds": [start_dt, end_exclusive], "step": 1, "unit": "day"},
        }},
        {"$set": {"date_string": date_string}},
        {"$lookup": {
            "from": "employees",
            "let": {"report_date": "$date_string"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "$expr": {"$lte": ["$joined_on", "$$report_date"]},
                }},
                {"$project": {"_id": 0, "emp_code": 1}},
            ],
            "as": "eligible_employees",
        }},
        {"$set": {
            "headcount": {"$size": "$eligible_employees"},
            "employee_codes": "$all_employee_codes",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "employee_codes",
            "foreignField": "emp_code",
            "let": {"report_date": "$date_string"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$report_date"]}}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$set": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date"}, 5]},
            "present_count": {"$reduce": {
                "input": "$logs",
                "initialValue": 0,
                "in": {"$add": ["$$value", {"$cond": [
                    {"$in": ["$$this.status", sorted(PRESENT_STATUSES)]},
                    {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1]},
                    0,
                ]}]},
            }},
            "late_count": {"$size": {"$filter": {
                "input": "$logs", "as": "log",
                "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
            }}},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$ne": ["$attendance_rate", None]},
            mongo_round_half_up("$attendance_rate", 4),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$ne": ["$moving_avg_7d", None]},
            mongo_round_half_up("$moving_avg_7d", 4),
            None,
        ]}}},
        {"$project": {
            "_id": 0,
            "date": "$date_string",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start = date_cls.fromisoformat(from_date)
        end = date_cls.fromisoformat(to_date)
    except ValueError as exc:
        raise HTTPException(422, "dates must be YYYY-MM-DD") from exc
    if end < start:
        raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = build_department_trend_pipeline(department, start, end)
    rows = list(db.employees.aggregate(pipeline))
    for row in rows:
        if row["attendance_rate"] is not None:
            row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
    return {"department": department, "items": rows}


def explain_aggregation(collection: str, pipeline: list[dict]) -> dict:
    return db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(default=None, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = build_attendance_filter(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"}:
        if not month:
            raise HTTPException(422, "month is required")
        try:
            start_date, end_date = get_date_range_for_month(month)
        except ValueError as exc:
            raise HTTPException(422, "month must be YYYY-MM") from exc

        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required")
            collection = "attendance_logs"
            pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = build_department_summary_pipeline(start_date, end_date, department)
        else:
            collection = "attendance_logs"
            pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit or 10, department)
        explain = explain_aggregation(collection, pipeline)
        return {"endpoint": endpoint, "collection": collection, "explain": explain}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        try:
            start = parse_iso_date(from_date)
            end = parse_iso_date(to_date)
        except ValueError as exc:
            raise HTTPException(422, "dates must be YYYY-MM-DD") from exc
        if end < start:
            raise HTTPException(422, "to must be >= from")
        if (end - start).days + 1 > 92:
            raise HTTPException(422, "range exceeds 92 days")
        pipeline = build_department_trend_pipeline(department, start, end)
        explain = explain_aggregation("employees", pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    raise HTTPException(422, "invalid endpoint")


def copy_value(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, list):
        return [copy_value(v) for v in value]
    if isinstance(value, dict):
        return {k: copy_value(v) for k, v in value.items()}
    return value


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
