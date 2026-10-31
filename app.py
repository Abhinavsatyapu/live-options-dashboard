"""Live options plan dashboard — Yahoo Finance quotes + option chains."""
from __future__ import annotations

import json

import hashlib
import math
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
CACHE_TTL = 15.0
R_RATE = 0.05

# Signals as of Fri 2026-10-30 close — structures from Sep14–Oct30 options backtest
from trade_config import WEEKLY_SIGNALS, REVERSAL_BEARISH

from positions_live import (
    enrich_positions,
    parse_occ,
    default_rules,
    load_exit_state,
    kinds_for_ticker,
)

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
    """Yahoo market data with Render-safe fallbacks.

    Underlyings: v8/finance/chart (no crumb; works when v7/quote is 401).
    Option chains: v7/options with crumb refresh + host rotation.
    Always keep last-good values so UI never blanks on transient blocks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA})
        self._crumb: Optional[str] = None
        self._crumb_ts = 0.0
        self._backoff_until = 0.0
        self._hosts = (
            "https://query1.finance.yahoo.com",
            "https://query2.finance.yahoo.com",
        )
        self._host_i = 0
        self._quote_cache: dict[str, Any] = {"ts": 0.0, "data": {}}
        self._last_good_quotes: dict[str, dict] = {}
        self._chain_cache: dict[tuple[str, int], Any] = {}
        self._chain_cache_ts: dict[tuple[str, int], float] = {}
        self._last_good_chains: dict[tuple[str, int], Any] = {}
        self._QUOTE_TTL = 12.0
        self._CHAIN_TTL = 25.0
        self._quotes_stale_age: Optional[float] = None

    def _mark_backoff(self, seconds: float = 45.0) -> None:
        self._backoff_until = max(self._backoff_until, time.time() + seconds)

    def _in_backoff(self) -> bool:
        return time.time() < self._backoff_until

    def _next_host(self) -> str:
        h = self._hosts[self._host_i % len(self._hosts)]
        self._host_i += 1
        return h

    def _refresh_auth(self) -> None:
        if self._in_backoff():
            return
        self._session.cookies.clear()
        # Consent / cookie warm-up — helps some Yahoo edges
        for url in (
            "https://finance.yahoo.com/",
            "https://fc.yahoo.com",
        ):
            try:
                self._session.get(url, timeout=12)
            except Exception:
                pass
        last_err: Optional[Exception] = None
        for host in self._hosts:
            try:
                r = self._session.get(f"{host}/v1/test/getcrumb", timeout=12)
                if r.status_code == 429:
                    self._mark_backoff(60.0)
                    last_err = RuntimeError("crumb 429")
                    continue
                if not r.ok:
                    last_err = RuntimeError(f"crumb {r.status_code}")
                    continue
                crumb = r.text.strip()
                if crumb and "html" not in crumb.lower() and len(crumb) < 80:
                    self._crumb = crumb
                    self._crumb_ts = time.time()
                    return
            except Exception as e:  # noqa: BLE001
                last_err = e
        if last_err and not self._crumb:
            # Don't raise — callers soft-fail to last-good / v8
            pass

    def ensure_auth(self) -> str:
        with self._lock:
            if not self._crumb or (time.time() - self._crumb_ts) > 2 * 3600:
                try:
                    self._refresh_auth()
                except Exception:
                    pass
            return self._crumb or ""

    def _get_json(self, path: str, params: Optional[dict] = None, need_crumb: bool = True) -> Any:
        """GET path on rotating hosts; refresh crumb on 401."""
        params = dict(params or {})
        last_status = None
        for attempt in range(4):
            if need_crumb:
                crumb = self.ensure_auth()
                if crumb:
                    params["crumb"] = crumb
                elif self._in_backoff():
                    raise RuntimeError("Yahoo crumb unavailable (backoff)")
            host = self._hosts[(self._host_i + attempt) % len(self._hosts)]
            url = f"{host}{path}"
            r = self._session.get(url, params=params, timeout=18)
            last_status = r.status_code
            if r.status_code == 429:
                with self._lock:
                    self._mark_backoff(60.0)
                time.sleep(0.4 * (attempt + 1))
                continue
            if r.status_code == 401:
                with self._lock:
                    self._crumb = None
                    try:
                        self._refresh_auth()
                    except Exception:
                        pass
                time.sleep(0.25 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Yahoo GET failed status={last_status} path={path}")

    @staticmethod
    def _quote_from_chart(symbol: str, chart_json: dict) -> Optional[dict]:
        try:
            result = (chart_json.get("chart") or {}).get("result") or []
            if not result:
                return None
            meta = result[0].get("meta") or {}
            px = meta.get("regularMarketPrice")
            if px is None:
                return None
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            chg_pct = None
            try:
                if prev and float(prev) > 0:
                    chg_pct = (float(px) / float(prev) - 1.0) * 100.0
            except Exception:
                chg_pct = None
            return {
                "symbol": symbol.upper(),
                "regularMarketPrice": float(px),
                "regularMarketPreviousClose": float(prev) if prev is not None else None,
                "regularMarketChangePercent": chg_pct,
                "regularMarketTime": meta.get("regularMarketTime"),
                "_source": "v8_chart",
                "_fetched_at": time.time(),
            }
        except Exception:
            return None

    def _chart_one(self, symbol: str) -> Optional[dict]:
        sym = symbol.upper()
        last_err = None
        for host in self._hosts:
            try:
                r = self._session.get(
                    f"{host}/v8/finance/chart/{sym}",
                    params={"interval": "1m", "range": "1d"},
                    timeout=15,
                )
                if r.status_code == 429:
                    with self._lock:
                        self._mark_backoff(45.0)
                    time.sleep(0.3)
                    continue
                if not r.ok:
                    last_err = RuntimeError(f"chart {r.status_code}")
                    continue
                q = self._quote_from_chart(sym, r.json())
                if q:
                    return q
            except Exception as e:  # noqa: BLE001
                last_err = e
        if last_err:
            pass
        return None

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        syms = [s.upper() for s in symbols]
        now = time.time()
        with self._lock:
            cached = self._quote_cache.get("data") or {}
            if cached and (now - float(self._quote_cache.get("ts") or 0)) < self._QUOTE_TTL:
                if all(s in cached for s in syms):
                    self._quotes_stale_age = None
                    return {s: cached[s] for s in syms}

        out: dict[str, dict] = {}
        # Prefer crumb-less v8 chart (reliable on Render when v7/quote is 401)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(self._chart_one, s): s for s in syms}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    q = fut.result()
                except Exception:
                    q = None
                if q:
                    out[s] = q

        # Optional enrichment via v7 if crumb works (ignore failures)
        missing = [s for s in syms if s not in out]
        if missing and not self._in_backoff():
            try:
                data = self._get_json(
                    "/v7/finance/quote",
                    {"symbols": ",".join(missing[:40])},
                    need_crumb=True,
                )
                for q in data.get("quoteResponse", {}).get("result", []) or []:
                    sym = q.get("symbol")
                    if sym:
                        q = dict(q)
                        q["_source"] = "v7_quote"
                        q["_fetched_at"] = time.time()
                        out[sym] = q
            except Exception:
                pass

        with self._lock:
            for s, q in out.items():
                self._last_good_quotes[s] = q
            # Fill holes from last-good
            stale_ages = []
            for s in syms:
                if s not in out and s in self._last_good_quotes:
                    lg = dict(self._last_good_quotes[s])
                    age = time.time() - float(lg.get("_fetched_at") or now)
                    lg["_stale"] = True
                    lg["_age_sec"] = age
                    out[s] = lg
                    stale_ages.append(age)
            merged = dict(self._quote_cache.get("data") or {})
            merged.update(out)
            self._quote_cache = {"ts": time.time(), "data": merged}
            self._quotes_stale_age = max(stale_ages) if stale_ages and len(out) == len(stale_ages) else (
                max(stale_ages) if stale_ages else None
            )
        return out

    def quotes_stale_age(self) -> Optional[float]:
        return self._quotes_stale_age

    def options_chain(self, symbol: str, expiry_unix: int) -> dict:
        key = (symbol.upper(), int(expiry_unix))
        now = time.time()
        with self._lock:
            ts = self._chain_cache_ts.get(key, 0.0)
            if key in self._chain_cache and (now - ts) < self._CHAIN_TTL:
                return self._chain_cache[key]
        try:
            data = self._get_json(
                f"/v7/finance/options/{symbol.upper()}",
                {"date": int(expiry_unix)},
                need_crumb=True,
            )
        except Exception:
            with self._lock:
                if key in self._last_good_chains:
                    return self._last_good_chains[key]
            raise
        with self._lock:
            self._chain_cache[key] = data
            self._chain_cache_ts[key] = time.time()
            self._last_good_chains[key] = data
            if len(self._chain_cache) > 80:
                oldest = sorted(self._chain_cache_ts.items(), key=lambda kv: kv[1])[:20]
                for k, _ in oldest:
                    self._chain_cache.pop(k, None)
                    self._chain_cache_ts.pop(k, None)
        return data


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

    errors: list[str] = []
    try:
        quotes = yahoo.quotes(all_quote_syms)
    except Exception as e:  # noqa: BLE001
        errors.append(f"quotes: {e}")
        quotes = {}

    chains: dict[tuple[str, str], tuple] = {}
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

    # Earnings badges + exit-alert badges on signal / early cards
    try:
        from earnings import earnings_before_expiry
    except Exception:
        earnings_before_expiry = None  # type: ignore
    exit_state = load_exit_state()

    def _attach_badges(rows: list, sym_key: str = "symbol") -> list:
        out = []
        for r in rows:
            rr = dict(r)
            sym = rr.get(sym_key) or rr.get("sym") or ""
            exp = rr.get("expiry") or (rr.get("plan") or {}).get("expiry")
            if earnings_before_expiry and sym and exp:
                try:
                    info = earnings_before_expiry(sym, exp)
                    rr["earnings"] = info
                    rr["earningsBadge"] = info.get("badge")
                    if info.get("beforeExpiry"):
                        rr["earningsFlag"] = True
                except Exception:
                    rr["earnings"] = None
            kinds = kinds_for_ticker(str(sym), exit_state)
            # also match open position alert kinds for this ticker
            rr["alertKinds"] = kinds
            out.append(rr)
        return out

    early = enrich_early(load_early(), quotes, chains)
    # Downgrade / flag Early ★ when earnings before expiry
    early_out = []
    for e in early:
        ee = dict(e)
        plan = ee.get("plan") or {}
        exp = plan.get("expiry")
        sym = ee.get("sym") or ""
        if earnings_before_expiry and sym and exp:
            try:
                info = earnings_before_expiry(sym, exp)
                ee["earnings"] = info
                ee["earningsBadge"] = info.get("badge")
                if info.get("beforeExpiry"):
                    ee["earningsFlag"] = True
                    ee["tierNote"] = "EARNINGS before expiry — size down / skip"
            except Exception:
                pass
        ee["alertKinds"] = kinds_for_ticker(str(sym), exit_state)
        early_out.append(ee)

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
        "weeklyBullish": _attach_badges(weekly_bullish),
        "weeklySqueeze": _attach_badges(weekly_squeeze),
        "weeklyBearishStar": _attach_badges(weekly_bearish),
        "reversalBearish": _attach_badges(reversals),
        "watchlist": watch,
        "earlySureShots": early_out,
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
        "quotesSource": "v8_chart",
        "quotesStaleAgeSec": yahoo.quotes_stale_age(),
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



def _require_admin(request: Request):
    info = _session_user(request)
    if not info:
        return None, JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if info.get("role") != "admin":
        return None, JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return info, None


def _parse_position_body(body: dict) -> dict:
    """Normalize position form/JSON into DB fields; prefill rulebook defaults."""
    ticker = (body.get("ticker") or "").strip().upper()
    signal_type = (body.get("signal_type") or body.get("signalType") or "WEEKLY").strip()
    side = (body.get("side") or "").strip().lower()
    structure = (body.get("structure") or "atm").strip().lower()
    long_occ = (body.get("long_occ") or body.get("longOcc") or "").strip().upper().replace(" ", "")
    short_raw = body.get("short_occ") or body.get("shortOcc") or ""
    short_occ = short_raw.strip().upper().replace(" ", "") or None
    quantity = int(body.get("quantity") or 1)
    fill_debit = float(body.get("fill_debit") or body.get("fillDebit") or 0)
    entry_date = (body.get("entry_date") or body.get("entryDate") or date.today().isoformat())[:10]
    notes = body.get("notes")
    if not side:
        side = "put" if ("REV" in signal_type.upper() or "ABSORB" in signal_type.upper()) else "call"
    rules = default_rules(signal_type, side, structure if short_occ else "atm")
    profit_target_pct = body.get("profit_target_pct", body.get("profitTargetPct", rules["profit_target_pct"]))
    time_exit = body.get("time_exit") or body.get("timeExit") or rules["time_exit"]
    stop_underlying = body.get("stop_underlying", body.get("stopUnderlying"))
    cover_level = body.get("cover_level", body.get("coverLevel"))
    expiry = body.get("expiry")
    meta = parse_occ(long_occ)
    if meta and not expiry:
        expiry = meta["expiry_str"]
    if meta and not side:
        side = "call" if meta["cp"] == "C" else "put"
    if short_occ and structure == "atm":
        structure = "spread"
    return {
        "ticker": ticker,
        "signal_type": signal_type,
        "side": side,
        "structure": structure,
        "long_occ": long_occ,
        "short_occ": short_occ,
        "quantity": quantity,
        "fill_debit": fill_debit,
        "entry_date": entry_date,
        "expiry": expiry,
        "stop_underlying": float(stop_underlying) if stop_underlying not in (None, "") else None,
        "profit_target_pct": float(profit_target_pct),
        "time_exit": time_exit,
        "cover_level": float(cover_level) if cover_level not in (None, "") else None,
        "notes": notes,
        "stop_hint": rules["stop_hint"],
    }


@app.on_event("startup")
def _startup_db():
    def _run():
        try:
            from db import ensure_schema, database_url
            if database_url():
                ensure_schema()
        except Exception as e:  # noqa: BLE001
            print(f"[startup] db schema skip: {e}")
    # Neon can be unreachable from some networks — never block boot
    threading.Thread(target=_run, daemon=True).start()


@app.get("/api/positions/defaults")
def positions_defaults(request: Request):
    info = _session_user(request)
    if not info:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    st = (request.query_params.get("signal_type") or "WEEKLY").strip()
    side = (request.query_params.get("side") or "").strip().lower()
    structure = (request.query_params.get("structure") or "atm").strip().lower()
    return {"ok": True, "defaults": default_rules(st, side or ("put" if "REV" in st.upper() else "call"), structure)}


@app.get("/api/positions")
def positions_list(request: Request):
    info = _session_user(request)
    if not info:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    status = request.query_params.get("status", "open")
    if status == "all":
        status = None
    try:
        from db import list_positions
        rows = list_positions(status)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e), "positions": [], "summary": {}}, status_code=503)
    tickers = sorted({r["ticker"].upper() for r in rows})
    quotes = {}
    # week closed for WTD realised
    closed_week = []
    try:
        from db import list_positions as lp
        all_closed = lp("closed")
        # Monday of current week ET
        today = datetime.now(ET).date()
        week_start = today.fromordinal(today.toordinal() - today.weekday())
        for c in all_closed:
            ca = c.get("closed_at") or c.get("updated_at") or ""
            try:
                d = date.fromisoformat(str(ca)[:10])
                if d >= week_start:
                    closed_week.append(c)
            except Exception:
                pass
    except Exception:
        pass
    try:
        quotes = yahoo.quotes(tickers) if tickers else {}
    except Exception as e:  # noqa: BLE001
        quotes = {}
        # continue with empty quotes
        _qerr = str(e)
    else:
        _qerr = None
    try:
        enriched = enrich_positions(yahoo, quotes, rows, closed_week=closed_week)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"enrich: {e}", "positions": rows, "summary": {}}, status_code=500)
    # earnings badges on positions
    try:
        from earnings import earnings_before_expiry
        for p in enriched["positions"]:
            try:
                info_e = earnings_before_expiry(
                    p["ticker"],
                    p.get("expiry"),
                    entry_date=p.get("entry_date"),
                )
                p["earnings"] = info_e
                p["earningsBadge"] = info_e.get("badge")
            except Exception:
                p["earnings"] = None
                p["earningsBadge"] = None
    except Exception:
        pass
    out = {
        "ok": True,
        "role": info["role"],
        "canEdit": info["role"] == "admin",
        **enriched,
    }
    if _qerr:
        out["quoteError"] = _qerr
    return out


@app.post("/api/positions")
async def positions_create(request: Request):
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    data = _parse_position_body(body)
    if not data["ticker"] or not data["long_occ"] or data["fill_debit"] <= 0:
        return JSONResponse({"ok": False, "error": "ticker, long_occ, fill_debit required"}, status_code=400)
    try:
        from db import create_position
        row = create_position(data)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    return {"ok": True, "position": row}


@app.patch("/api/positions/{pid}")
async def positions_update(pid: int, request: Request):
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    mapping = {
        "ticker": "ticker", "signal_type": "signal_type", "signalType": "signal_type",
        "side": "side", "structure": "structure",
        "long_occ": "long_occ", "longOcc": "long_occ",
        "short_occ": "short_occ", "shortOcc": "short_occ",
        "quantity": "quantity",
        "fill_debit": "fill_debit", "fillDebit": "fill_debit",
        "entry_date": "entry_date", "entryDate": "entry_date",
        "expiry": "expiry",
        "stop_underlying": "stop_underlying", "stopUnderlying": "stop_underlying",
        "profit_target_pct": "profit_target_pct", "profitTargetPct": "profit_target_pct",
        "time_exit": "time_exit", "timeExit": "time_exit",
        "cover_level": "cover_level", "coverLevel": "cover_level",
        "notes": "notes", "status": "status",
        "close_reason": "close_reason", "closeReason": "close_reason",
        "close_pnl": "close_pnl", "closePnl": "close_pnl",
    }
    patch = {}
    for k, v in body.items():
        if k not in mapping:
            continue
        dest = mapping[k]
        if dest == "quantity":
            patch[dest] = int(v)
        elif dest in ("fill_debit", "stop_underlying", "profit_target_pct", "cover_level", "close_pnl"):
            patch[dest] = float(v) if v not in (None, "") else None
        elif dest == "side":
            patch[dest] = str(v).strip().lower()
        elif dest == "structure":
            patch[dest] = str(v).strip().lower()
        elif dest == "signal_type":
            patch[dest] = str(v).strip()
        elif dest in ("ticker", "long_occ"):
            patch[dest] = str(v).strip().upper().replace(" ", "")
        elif dest == "short_occ":
            patch[dest] = str(v).strip().upper().replace(" ", "") or None
        else:
            patch[dest] = v
    try:
        from db import update_position
        row = update_position(pid, patch)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True, "position": row}



@app.post("/api/positions/{pid}/close")
async def positions_close(pid: int, request: Request):
    _, err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    patch = {
        "status": "closed",
        "close_reason": body.get("close_reason") or body.get("closeReason") or "manual",
        "close_pnl": body.get("close_pnl", body.get("closePnl")),
    }
    if patch["close_pnl"] is not None:
        try:
            patch["close_pnl"] = float(patch["close_pnl"])
        except Exception:
            patch["close_pnl"] = None
    try:
        from db import update_position
        row = update_position(pid, patch)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True, "position": row}


@app.delete("/api/positions/{pid}")
def positions_delete(pid: int, request: Request):
    _, err = _require_admin(request)
    if err:
        return err
    try:
        from db import delete_position
        ok = delete_position(pid)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    if not ok:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return {"ok": True}



if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
