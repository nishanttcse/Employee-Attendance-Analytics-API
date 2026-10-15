# Employee Attendance & Analytics API

FastAPI service for employee attendance, regularization, and analytics using MongoDB. The API contract and business rules are in `openapi.yaml`; the stored document shapes are in `DATA_MODEL.md`.

## Run locally (Windows PowerShell)

Requires Python 3.11+ and MongoDB 6.0+.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
docker run -d --name attendance-mongo -p 27017:27017 -v attendance-mongo-data:/data/db mongo:7
python sample_seed.py
uvicorn app.main:app --reload --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in `.env`; Atlas connection strings work too. `sample_seed.py` clears and reloads the `employees` and `attendance_logs` collections, so use it only with a disposable development database. Open `http://localhost:8000/docs` for the API.

## GitHub submission

Push the application, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README. The contract, data model, and sample seed files are retained as useful project documentation and test data. Do not commit `.env`, credentials, or database secrets.
