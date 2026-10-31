"""Live options plan dashboard — Yahoo Finance quotes + option chains."""
from __future__ import annotations

import json

import hashlib
import math
import os
import secrets
import threading
import time
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
IST = ZoneInfo("Asia/Calcutta")
ET = ZoneInfo("America/New_York")
CACHE_TTL = 4.0
R_RATE = 0.05

# Signals as of Fri 2026-10-30 close — structures from Sep14–Oct30 options backtest
from trade_config import WEEKLY_SIGNALS, REVERSAL_BEARISH

_EARLY_PATH = Path(__file__).parent / "early_current.json"
_EARLY_RULES_PATH = Path(__file__).parent / "early_rules.json"

def load_early():
    try:
        return json.loads(_EARLY_PATH.read_text())
    except Exception:
        return {"sure_shots": [], "forming_watch": [], "counts": {}, "rules": {}}


# Back-compat alias used by snapshot builder
WEEKLY_BULLISH = WEEKLY_SIGNALS

WATCHLIST = [
    {"symbol": "AAOI", "low": 65.77, "high": 74.25},
    {"symbol": "AAPL", "low": 334.35, "high": 349.19},
    {"symbol": "AMAT", "low": 382.62, "high": 405.20},
    {"symbol": "AMD", "low": 492.21, "high": 520.40},
    {"symbol": "BMNR", "low": 19.60, "high": 20.77},
    {"symbol": "CRCL", "low": 71.00, "high": 74.80},
    {"symbol": "CRWD", "low": 245.15, "high": 264.90},
    {"symbol": "CRWV", "low": 53.48, "high": 56.90},
    {"symbol": "CVNA", "low": 66.43, "high": 69.84},
    {"symbol": "GEV", "low": 844.08, "high": 886.07},
    {"symbol": "HIMS", "low": 26.68, "high": 28.75},
    {"symbol": "IBM", "low": 254.71, "high": 259.59},
    {"symbol": "INOD", "low": 42.56, "high": 44.64},
    {"symbol": "INTC", "low": 63.20, "high": 68.77},
    {"symbol": "IONQ", "low": 28.55, "high": 30.37},
    {"symbol": "MDB", "low": 341.16, "high": 371.49},
    {"symbol": "NET", "low": 271.63, "high": 299.69},
    {"symbol": "PLTR", "low": 180.81, "high": 193.95},
    {"symbol": "QBTS", "low": 10.23, "high": 11.09},
    {"symbol": "QCOM", "low": 161.72, "high": 176.17},
    {"symbol": "RBLX", "low": 41.36, "high": 47.76},
    {"symbol": "RGTI", "low": 10.82, "high": 11.52},
    {"symbol": "SNDK", "low": 1115.16, "high": 1239.00},
    {"symbol": "SQQQ", "low": 40.55, "high": 42.96},
    {"symbol": "TER", "low": 266.36, "high": 284.16},
]

BANNER = {
    "text": "SPY negative gamma (gex -0.75, structure COLLAPSED) — size down.",
    "gex": -0.75,
}


class YahooClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA})
        self._crumb: Optional[str] = None
        self._crumb_ts = 0.0

    def _refresh_auth(self) -> None:
        self._session.cookies.clear()
        self._session.get("https://fc.yahoo.com", timeout=15)
        r = self._session.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15
        )
        r.raise_for_status()
        self._crumb = r.text.strip()
        self._crumb_ts = time.time()

    def ensure_auth(self) -> str:
        with self._lock:
            if not self._crumb or (time.time() - self._crumb_ts) > 3600:
                self._refresh_auth()
            assert self._crumb
            return self._crumb

    def _get(self, url: str, params: Optional[dict] = None) -> Any:
        crumb = self.ensure_auth()
        p = dict(params or {})
        p["crumb"] = crumb
        r = self._session.get(url, params=p, timeout=20)
        if r.status_code == 401:
            with self._lock:
                self._refresh_auth()
                crumb = self._crumb
            p["crumb"] = crumb
            r = self._session.get(url, params=p, timeout=20)
        r.raise_for_status()
        return r.json()

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        # batch in chunks of 40
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 40):
            chunk = symbols[i : i + 40]
            data = self._get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                {"symbols": ",".join(chunk)},
            )
            for q in data.get("quoteResponse", {}).get("result", []) or []:
                sym = q.get("symbol")
                if sym:
                    out[sym] = q
        return out

    def options_chain(self, symbol: str, expiry_unix: int) -> dict:
        return self._get(
            f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}",
            {"date": expiry_unix},
        )


yahoo = YahooClient()
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def expiry_unix(expiry_str: str) -> int:
    y, m, d = map(int, expiry_str.split("-"))
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def years_to_expiry(expiry_str: str) -> float:
    y, m, d = map(int, expiry_str.split("-"))
    exp = datetime(y, m, d, 16, 0, tzinfo=ET)  # ~4pm ET expiration
    now = datetime.now(timezone.utc)
    secs = (exp - now).total_seconds()
    return max(secs / (365.25 * 24 * 3600), 1e-6)


def mid_price(opt: Optional[dict]) -> Optional[float]:
    if not opt:
        return None
    bid = opt.get("bid")
    ask = opt.get("ask")
    last = opt.get("lastPrice")
    if bid is not None and ask is not None and bid >= 0 and ask > 0:
        return round((bid + ask) / 2.0, 4)
    if last is not None and last > 0:
        return float(last)
    if bid is not None and bid > 0:
        return float(bid)
    return None


def find_contract(options: list[dict], strike: float) -> Optional[dict]:
    best = None
    best_diff = 1e18
    for o in options:
        s = o.get("strike")
        if s is None:
            continue
        diff = abs(float(s) - strike)
        if diff < best_diff:
            best_diff = diff
            best = o
    return best


def find_atm(options: list[dict], spot: float) -> Optional[dict]:
    if not options or spot is None:
        return None
    return min(options, key=lambda o: abs(float(o.get("strike") or 0) - spot))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(
    S: float, K: float, T: float, r: float, sigma: float, side: str
) -> Optional[float]:
    if S <= 0 or K <= 0 or T <= 0 or sigma is None or sigma <= 0:
        return None
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (
            sigma * math.sqrt(T)
        )
        if side == "call":
            return round(norm_cdf(d1), 4)
        return round(norm_cdf(d1) - 1.0, 4)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def underlying_price(q: Optional[dict]) -> Optional[float]:
    if not q:
        return None
    for key in (
        "regularMarketPrice",
        "postMarketPrice",
        "preMarketPrice",
        "bid",
        "ask",
    ):
        v = q.get(key)
        if v is not None and v > 0:
            return float(v)
    return None


def pct_change(q: Optional[dict]) -> Optional[float]:
    if not q:
        return None
    v = q.get("regularMarketChangePercent")
    return float(v) if v is not None else None


def chain_legs(chain_json: dict) -> tuple[Optional[dict], list[dict], list[dict]]:
    try:
        result = chain_json["optionChain"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None, [], []
    quote = result.get("quote") or {}
    opts = result.get("options") or []
    if not opts:
        return quote, [], []
    calls = opts[0].get("calls") or []
    puts = opts[0].get("puts") or []
    return quote, calls, puts



def enrich_spread(plan: dict, quote: dict, calls: list, puts: list) -> dict:
    side = plan["side"]
    legs = calls if side == "call" else puts
    spot = underlying_price(quote)
    chg = pct_change(quote)
    is_atm = bool(plan.get("is_atm")) or (
        plan.get("structure") == "atm"
        or plan.get("short_strike") is None
        or (
            plan.get("short_strike") is not None
            and abs(float(plan["short_strike"]) - float(plan["long_strike"])) < 1e-9
            and plan.get("structure") == "atm"
        )
    )
    long_leg = find_contract(legs, plan["long_strike"])
    short_leg = None if is_atm else find_contract(legs, plan["short_strike"])
    long_mid = mid_price(long_leg)
    short_mid = mid_price(short_leg) if short_leg else None
    spread_mid = None
    if is_atm:
        spread_mid = long_mid
    elif long_mid is not None and short_mid is not None:
        spread_mid = round(long_mid - short_mid, 4)

    debit = plan.get("debit")
    if debit is None and spread_mid is not None:
        debit = spread_mid
    debit = float(debit) if debit is not None else 0.0

    if is_atm:
        width = None
        max_profit = None  # uncapped
        if side == "call":
            breakeven = round(plan["long_strike"] + debit, 4)
        else:
            breakeven = round(plan["long_strike"] - debit, 4)
    else:
        width = abs(float(plan["short_strike"]) - float(plan["long_strike"]))
        max_profit = round(width - debit, 4)
        if side == "call":
            breakeven = round(plan["long_strike"] + debit, 4)
        else:
            breakeven = round(plan["long_strike"] - debit, 4)

    pnl_per = None
    pnl_total = None
    if spread_mid is not None:
        pnl_per = round(spread_mid - debit, 4)
        pnl_total = round(pnl_per * 100 * plan.get("size", 0), 2)

    iv = None
    if long_leg and long_leg.get("impliedVolatility") is not None:
        iv = float(long_leg["impliedVolatility"])

    T = years_to_expiry(plan["expiry"])
    delta = None
    if spot is not None and iv is not None:
        delta = bs_delta(spot, plan["long_strike"], T, R_RATE, iv, side)

    atm_leg = find_atm(legs, spot) if spot else None
    atm = None
    if atm_leg:
        atm = {
            "strike": atm_leg.get("strike"),
            "bid": atm_leg.get("bid"),
            "ask": atm_leg.get("ask"),
            "last": atm_leg.get("lastPrice"),
            "mid": mid_price(atm_leg),
            "iv": atm_leg.get("impliedVolatility"),
            "contractSymbol": atm_leg.get("contractSymbol"),
        }

    status = None
    dist_trigger = None
    dist_sl = None
    if plan.get("already_in"):
        status = "IN"
        if plan.get("cover_above") is not None and spot is not None:
            dist_trigger = round(spot - plan["cover_above"], 4)
    elif plan.get("trigger") is not None and spot is not None:
        dist_trigger = round(spot - plan["trigger"], 4)
        dist_sl = round(plan["sl"] - spot, 4) if plan.get("sl") else None
        if spot < plan["trigger"]:
            status = "TRIGGERED"
        elif plan.get("sl") is not None and spot > plan["sl"]:
            status = "STOPPED"
        else:
            status = "WAITING"

    short_occ = plan.get("short_occ") or ""
    long_occ = plan.get("long_occ") or ""

    return {
        **{k: plan[k] for k in plan},
        "is_atm": is_atm,
        "structure_label": "ATM " + side if is_atm else ("bull call debit" if side == "call" else "bear put debit"),
        "price": spot,
        "changePct": chg,
        "long": {
            "bid": long_leg.get("bid") if long_leg else None,
            "ask": long_leg.get("ask") if long_leg else None,
            "last": long_leg.get("lastPrice") if long_leg else None,
            "mid": long_mid,
            "iv": iv,
            "contractSymbol": long_leg.get("contractSymbol") if long_leg else long_occ,
        },
        "short": {
            "bid": short_leg.get("bid") if short_leg else None,
            "ask": short_leg.get("ask") if short_leg else None,
            "last": short_leg.get("lastPrice") if short_leg else None,
            "mid": short_mid,
            "iv": short_leg.get("impliedVolatility") if short_leg else None,
            "contractSymbol": short_leg.get("contractSymbol") if short_leg else short_occ,
        },
        "spreadMid": spread_mid,
        "pnlPerSpread": pnl_per,
        "pnlTotal": pnl_total,
        "maxProfit": max_profit,
        "breakeven": breakeven,
        "delta": delta,
        "atm": atm,
        "status": status,
        "distTrigger": dist_trigger,
        "distSL": dist_sl,
        "width": width,
    }




def enrich_early(early: dict, quotes: dict, chains: dict) -> list:
    """Attach live marks to Tier-S early sure shots; map to known plan OCC when present."""
    plans = {}
    for p in WEEKLY_BULLISH + REVERSAL_BEARISH:
        sym = p["symbol"]
        # Prefer recommended / non-skip / larger size when duplicates exist
        prev = plans.get(sym)
        if prev is None:
            plans[sym] = p
        elif p.get("recommended") and not prev.get("recommended"):
            plans[sym] = p
        elif (not p.get("skip")) and prev.get("skip"):
            plans[sym] = p
        elif (p.get("size") or 0) > (prev.get("size") or 0):
            plans[sym] = p
    out = []
    for s in early.get("sure_shots") or []:
        sym = s.get("sym")
        plan = plans.get(sym)
        spot = underlying_price(quotes.get(sym) or {})
        chg = pct_change(quotes.get(sym) or {})
        row = {**s, "price": spot, "changePct": chg}
        if plan:
            q, calls, puts = chains.get((plan["symbol"], plan["expiry"]), (quotes.get(sym), [], []))
            enriched = enrich_spread(plan, q or quotes.get(sym) or {}, calls, puts)
            row["plan"] = {
                "expiry": plan.get("expiry"),
                "structure": enriched.get("structure_label"),
                "long_occ": plan.get("long_occ"),
                "short_occ": plan.get("short_occ") if not plan.get("is_atm") else "",
                "debit": plan.get("debit"),
                "spreadMid": enriched.get("spreadMid"),
                "breakeven": enriched.get("breakeven"),
                "maxProfit": enriched.get("maxProfit"),
                "side": plan.get("side"),
                "is_atm": plan.get("is_atm"),
                "long_strike": plan.get("long_strike"),
                "short_strike": plan.get("short_strike"),
                "size": plan.get("size"),
            }
        else:
            row["plan"] = None
        out.append(row)
    return out



def build_snapshot() -> dict:
    opt_jobs: dict[tuple[str, str], None] = {}
    for p in WEEKLY_BULLISH + REVERSAL_BEARISH:
        opt_jobs[(p["symbol"], p["expiry"])] = None

    watch_syms = [w["symbol"] for w in WATCHLIST]
    all_quote_syms = sorted(
        set([p["symbol"] for p in WEEKLY_BULLISH + REVERSAL_BEARISH] + watch_syms + ["SPY"])
    )

    quotes = yahoo.quotes(all_quote_syms)

    chains: dict[tuple[str, str], tuple] = {}
    errors: list[str] = []
    for sym, exp in opt_jobs:
        try:
            raw = yahoo.options_chain(sym, expiry_unix(exp))
            chains[(sym, exp)] = chain_legs(raw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym} {exp}: {e}")
            chains[(sym, exp)] = (quotes.get(sym), [], [])

    weekly = []
    for p in WEEKLY_BULLISH:
        q, calls, puts = chains.get((p["symbol"], p["expiry"]), (quotes.get(p["symbol"]), [], []))
        quote = q or quotes.get(p["symbol"]) or {}
        weekly.append(enrich_spread(p, quote, calls, puts))

    weekly_bullish = [r for r in weekly if r.get("section") == "bullish"]
    weekly_squeeze = [r for r in weekly if r.get("section") == "squeeze"]
    weekly_bearish = [r for r in weekly if r.get("section") == "bearish_weekly"]

    reversals = []
    for p in REVERSAL_BEARISH:
        q, calls, puts = chains.get((p["symbol"], p["expiry"]), (quotes.get(p["symbol"]), [], []))
        quote = q or quotes.get(p["symbol"]) or {}
        reversals.append(enrich_spread(p, quote, calls, puts))

    watch = []
    for w in WATCHLIST:
        q = quotes.get(w["symbol"]) or {}
        px = underlying_price(q)
        chg = pct_change(q)
        if px is None:
            status = "N/A"
        elif px > w["high"]:
            status = "above high"
        elif px < w["low"]:
            status = "below low"
        else:
            status = "between"
        watch.append({**w, "price": px, "changePct": chg, "status": status})

    spy_q = quotes.get("SPY") or {}
    now = datetime.now(timezone.utc)
    return {
        "ok": True,
        "updatedAt": now.isoformat(),
        "updatedIST": now.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "updatedET": now.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        "banner": BANNER,
        "spy": {
            "price": underlying_price(spy_q),
            "changePct": pct_change(spy_q),
        },
        "weeklyBullish": weekly_bullish,
        "weeklySqueeze": weekly_squeeze,
        "weeklyBearishStar": weekly_bearish,
        "reversalBearish": reversals,
        "watchlist": watch,
        "earlySureShots": enrich_early(load_early(), quotes, chains),
        "earlyRules": (load_early().get("rules") or {}),
        "signalsAsOf": "2026-10-30 close",
        "backtestNote": (
            "Standing rule: bullish ONLY from Weekly; bearish ONLY from Reversals "
            "(Weekly BEARISH reads never used). Structures from BS backtest Sep14–Oct30 @ $1k equal risk: "
            "BULLISH★ → ATM call 1w; SQ+VC3★ → 1EM call debit 1w; SQ → ATM call 1w; "
            "SQ+VC → skip/micro; Reversals → ATM put (spread if ATM rich). Early ★ tab = Tier-S only (Fri ★/SQ+VC3★ + Reversal ABSORBED). "
            "Modelled IV=RV20×1.15 — not live fills."
        ),
        "disclaimer": (
            "Not financial advice. Yahoo option quotes may be ~15 min delayed "
            "intraday and are Friday-close over the weekend. No order placement."
        ),
        "errors": errors,
    }




app = FastAPI(title="Live Options Dashboard")
STATIC = Path(__file__).parent / "static"

SESSION_MAX_AGE = 30 * 24 * 3600  # ~30 days
SESSION_SECRET = os.environ.get("SESSION_SECRET") or ""
# Secure cookies on Render / HTTPS; allow plain HTTP for local dev
_HTTPS_ONLY = bool(os.environ.get("RENDER")) or os.environ.get("FORCE_SECURE_COOKIE", "").lower() in (
    "1",
    "true",
    "yes",
)

# Fail-closed: empty env => no valid logins
_ADMIN_PASSWORD = os.environ.get("DASH_ADMIN_PASSWORD") or ""
_GUEST_PASSWORD = os.environ.get("DASH_GUEST_PASSWORD") or ""
_USERS: dict[str, dict[str, str]] = {}
if _ADMIN_PASSWORD:
    _USERS["abhinav"] = {"password": _ADMIN_PASSWORD, "role": "admin"}
if _GUEST_PASSWORD:
    _USERS["guest"] = {"password": _GUEST_PASSWORD, "role": "guest"}

# Basic failed-login rate limit: IP -> list of recent failure timestamps
_login_fails: dict[str, list[float]] = {}
_login_lock = threading.Lock()
_FAIL_WINDOW = 300.0  # 5 minutes
_FAIL_LIMIT = 8
_FAIL_DELAY_SEC = 1.25


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _too_many_failures(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        hits = [t for t in _login_fails.get(ip, []) if now - t < _FAIL_WINDOW]
        _login_fails[ip] = hits
        return len(hits) >= _FAIL_LIMIT


def _record_failure(ip: str) -> None:
    now = time.time()
    with _login_lock:
        hits = [t for t in _login_fails.get(ip, []) if now - t < _FAIL_WINDOW]
        hits.append(now)
        _login_fails[ip] = hits


def _clear_failures(ip: str) -> None:
    with _login_lock:
        _login_fails.pop(ip, None)


def _session_user(request: Request) -> Optional[dict[str, str]]:
    user = request.session.get("user")
    role = request.session.get("role")
    if isinstance(user, str) and user and isinstance(role, str) and role:
        return {"user": user, "role": role}
    return None


def _is_public(path: str, method: str) -> bool:
    if path == "/api/health":
        return True
    if path == "/login" and method in ("GET", "POST", "HEAD", "OPTIONS"):
        return True
    return False


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    if _is_public(path, request.method):
        return await call_next(request)
    # Static login assets are not needed; everything else requires a session
    if _session_user(request) is None:
        if path.startswith("/api/") or path.startswith("/static/"):
            # APIs and static files: 401 (pages under /static shouldn't leak HTML)
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


# Session cookie: signed, HttpOnly (Starlette default), SameSite=Lax, ~30d
# Middleware added last so it wraps the auth middleware (outermost).
if not SESSION_SECRET:
    # Still start, but sessions cannot be trusted / logins will fail closed below.
    # Use a random ephemeral secret so the app boots; cookies won't survive restart.
    SESSION_SECRET = secrets.token_urlsafe(32)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="dash_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=_HTTPS_ONLY,
)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/login")
def login_page(request: Request):
    if _session_user(request):
        return RedirectResponse(url="/", status_code=303)
    return FileResponse(STATIC / "login.html")


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    ip = _client_ip(request)
    wants_json = "application/json" in (request.headers.get("accept") or "")

    def fail(status: int, error: str, code: str):
        if wants_json:
            return JSONResponse({"ok": False, "error": error}, status_code=status)
        return RedirectResponse(url=f"/login?e={code}", status_code=303)

    if not _USERS:
        time.sleep(_FAIL_DELAY_SEC)
        return fail(503, "Sign-in is not configured on the server.", "cfg")

    if _too_many_failures(ip):
        time.sleep(_FAIL_DELAY_SEC)
        return fail(429, "Too many attempts. Wait a moment and try again.", "rate")

    user_key = (username or "").strip().lower()
    record = _USERS.get(user_key)
    pw = password or ""
    # Hash then constant-time compare (handles unequal password lengths safely)
    def _pw_digest(s: str) -> bytes:
        return hashlib.sha256(s.encode("utf-8")).digest()

    expected = record["password"] if record else secrets.token_hex(16)
    matched = secrets.compare_digest(_pw_digest(pw), _pw_digest(expected))
    ok = bool(record) and matched

    if not ok:
        _record_failure(ip)
        time.sleep(_FAIL_DELAY_SEC)
        return fail(401, "Invalid username or password.", "1")

    _clear_failures(ip)
    request.session.clear()
    request.session["user"] = user_key
    request.session["role"] = record["role"]
    if wants_json:
        return JSONResponse({"ok": True, "user": user_key, "role": record["role"], "next": "/"})
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    wants_json = "application/json" in (request.headers.get("accept") or "")
    if wants_json:
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/login", status_code=303)


@app.get("/logout")
def logout_get(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/api/me")
def me(request: Request):
    info = _session_user(request)
    if not info:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return {"ok": True, **info}


@app.get("/api/snapshot")
def snapshot(request: Request):
    # Auth enforced by middleware; keep view-only for all roles
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
            return JSONResponse(_cache["data"])
    try:
        data = build_snapshot()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(e), "updatedAt": datetime.now(timezone.utc).isoformat()},
            status_code=502,
        )
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return JSONResponse(data)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# Do not mount /static publicly — login.html & index.html are served via routes.
# (Prevents unauthenticated fetch of /static/index.html)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
