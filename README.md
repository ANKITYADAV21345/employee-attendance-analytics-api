# Employee Attendance & Analytics API

FastAPI + MongoDB backend for employee attendance tracking, manual regularization audit trails, and MongoDB aggregation pipeline analytics.

## Technology Stack
- **Python 3.11+** (FastAPI, PyMongo, Pydantic v2)
- **MongoDB 6.0+** (Aggregation Pipelines, Indexing, $densify, $setWindowFields)

## Features & Implementation Highlights
- **Business Rules R1-R10 Enforced**:
  - IST timezone handling (UTC+05:30) & overnight shift cutoff calculation.
  - 10-minute grace period late calculation.
  - Overtime calculation with >= 30 min minimum threshold.
  - Work hours rounded 2 decimals half-up (`Decimal` quantization).
  - Half day detection (`work_hours < 4.50`).
  - Mon-Fri working days counting with mid-month joiner support.
- **Concurrency & Race Safety**:
  - Database-enforced unique compound indexes (`emp_code`, `date`) ensuring atomic punch-ins and 409 handling under concurrent load.
  - Optimistic locking on history array updates for regularizations.
- **MongoDB Aggregation Pipelines**:
  - Monthly employee summary.
  - Cross-collection department summary with headcount inclusion for employees with 0 logs.
  - Competition ranking late leaderboard with tie handling (`$rank`, `$setWindowFields`).
  - Daily department attendance trend with gap filling (`$densify`) and 7-day moving average.
- **Admin Query Profiling**:
  - `GET /admin/explain/{endpoint}` running `executionStats` query profiling to verify index coverage (`IXSCAN`).

## Local Setup & Run

1. **Activate Virtual Environment & Install Dependencies**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   Copy or configure `.env`:
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

3. **Seed Sample Data**:
   ```bash
   python sample_seed.py
   ```

4. **Start Application Server**:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

5. **Access Interactive API Docs**:
   - Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)

## Running Unit & Integration Tests

Run the complete test suite:
```bash
python -m unittest discover tests
```
