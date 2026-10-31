# Live Options Dashboard

View-only web dashboard for a weekly bullish / reversal bearish options plan.
Fetches underlying prices and option quotes from Yahoo Finance via a small FastAPI backend.
Frontend polls `/api/snapshot` every 5 seconds. Protected by a username/password sign-in page.

**Not financial advice. No order placement.**

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DASH_ADMIN_PASSWORD=...   # username: abhinav
export DASH_GUEST_PASSWORD=...   # username: guest
export SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/login

## Auth

- Env vars (required for login; missing passwords fail closed — no logins allowed):
  - `DASH_ADMIN_PASSWORD` — user `abhinav` (admin)
  - `DASH_GUEST_PASSWORD` — user `guest` (view-only)
  - `SESSION_SECRET` — signs the HttpOnly session cookie (~30 days)
- Public routes: `/login`, `POST /login`, `/api/health`
- Everything else requires a valid session (pages redirect to `/login`; APIs return 401)

## Render

Free web service: build `pip install -r requirements.txt`, start `uvicorn app:app --host 0.0.0.0 --port $PORT`.
Set the three env vars above in the Render dashboard (never commit secrets; repo is public).
