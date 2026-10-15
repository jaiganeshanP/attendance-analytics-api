"""
Test-data generator + independent checker.  NOT part of the submission (do not commit it, or do: it is harmless).

  python make_test_data.py seed    # small hand-made dataset with the awkward cases (July 2026)
  python make_test_data.py bulk    # ~100k records, for speed / explain checks
  python make_test_data.py check   # compare the running API against an independent Python oracle
  python make_test_data.py time    # time the main endpoints

It only ever writes to the database named TEST_DB (default "attendance_test"), never to MONGO_DB.
To test, start the API against that database:

  export MONGO_DB=attendance_test      # (and MONGO_URI as usual)
  uvicorn app.main:app --port 8000
  python make_test_data.py seed && python make_test_data.py check
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from math import floor

UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
MONTH = "2026-07"
API = os.getenv("API", "http://localhost:8000")
TEST_DB = os.getenv("TEST_DB", "attendance_test")


# --------------------------------------------------------------------------- #
# Data builders
# --------------------------------------------------------------------------- #
def emp(code, dept, joined="2026-01-05", start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": datetime(2026, 1, 5, tzinfo=UTC)}


def rec(code, day, status="PRESENT", late=0, ot=0, wh=9.0, half=False, legacy=False):
    """A record with STORED derived values (the analytics must read these, not recompute them)."""
    present = status in PRESENCE
    pin = datetime.fromisoformat(day).replace(hour=4, tzinfo=UTC) if present else None
    pout = pin + timedelta(hours=wh) if present and wh is not None else None
    d = {"emp_code": code, "date": day, "status": status, "punch_in": pin, "punch_out": pout,
         "work_hours": wh if present else None, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
    if legacy:  # old documents have neither half_day nor history
        del d["half_day"], d["history"]
    return d


def designed():
    emps = [
        emp("EMP9001", "Engineering"), emp("EMP9002", "Engineering", joined="2026-07-15"),  # mid-month joiner
        emp("EMP9003", "Engineering"),                                                    # no logs at all
        emp("EMP9004", "Engineering", start="22:00", end="06:00"),                        # overnight shift
        emp("EMP9101", "Sales"), emp("EMP9102", "Sales"), emp("EMP9103", "Sales"), emp("EMP9104", "Sales"),
        emp("EMP9201", "Ops"), emp("EMP9202", "Ops", joined="2026-08-10"),                # joins after July
        emp("EMP9301", "Support", joined="2026-08-01"),                                   # headcount 0 in July: omitted
    ]
    r = [
        rec("EMP9001", "2026-07-01", late=0, wh=9.12), rec("EMP9001", "2026-07-02", status="WFH", late=35, ot=40, wh=9.08),
        rec("EMP9001", "2026-07-03", status="ON_DUTY", wh=8.0), rec("EMP9001", "2026-07-06", wh=3.5, half=True),
        rec("EMP9001", "2026-07-07", status="LEAVE"), rec("EMP9001", "2026-07-08", status="ABSENT"),
        rec("EMP9001", "2026-07-04", late=20, ot=30, wh=8.0),            # Saturday: counts for late/overtime, not present_days
        rec("EMP9001", "2026-07-09", legacy=True, wh=8.98),               # legacy document
        rec("EMP9002", "2026-07-15"), rec("EMP9002", "2026-07-16", late=12),
        rec("EMP9004", "2026-07-06", ot=40, wh=8.75), rec("EMP9004", "2026-07-07", late=300, wh=8.0),
        # Sales: a tie for rank 1 (100, 100), then 70, then 30 -> ranks 1, 1, 3, 4
        rec("EMP9101", "2026-07-01", late=60), rec("EMP9101", "2026-07-02", late=40),
        rec("EMP9102", "2026-07-01", late=100),
        rec("EMP9103", "2026-07-01", late=50), rec("EMP9103", "2026-07-04", late=20),
        rec("EMP9104", "2026-07-03", late=30),
        rec("EMP9201", "2026-07-01", wh=9.0), rec("EMP9201", "2026-07-02", status="ON_DUTY", wh=7.0),
        rec("EMP0000", "2026-07-01", late=500),                           # log of an unknown employee: ignored
    ]
    return emps, r


def bulk(n_emp=2000, days=72, n_dept=20):
    rnd = random.Random(1)
    emps = [emp(f"EMP{10000 + i}", f"Dept{i % n_dept:02d}", joined=f"2026-0{rnd.choice([1, 2, 3, 7])}-{rnd.randint(1, 28):02d}") for i in range(n_emp)]
    recs = []
    start = date(2026, 6, 1)
    for e in emps:
        for k in range(days):
            day = (start + timedelta(days=k)).isoformat()
            if day < e["joined_on"] or rnd.random() < 0.1:
                continue
            x = rnd.random()
            if x < 0.05:
                recs.append(rec(e["emp_code"], day, status=rnd.choice(["ABSENT", "LEAVE"])))
            else:
                late = rnd.choice([0, 0, 0, 12, 25, 60])
                wh = round(rnd.uniform(3, 10), 2)
                recs.append(rec(e["emp_code"], day, status=rnd.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY"]), late=late,
                                ot=rnd.choice([0, 0, 35, 70]), wh=wh, half=wh < 4.5))
    return emps, recs


# --------------------------------------------------------------------------- #
# Independent oracle (plain Python over the documents; deliberately simple)
# --------------------------------------------------------------------------- #
def rhu(x, places):
    return floor(Fraction(x) * 10**places + Fraction(1, 2)) / 10**places


def weekday(day):
    return date.fromisoformat(day).weekday() < 5


def halves(r):  # presence in half-day units, Mon-Fri only
    if r["status"] in PRESENCE and weekday(r["date"]):
        return 1 if r.get("half_day", False) else 2
    return 0


def bounds(month):
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    return first, date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def in_month(r, month):
    f, l = bounds(month)
    return f.isoformat() <= r["date"] <= l.isoformat()


def oracle_monthly(e, recs, month):
    f, l = bounds(month)
    rs = [r for r in recs if r["emp_code"] == e["emp_code"] and in_month(r, month)]
    start, wd = max(f, date.fromisoformat(e["joined_on"])), 0
    d = start
    while d <= l:
        wd += d.weekday() < 5
        d += timedelta(days=1)
    h = sum(halves(r) for r in rs)
    return {"emp_code": e["emp_code"], "month": month, "working_days": wd, "present_days": h / 2,
            "leave_days": sum(r["status"] == "LEAVE" for r in rs), "late_count": sum(r.get("late_minutes", 0) > 0 for r in rs),
            "total_late_minutes": sum(r.get("late_minutes", 0) for r in rs),
            "total_overtime_minutes": sum(r.get("overtime_minutes", 0) for r in rs),
            "attendance_pct": rhu(Fraction(h, 2) / wd * 100, 2) if wd else None}


def oracle_summary(emps, recs, month):
    _, l = bounds(month)
    out = []
    for dept in sorted({e["department"] for e in emps}):
        es = [e for e in emps if e["department"] == dept and e["joined_on"] <= l.isoformat()]
        if not es:
            continue
        codes = {e["emp_code"] for e in es}
        rs = [r for r in recs if r["emp_code"] in codes and in_month(r, month)]
        whs = [Fraction(str(r["work_hours"])) for r in rs if r["status"] in PRESENCE and r.get("work_hours") is not None]
        out.append({"department": dept, "headcount": len(es), "present_days": sum(halves(r) for r in rs) / 2,
                    "avg_work_hours": rhu(sum(whs) / len(whs), 2) if whs else None,
                    "late_count": sum(r.get("late_minutes", 0) > 0 for r in rs),
                    "total_late_minutes": sum(r.get("late_minutes", 0) for r in rs),
                    "leave_count": sum(r["status"] == "LEAVE" for r in rs), "on_duty_count": sum(r["status"] == "ON_DUTY" for r in rs)})
    return out


def oracle_leaderboard(emps, recs, month, limit, dept=None):
    by = {e["emp_code"]: e for e in emps if dept is None or e["department"] == dept}
    tot = {}
    for r in recs:
        if r["emp_code"] in by and in_month(r, month) and r.get("late_minutes", 0) > 0:
            t = tot.setdefault(r["emp_code"], [0, 0])
            t[0] += r["late_minutes"]
            t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for i, (code, (mins, cnt)) in enumerate(rows):
        rank = 1 + sum(1 for _, (m2, _) in rows if m2 > mins)  # standard competition ranking
        if rank <= limit:
            out.append({"rank": rank, "emp_code": code, "name": by[code]["name"], "department": by[code]["department"],
                        "total_late_minutes": mins, "late_count": cnt})
    return out


def oracle_trend(emps, recs, dept, d_from, d_to):
    es = [e for e in emps if e["department"] == dept]
    codes = {e["emp_code"] for e in es}
    rows, rates, day = [], [], d_from
    while day <= d_to:
        s = day.isoformat()
        h = sum(e["joined_on"] <= s for e in es)
        rs = [r for r in recs if r["emp_code"] in codes and r["date"] == s]
        halves_sum = sum(2 if (r["status"] in PRESENCE and not r.get("half_day", False)) else 1 if r["status"] in PRESENCE else 0 for r in rs)
        rate = floor(Fraction(halves_sum, 2) / h * 10000 + Fraction(1, 2)) if (day.weekday() < 5 and h > 0) else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        rows.append({"date": s, "is_working_day": day.weekday() < 5, "headcount": h, "present_count": halves_sum / 2,
                     "late_count": sum(r.get("late_minutes", 0) > 0 for r in rs),
                     "attendance_rate": None if rate is None else rate / 10000,
                     "moving_avg_7d": floor(Fraction(sum(window), len(window)) + Fraction(1, 2)) / 10000 if window else None})
        day += timedelta(days=1)
    return rows


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def get_db():
    from pymongo import MongoClient
    if "test" not in TEST_DB:  # safety: this script deletes everything in the database it loads into
        sys.exit(f"refusing to wipe '{TEST_DB}': TEST_DB must contain the word 'test'")
    return MongoClient(os.environ["MONGO_URI"], tz_aware=True)[TEST_DB]


def load(emps, recs):
    d = get_db()
    d.employees.delete_many({})
    d.attendance_logs.delete_many({})
    d.employees.insert_many(emps)
    for i in range(0, len(recs), 5000):
        d.attendance_logs.insert_many(recs[i:i + 5000])
    print(f"loaded {len(emps)} employees and {len(recs)} records into '{TEST_DB}'.")
    print("Now (re)start the API with MONGO_DB=" + TEST_DB + " so it creates its indexes on this data.")


def api_get(path, **params):
    url = f"{API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return {"__error__": exc.code, "body": exc.read().decode()[:300]}
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach the API at {API}: {exc.reason}. Start it first (see the instructions at the top of this file).")


def same(a, b):
    if isinstance(b, float):  # numbers: compare with a tiny tolerance; anything else is a mismatch, never a crash
        return isinstance(a, (int, float)) and not isinstance(a, bool) and abs(a - b) < 1e-9
    return a == b


def diff(actual, expected, path=""):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object, got {actual!r}"]
        return [d for k in expected for d in diff(actual.get(k, "<missing>"), expected[k], f"{path}.{k}")]
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [f"{path}: expected {len(expected)} items, got {len(actual) if isinstance(actual, list) else actual!r}"]
        return [d for i, (x, y) in enumerate(zip(actual, expected)) for d in diff(x, y, f"{path}[{i}]")]
    return [] if same(actual, expected) else [f"{path}: got {actual!r}, expected {expected!r}"]


def check():
    d = get_db()
    emps = list(d.employees.find({}, {"_id": 0}))
    recs = list(d.attendance_logs.find({}, {"_id": 0}))
    for r in recs:
        r["date"] = str(r["date"])
    failures = 0

    def report(name, problems):
        nonlocal failures
        failures += bool(problems)
        print(("PASS  " if not problems else "FAIL  ") + name)
        for p in problems[:5]:
            print("        " + p)

    sample = emps if len(emps) < 100 else emps[:40]
    for e in sample:
        report(f"monthly {e['emp_code']}", diff(api_get(f"/analytics/employees/{e['emp_code']}/monthly", month=MONTH), oracle_monthly(e, recs, MONTH)))
    report("department summary", diff(api_get("/analytics/departments/summary", month=MONTH).get("items"), oracle_summary(emps, recs, MONTH)))
    depts = sorted({e["department"] for e in emps})
    for limit in (1, 2, 3, 10):
        report(f"leaderboard limit={limit}", diff(api_get("/analytics/leaderboard/late", month=MONTH, limit=limit).get("items"), oracle_leaderboard(emps, recs, MONTH, limit)))
    report(f"leaderboard dept={depts[0]}", diff(api_get("/analytics/leaderboard/late", month=MONTH, limit=5, department=depts[0]).get("items"), oracle_leaderboard(emps, recs, MONTH, 5, depts[0])))
    for dept in depts[:3]:
        f, t = (date(2026, 7, 1), date(2026, 7, 31)) if len(emps) < 100 else (date(2026, 6, 1), date(2026, 7, 20))
        report(f"trend {dept} {f}..{t}", diff(api_get(f"/analytics/departments/{dept}/trend", **{"from": f.isoformat(), "to": t.isoformat()}).get("items"), oracle_trend(emps, recs, dept, f, t)))
    print(f"\n{failures} failing check(s)")


def timing():
    for name, path, params in [
        ("attendance list", "/attendance", {"page": 1}), ("attendance list deep page", "/attendance", {"page": 500}),
        ("attendance by employee", "/attendance", {"emp_code": "EMP10001"}),
        ("department summary", "/analytics/departments/summary", {"month": MONTH}),
        ("late leaderboard", "/analytics/leaderboard/late", {"month": MONTH}),
        ("department trend", "/analytics/departments/Dept01/trend", {"from": "2026-06-01", "to": "2026-08-31"}),
    ]:
        t0 = time.perf_counter()
        out = api_get(path, **params)
        ms = (time.perf_counter() - t0) * 1000
        print(f"{ms:8.0f} ms  {name}" + ("   <-- ERROR " + str(out) if isinstance(out, dict) and "__error__" in out else ""))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        load(*designed())
    elif cmd == "bulk":
        load(*bulk())
    elif cmd == "check":
        check()
    elif cmd == "time":
        timing()
    else:
        sys.exit(__doc__)
