# Live Options Dashboard

View-only web dashboard for weekly ★ bullish, all squeeze setups, weekly ★ bearish, and reversal bearish options plans (structures from Sep14–Oct30 BS backtest).
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


## My Positions + exit alerts + earnings

- Tab **My Positions**: admin (`abhinav`) can add/edit/close; guest is read-only.
- Persistence: Neon Postgres via `DATABASE_URL` (set on Render; never commit).
- Exit monitor (from the box that can reach Neon + Yahoo):
  ```bash
  python /workspace/check_exits.py
  # reads DATABASE_URL or /workspace/.secrets/options_db_url
  # optional fixture: python /workspace/check_exits.py --positions-file /tmp/open.json
  ```
  Stdout JSON: `{ ok, checked_at, open, alerts, new_alerts, alert_kinds }`.
  Dedupe state: `/workspace/exit_alert_state.json` (same alert key not re-emitted in `new_alerts`).
- Earnings-before-expiry badges on signal/position/Early★ cards (Yahoo, cached daily in Neon + local file).
