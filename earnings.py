"""Fetch next earnings date from Yahoo; cache daily (file + optional Neon).

Only FUTURE earnings count: if Yahoo returns a past date, treat next earnings
as unknown (no badge). Badge only when today/entry <= earnings <= option expiry.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_FILE_CACHE = Path(__file__).parent / "earnings_cache.json"
_MAX_AGE_H = 24.0


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    try:
        s.get("https://fc.yahoo.com", timeout=10)
        cr = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10)
        crumb = cr.text.strip() if cr.ok else ""
        if crumb and "html" not in crumb.lower():
            s.headers["X-Yahoo-Crumb"] = crumb
            s.params = {"crumb": crumb}
    except Exception:
        pass
    return s


def _parse_ts(ts: Any) -> Optional[date]:
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        if isinstance(ts, str) and ts.isdigit():
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        if isinstance(ts, str) and len(ts) >= 10:
            return date.fromisoformat(ts[:10])
    except Exception:
        return None
    return None


def _future_only(d: Optional[date], as_of: Optional[date] = None) -> Optional[date]:
    """Past earnings are not 'next' — return None so we do not badge."""
    if d is None:
        return None
    ref = as_of or _today()
    if d < ref:
        return None
    return d


def _load_file() -> dict:
    try:
        if _FILE_CACHE.is_file():
            return json.loads(_FILE_CACHE.read_text())
    except Exception:
        pass
    return {}


def _save_file(cache: dict) -> None:
    try:
        _FILE_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass


def _file_put(symbol: str, d: Optional[date]) -> None:
    cache = _load_file()
    cache[symbol.upper()] = {
        "next_earnings": d.isoformat() if d else None,
        "ts": time.time(),
    }
    _save_file(cache)


def _pick_next_from_list(dates: list, as_of: date) -> Optional[date]:
    """From Yahoo earningsDate list (may include past), pick earliest >= as_of."""
    parsed: list[date] = []
    for raw in dates:
        if isinstance(raw, dict):
            d = _parse_ts(raw.get("raw"))
        else:
            d = _parse_ts(raw)
        if d is not None:
            parsed.append(d)
    future = sorted(d for d in parsed if d >= as_of)
    return future[0] if future else None


def fetch_next_earnings(symbol: str, sess: Optional[requests.Session] = None, as_of: Optional[date] = None) -> Optional[date]:
    s = sess or _session()
    sym = symbol.upper()
    ref = as_of or _today()
    # 1) quoteSummary calendarEvents — may return a range; pick first future
    try:
        r = s.get(
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}",
            params={"modules": "calendarEvents"},
            timeout=15,
        )
        if r.ok:
            j = r.json()
            res = (j.get("quoteSummary") or {}).get("result") or []
            if res:
                cal = (res[0].get("calendarEvents") or {}).get("earnings") or {}
                dates = cal.get("earningsDate") or []
                if dates:
                    d = _pick_next_from_list(dates, ref)
                    if d:
                        return d
                    # All past — unknown next
                    return None
    except Exception:
        pass
    # 2) v7 quote timestamps — only accept if >= as_of
    try:
        r = s.get(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={"symbols": sym},
            timeout=15,
        )
        if r.ok:
            results = ((r.json().get("quoteResponse") or {}).get("result")) or []
            if results:
                q = results[0]
                for key in ("earningsTimestampStart", "earningsTimestamp", "earningsTimestampEnd"):
                    d = _future_only(_parse_ts(q.get(key)), ref)
                    if d:
                        return d
    except Exception:
        pass
    return None


def next_earnings_for(
    symbol: str,
    sess: Optional[requests.Session] = None,
    as_of: Optional[date] = None,
) -> Optional[date]:
    sym = symbol.upper()
    ref = as_of or _today()
    # 1) file cache — still filter past (cache may be stale from before the fix)
    cache = _load_file()
    row = cache.get(sym)
    if row and (time.time() - float(row.get("ts") or 0)) <= _MAX_AGE_H * 3600:
        ne = row.get("next_earnings")
        if ne:
            try:
                return _future_only(date.fromisoformat(str(ne)[:10]), ref)
            except Exception:
                return None
        return None  # known miss / past cached as null

    # 2) Neon cache (soft)
    try:
        from db import get_earnings_cached, upsert_earnings

        cached = get_earnings_cached(sym)
        if cached is not None:
            ne = cached.get("next_earnings")
            d = None
            if isinstance(ne, str):
                try:
                    d = date.fromisoformat(ne[:10])
                except Exception:
                    d = None
            elif isinstance(ne, date):
                d = ne
            d = _future_only(d, ref)
            # If Neon had a past date, treat as miss and refetch (or store null)
            if d is None and ne:
                # Past date in DB — refetch in case Yahoo has a real next; else cache null
                d = fetch_next_earnings(sym, sess=sess, as_of=ref)
                _file_put(sym, d)
                try:
                    upsert_earnings(sym, d, raw={"source": "yahoo", "cleared_past": True})
                except Exception:
                    pass
                return d
            _file_put(sym, d)
            return d
    except Exception:
        pass

    d = fetch_next_earnings(sym, sess=sess, as_of=ref)
    _file_put(sym, d)
    try:
        from db import upsert_earnings

        upsert_earnings(sym, d, raw={"source": "yahoo"})
    except Exception:
        pass
    return d


def earnings_before_expiry(
    symbol: str,
    expiry: Optional[str],
    sess=None,
    as_of: Optional[date] = None,
    entry_date: Optional[str] = None,
) -> dict:
    """Badge only when next earnings is on/after as_of (today or entry) AND on/before expiry."""
    out = {
        "symbol": symbol.upper(),
        "nextEarnings": None,
        "beforeExpiry": False,
        "badge": None,
    }
    if not expiry:
        return out
    try:
        exp = date.fromisoformat(str(expiry)[:10])
    except Exception:
        return out

    ref = as_of or _today()
    if entry_date:
        try:
            ed = date.fromisoformat(str(entry_date)[:10])
            # Use the later of today and entry — don't flag earnings that already passed before entry
            if ed > ref:
                ref = ed
        except Exception:
            pass

    d = next_earnings_for(symbol, sess=sess, as_of=ref)
    if not d:
        return out
    # Defense in depth
    if d < ref:
        return out
    out["nextEarnings"] = d.isoformat()
    if ref <= d <= exp:
        out["beforeExpiry"] = True
        out["badge"] = f"EARNINGS {d.isoformat()} before expiry"
    return out
