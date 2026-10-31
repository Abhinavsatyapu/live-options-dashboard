"""Fetch next earnings date from Yahoo; cache daily (file + optional Neon)."""
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
    except Exception:
        return None
    return None


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


def _file_get(symbol: str) -> Optional[date]:
    cache = _load_file()
    row = cache.get(symbol.upper())
    if not row:
        return None
    if (time.time() - float(row.get("ts") or 0)) > _MAX_AGE_H * 3600:
        return None
    ne = row.get("next_earnings")
    if not ne:
        return None  # cached miss
    try:
        return date.fromisoformat(str(ne)[:10])
    except Exception:
        return None


def _file_put(symbol: str, d: Optional[date]) -> None:
    cache = _load_file()
    cache[symbol.upper()] = {
        "next_earnings": d.isoformat() if d else None,
        "ts": time.time(),
    }
    _save_file(cache)


def fetch_next_earnings(symbol: str, sess: Optional[requests.Session] = None) -> Optional[date]:
    s = sess or _session()
    sym = symbol.upper()
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
                    raw = dates[0]
                    if isinstance(raw, dict):
                        d = _parse_ts(raw.get("raw"))
                    else:
                        d = _parse_ts(raw)
                    if d:
                        return d
    except Exception:
        pass
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
                for key in ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd"):
                    d = _parse_ts(q.get(key))
                    if d:
                        return d
    except Exception:
        pass
    return None


def next_earnings_for(symbol: str, sess: Optional[requests.Session] = None) -> Optional[date]:
    sym = symbol.upper()
    # 1) file cache
    cache = _load_file()
    row = cache.get(sym)
    if row and (time.time() - float(row.get("ts") or 0)) <= _MAX_AGE_H * 3600:
        ne = row.get("next_earnings")
        if ne:
            try:
                return date.fromisoformat(str(ne)[:10])
            except Exception:
                return None
        return None  # known miss today

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
            _file_put(sym, d)
            return d
    except Exception:
        pass

    d = fetch_next_earnings(sym, sess=sess)
    _file_put(sym, d)
    try:
        from db import upsert_earnings

        upsert_earnings(sym, d, raw={"source": "yahoo"})
    except Exception:
        pass
    return d


def earnings_before_expiry(symbol: str, expiry: Optional[str], sess=None) -> dict:
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
    d = next_earnings_for(symbol, sess=sess)
    if not d:
        return out
    out["nextEarnings"] = d.isoformat()
    if d <= exp:
        out["beforeExpiry"] = True
        out["badge"] = f"EARNINGS {d.isoformat()} before expiry"
    return out
