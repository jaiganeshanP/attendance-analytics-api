# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or copy .env.example to .env
python sample_seed.py            # optional: load the nine sample documents
uvicorn app.main:app --port 8000
```

Indexes are created at startup (idempotently), and again from `/health` if MongoDB was not up yet.

## Notes

* Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`.
* The three hard analytics endpoints are single aggregation pipelines: department summary (`$lookup` from `employees`), late leaderboard (`$setWindowFields` + `$rank`) and department trend (`$densify` + two `$setWindowFields`). Numbers that need half-up rounding are computed with integer arithmetic, because MongoDB's `$round` rounds half to even.
* Business rules R1-R5 live in `derive`, `attendance_day` and `shift_window`.
* `REVIEW.md` lists the defects I found in the starter; `DECISIONS.md` answers the five questions.

## Known limits

* The analytics and explain pipelines (especially `$densify` / `$setWindowFields` in the trend) were written against the MongoDB 6/7 documentation but have **not been run against a live MongoDB** by me. Run the seed script and try each endpoint before relying on them.
* Offset pagination (`skip`) is used for lists, as the contract specifies `page` / `page_size`.
