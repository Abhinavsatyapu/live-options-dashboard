# Live Options Dashboard

View-only web dashboard for a weekly bullish / reversal bearish options plan.
Fetches underlying prices and option quotes from Yahoo Finance via a small FastAPI backend (CORS requires a server). Frontend polls `/api/snapshot` every 5 seconds.

**Not financial advice. No order placement.**

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000

## Render

Free web service: build `pip install -r requirements.txt`, start `uvicorn app:app --host 0.0.0.0 --port $PORT`.
