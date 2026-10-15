"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Conventions used throughout this file
  * Instants are BSON datetimes in MongoDB (client is tz_aware, so always UTC-aware in Python) and integer epoch
    milliseconds in the API. Everything is converted at the boundary (to_ms / from_ms).
  * Calendar dates / months / shift times are plain strings, IST.
  * Exact arithmetic (Fraction / integers) is used for every reported number that needs half-up rounding.
  * All analytics are MongoDB aggregation pipelines; Python only formats the (tiny) results.
"""
import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv()  # override=False: variables already in the environment win

log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # datetimes come back timezone-aware (UTC), never naive
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS = 100_000_000_000  # below this a value "looks like seconds"
MAX_MS = 4_102_444_800_000

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
NO_PRESENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]  # strict: no floats / strings / bools

# [0-9] instead of \d: the regex engine would otherwise accept non-ASCII digits.
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
INDEXES = [
    # (collection, keys, options)
    # emp_code is the business key: uniqueness is enforced by the database, not by check-then-insert.
    (employees, [("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
    # GET /employees?department= (filter + sort by emp_code) and the department trend's employee scan.
    (employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "department_emp_code"}),
    # Department summary without a department filter: employees who joined on/before month end.
    (employees, [("joined_on", ASCENDING)], {"name": "joined_on"}),
    # One record per (emp_code, date): punch-in race guard, PATCH lookup, per-employee month ranges,
    # and the per-employee $lookup in the department analytics.
    (logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "emp_code_date_unique"}),
    # GET /attendance sort order (date desc, emp_code asc) and month-range scans (late leaderboard).
    (logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp_code"}),
    # GET /attendance?status=... keeps the sort index-backed.
    (logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp_code"}),
    # Punch-out: "most recent record of this employee with punch_in <= punched_at".
    (logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_code_punch_in_desc"}),
]
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    for coll, keys, opts in INDEXES:
        try:
            coll.create_index(keys, **opts)
        except OperationFailure as exc:  # e.g. pre-existing duplicates under a unique index: stay up, say so loudly
            log.error("could not create index %s on %s: %s", opts.get("name"), coll.name, exc)
        # connection errors propagate on purpose: the flag stays False and /health retries later
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # MongoDB may still be starting; /health retries index creation
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def invalid(loc: tuple, msg: str, value: Any = None):
    """Raise a 422 with FastAPI's default validation-error body."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    ms = ms - ms % 1000
    return EPOCH + timedelta(milliseconds=ms)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: seeded data read through a non-tz-aware client
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)  # BSON dates hold milliseconds


def round_half_up(x, places: int) -> float:
    """Exact half-up rounding of a non-negative rational (Fraction / int)."""
    scaled = Fraction(x) * 10**places
    return math.floor(scaled + Fraction(1, 2)) / 10**places


def parse_date(value: str, loc: tuple) -> date:
    if not re.fullmatch(DATE_PATTERN, value):
        invalid(loc, "date must be YYYY-MM-DD", value)
    try:
        return date.fromisoformat(value)
    except ValueError:
        invalid(loc, "not a valid calendar date", value)


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1 or year > 9998:
        invalid(("query", "month"), "year out of range", month)
    first = date(year, mon, 1)
    nxt = date(year + (mon == 12), mon % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def working_days(first: date, last: date, joined_on: Optional[str]) -> int:
    """R7: Mon-Fri days in [first, last], counted from joined_on for a mid-month joiner."""
    start = first
    if joined_on:
        try:
            start = max(first, date.fromisoformat(joined_on))
        except ValueError:
            pass
    count, day = 0, start
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def parse_hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def shift_times(emp: dict) -> tuple[str, str]:
    return emp.get("shift_start") or "09:30", emp.get("shift_end") or "18:30"


def shift_window(day: date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Instants (aware, IST) at which the shift that belongs to attendance date `day` starts and ends.
    The end of an overnight shift (shift_end <= shift_start) falls on the next calendar day."""
    sh, sm = parse_hm(shift_start)
    eh, em = parse_hm(shift_end)
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    end = datetime(end_day.year, end_day.month, end_day.day, eh, em, tzinfo=IST)
    return start, end


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date of the punch-in; for an overnight shift a punch-in earlier than shift_end belongs
    to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    eh, em = parse_hm(shift_end)
    if (eh, em) <= parse_hm(shift_start) and local.time() < time(eh, em):
        day -= timedelta(days=1)
    return day


def derive(emp: dict, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: str) -> dict:
    """The four derived fields (R2-R5) from final status / punch times."""
    zero = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in NO_PRESENCE or punch_in is None:
        return zero
    punch_in = punch_in.replace(microsecond=0)
    start, end = shift_window(date.fromisoformat(day), *shift_times(emp))
    late_seconds = int((punch_in - start).total_seconds())
    out = dict(zero)
    out["late_minutes"] = late_seconds // 60 if late_seconds > 600 else 0  # R2: strictly more than 10:00
    if punch_out is not None:
        punch_out = punch_out.replace(microsecond=0)
        worked = int((punch_out - punch_in).total_seconds())
        out["work_hours"] = round_half_up(Fraction(worked, 3600), 2)  # R4
        over = int((punch_out - end).total_seconds()) // 60  # floor; negative if left early
        out["overtime_minutes"] = over if over >= 30 else 0  # R3
        out["half_day"] = out["work_hours"] < 4.5  # R5 (on the rounded value)
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    # Derived fields (work_hours, ...) are not declared, so a client that sends them is simply ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_null(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


# --------------------------------------------------------------------------- #
# Response formatting (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def format_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start") or "09:30",
        "shift_end": doc.get("shift_end") or "18:30",
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def format_history(history: Optional[list]) -> list:
    out = []
    for entry in history or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            if field in ("punch_in", "punch_out") and isinstance(change, dict):
                change = {k: to_ms(v) if isinstance(v, datetime) else v for k, v in change.items()}
            changes[field] = change
        out.append({"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes})
    return out


def format_record(doc: dict) -> dict:
    """Missing legacy fields (history, half_day, ...) are treated as empty / false / 0."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": format_history(doc.get("history")),
    }


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# System / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    try:
        ensure_indexes()  # no-op once done; covers a MongoDB that came up after the app
    except Exception as exc:
        log.warning("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {} if department is None else {"department": department}
    total = employees.count_documents(q)
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [format_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_day(ts, *shift_times(emp)).isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, body.status, ts, None, day),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return format_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},  # null punch_in (ABSENT / LEAVE) never matches
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punched_in = rec["punch_in"].replace(microsecond=0)
    if ts <= punched_in:
        invalid(("body", "punched_at"), "punched_at must be after punch_in", body.punched_at)
    if ts - punched_in > timedelta(hours=24):
        invalid(("body", "punched_at"), "punched_at is more than 24 hours after punch_in", body.punched_at)
    values = derive(emp, rec["status"], rec["punch_in"], ts, rec["date"])
    # The filter re-checks that the record is still open and unchanged: of two simultaneous punch-outs only one matches.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": ts, **values}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out (or modified concurrently)")
    return format_record(updated)


# --------------------------------------------------------------------------- #
# Attendance: list
# --------------------------------------------------------------------------- #
def build_attendance_find(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """The find() used by GET /attendance; also used verbatim by the explain endpoint."""
    d_from = parse_date(date_from, ("query", "date_from")) if date_from is not None else None
    d_to = parse_date(date_to, ("query", "date_to")) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        invalid(("query", "date_from"), "date_from must not be after date_to", date_from)
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from.isoformat()
        if d_to:
            q["date"]["$lte"] = d_to.isoformat()
    if status is not None:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "projection": {"_id": 0},
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    f = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(f["filter"]) if f["filter"] else logs.estimated_document_count()
    cursor = (
        logs.find(f["filter"], f["projection"])
        .sort(list(f["sort"].items()))
        .skip(f["skip"])
        .limit(f["limit"])
    )
    return {"items": [format_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: regularization (manual correction with audit trail)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):  # noqa: A002 - name fixed by the contract
    day = parse_date(date, ("path", "date")).isoformat()
    emp = get_employee(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    cur_status, cur_in, cur_out = rec.get("status"), rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or cur_status

    if new_status in NO_PRESENCE:
        if "punch_in" in sent or "punch_out" in sent:
            invalid(("body",), f"{new_status} clears the punch times; do not send punch_in / punch_out")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if "punch_in" in sent else cur_in
        new_out = from_ms(body.punch_out) if "punch_out" in sent else cur_out
        if new_in is None:
            invalid(("body", "punch_in"), f"status {new_status} requires a punch_in")
        if "punch_in" in sent and attendance_day(new_in, *shift_times(emp)).isoformat() != day:
            invalid(("body", "punch_in"), "punch_in must stay on the record's attendance date", body.punch_in)
        if new_out is not None and ("punch_in" in sent or "punch_out" in sent):
            span = new_out.replace(microsecond=0) - new_in.replace(microsecond=0)
            if span <= timedelta(0):
                invalid(("body", "punch_out"), "punch_out must be after punch_in", body.punch_out)
            if span > timedelta(hours=24):
                invalid(("body", "punch_out"), "punch_out must be within 24 hours of punch_in", body.punch_out)

    if new_status == cur_status and to_ms(new_in) == to_ms(cur_in) and to_ms(new_out) == to_ms(cur_out):
        invalid(("body",), "the request changes nothing")

    old = {
        "status": cur_status,
        "punch_in": cur_in,
        "punch_out": cur_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derive(emp, new_status, new_in, new_out, day)}
    changes = {}
    for field, before in old.items():
        after = new[field]
        if isinstance(before, datetime) or isinstance(after, datetime):
            same = to_ms(before) == to_ms(after)
        else:
            same = before == after
        if not same:
            changes[field] = {"from": before, "to": after}  # datetimes stay BSON dates in the database

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    # Optimistic concurrency: the update applies only if history still has exactly n entries and the fields we
    # read are unchanged, so two simultaneous corrections cannot silently overwrite each other (loser gets 409).
    guard: dict = {
        "_id": rec["_id"],
        "status": cur_status,
        "punch_in": cur_in,
        "punch_out": cur_out,
        f"history.{n}": {"$exists": False},
    }
    if n:
        guard[f"history.{n - 1}"] = {"$exists": True}
    updated = logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return format_record(updated)


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and the explain endpoint)
# --------------------------------------------------------------------------- #
def _is_presence(p: str = "$") -> dict:
    return {"$in": [f"{p}status", PRESENCE]}


def _is_weekday(p: str = "$") -> dict:
    # $dayOfWeek: 1 = Sunday ... 7 = Saturday. Dates are plain strings, parsed as UTC midnight (no zone shift).
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": f"{p}date", "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]}


def _halves(p: str = "$") -> dict:
    """Presence in half-day units: 2 = full day, 1 = half day (R5). Integers keep the arithmetic exact."""
    return {"$cond": [{"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}, 1, 2]}


def _late(p: str = "$") -> dict:
    return {"$gt": [{"$ifNull": [f"{p}late_minutes", 0]}, 0]}


def _count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def _month_match(first: date, last: date) -> dict:
    return {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}


def _summary_group() -> dict:
    """Per-group sums shared by the employee monthly and the department summary pipelines."""
    work_hours_cents = {
        "$cond": [
            {"$and": [_is_presence(), {"$isNumber": "$work_hours"}]},
            {"$round": [{"$multiply": ["$work_hours", 100]}, 0]},  # exact integer hundredths
            0,
        ]
    }
    return {
        "present_halves": {"$sum": {"$cond": [{"$and": [_is_presence(), _is_weekday()]}, _halves(), 0]}},
        "wh_cents": {"$sum": work_hours_cents},
        "wh_n": _count_if({"$and": [_is_presence(), {"$isNumber": "$work_hours"}]}),
        "late_count": _count_if(_late()),
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": _count_if({"$eq": ["$status", "LEAVE"]}),
        "on_duty_count": _count_if({"$eq": ["$status", "ON_DUTY"]}),
    }


def build_employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, **_month_match(first, last)}},  # (emp_code, date) index
        {"$group": {"_id": None, **_summary_group()}},
    ]


def build_department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    emp_match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined on/before the end of the month
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        # Start from employees so people with zero logs still count; the sub-pipeline is index-backed
        # ((emp_code, date) index) and returns at most one small document per employee.
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$group": {"_id": None, **_summary_group()}},
                ],
                "as": "s",
            }
        },
        {"$set": {"s": {"$arrayElemAt": ["$s", 0]}}},  # no logs -> s missing -> every $ifNull below gives 0
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_halves": {"$sum": {"$ifNull": ["$s.present_halves", 0]}},
                "wh_cents": {"$sum": {"$ifNull": ["$s.wh_cents", 0]}},
                "wh_n": {"$sum": {"$ifNull": ["$s.wh_n", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {**_month_match(first, last), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4); the array position is never used as the rank.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff are all kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def build_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    f, t = d_from.isoformat(), d_to.isoformat()
    lo, hi = day_start(d_from), day_start(d_to)
    window7 = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department}},  # (department, emp_code) index
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": f, "$lte": t}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        # One "event" row per join date and per log, so headcount (cumulative joins) and the day's numbers can be
        # folded together by a single $group on the date.
        {
            "$project": {
                "_id": 0,
                "rows": {
                    "$concatArrays": [
                        [{"d": "$joined_on", "j": 1, "p": 0, "l": 0}],
                        {
                            "$map": {
                                "input": "$logs",
                                "as": "g",
                                "in": {
                                    "d": "$$g.date",
                                    "j": 0,
                                    "p": {"$cond": [_is_presence("$$g."), _halves("$$g."), 0]},
                                    "l": {"$cond": [_late("$$g."), 1, 0]},
                                },
                            }
                        },
                    ]
                },
            }
        },
        {"$unwind": "$rows"},
        {"$group": {"_id": "$rows.d", "joins": {"$sum": "$rows.j"}, "halves": {"$sum": "$rows.p"}, "late": {"$sum": "$rows.l"}}},
        {"$set": {"date": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # Gap filling: one document per calendar day in [from, to] (upper bound is exclusive, hence +1 day).
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [lo, hi + timedelta(days=1)]}}},
        {"$set": {"joins": {"$ifNull": ["$joins", 0]}, "halves": {"$ifNull": ["$halves", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        # Headcount = employees joined on or before this day (includes joins before `from`).
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$match": {"date": {"$gte": lo, "$lte": hi}}},  # only now drop rows outside the requested range
        {"$set": {"is_working": {"$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]}}},
        # Rate in 1e-4 units, half-up, with integer arithmetic: floor((halves*10000 + h) / (2h)).
        {
            "$set": {
                "rate_int": {
                    "$cond": [
                        {"$and": ["$is_working", {"$gt": ["$headcount", 0]}]},
                        {"$floor": {"$divide": [{"$add": [{"$multiply": ["$halves", 10000]}, "$headcount"]}, {"$multiply": ["$headcount", 2]}]}},
                        None,
                    ]
                }
            }
        },
        {"$set": {"has_rate": {"$cond": [{"$eq": ["$rate_int", None]}, 0, 1]}}},
        # 7-day window over rows inside the range only (rows before `from` were removed above).
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"rate_sum": {"$sum": "$rate_int", "window": window7}, "rate_n": {"$sum": "$has_rate", "window": window7}}}},
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "is_working_day": "$is_working",
                "headcount": 1,
                "present_count": {"$divide": ["$halves", 2]},
                "late_count": "$late",
                "attendance_rate": {"$cond": [{"$eq": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$rate_n", 0]},
                        None,
                        {"$divide": [{"$floor": {"$divide": [{"$add": [{"$multiply": ["$rate_sum", 2]}, "$rate_n"]}, {"$multiply": ["$rate_n", 2]}]}}, 10000]},
                    ]
                },
            }
        },
    ]


def parse_trend_range(from_: str, to: str) -> tuple[date, date]:
    d_from = parse_date(from_, ("query", "from"))
    d_to = parse_date(to, ("query", "to"))
    if d_to < d_from:
        invalid(("query", "to"), "to must not be before from", to)
    if (d_to - d_from).days + 1 > 92:
        invalid(("query", "to"), "range must not exceed 92 days", to)
    return d_from, d_to


# --------------------------------------------------------------------------- #
# Analytics: endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)
    rows = list(logs.aggregate(build_employee_monthly_pipeline(emp_code, first, last)))
    s = rows[0] if rows else {}
    halves = s.get("present_halves", 0)
    days = working_days(first, last, emp.get("joined_on"))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": halves / 2,
        "leave_days": s.get("leave_count", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Fraction(halves, 2) / days * 100, 2) if days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in employees.aggregate(build_department_summary_pipeline(first, last, department)):
        n = int(r["wh_n"])
        items.append(
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": r["present_halves"] / 2,
                # mean over records (not a mean of per-employee means), exact from integer hundredths
                "avg_work_hours": round_half_up(Fraction(int(r["wh_cents"]), 100 * n), 2) if n else None,
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    return {"month": month, "items": list(logs.aggregate(build_leaderboard_pipeline(first, last, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from", pattern=DATE_PATTERN), to: str = Query(..., pattern=DATE_PATTERN)):
    d_from, d_to = parse_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": list(employees.aggregate(build_trend_pipeline(department, d_from, d_to)))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _require(**params):
    for name, value in params.items():
        if value is None:
            invalid(("query", name), "field required")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs explain(executionStats) on the same find / pipeline the real endpoint builds."""
    if endpoint == "attendance_list":
        collection = logs.name
        command: dict = {"find": collection, **build_attendance_find(emp_code, date_from, date_to, status, page, page_size)}
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            first, last = month_bounds(month)
            collection, pipeline = logs.name, build_employee_monthly_pipeline(emp_code, first, last)
        elif endpoint == "department_summary":
            _require(month=month)
            first, last = month_bounds(month)
            collection, pipeline = employees.name, build_department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            first, last = month_bounds(month)
            collection, pipeline = logs.name, build_leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            _require(department=department, **{"from": from_, "to": to})
            d_from, d_to = parse_trend_range(from_, to)
            collection, pipeline = employees.name, build_trend_pipeline(department, d_from, d_to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    # The raw explain contains BSON types (dates, Int64, ...): round-trip through extended JSON to make it JSON-safe.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
