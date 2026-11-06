"""Zone Monitor intraday lists.

The live service cannot sign in to the Zone Monitor dashboard, so these names
come from JSON the operator updates. Trading rules stay in this module:

  Intraday bullish (long)
    Skip FADE, Book A Neutral, and Book B Neutral.
    Entry = previous regular-session high. Stop = previous regular-session low.
    Triggered once last or today's high has printed that high.
    If price trades under the previous low before that break, the stop side
    has already traded.

  Intraday reversals (short)
    Entry = previous regular-session low. Stop = previous regular-session high.
    Triggered once last or today's low has printed that low.
    If price trades above the previous high before that break, the stop side
    has already traded.

Latest print per ticker wins. A newer skipped print removes the ticker.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BULL_PATH = ROOT / "intraday_bullish.json"
REV_PATH = ROOT / "intraday_reversals.json"
ET = ZoneInfo("America/New_York")

BULLISH_RULE = (
    "Intraday bullish: long if the previous regular-session high breaks. "
    "Stop is the previous regular-session low. "
    "FADE, Book A Neutral, and Book B Neutral are not shown."
)
REVERSAL_RULE = (
    "Intraday reversal: short if the previous regular-session low breaks. "
    "Stop is the previous regular-session high."
)

# Weak Zone Monitor types. Matching is on the family + type text, not an allow-list,
# so a new strong print (IGN-A, book A IGNITION, …) still shows.
_SKIP_RE = re.compile(r"\bfade\b|book\s+[ab]\s+neutral", re.IGNORECASE)
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_ZONE_RE = re.compile(r"\b(ET|EST|EDT)\b", re.IGNORECASE)


def load_intraday_file(path: Path) -> dict:
    """Read an operator list. A missing or empty file is a valid empty list."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {"signalsAsOf": None, "prints": []}
    except Exception:
        return {"signalsAsOf": None, "prints": [], "error": f"Could not read {path.name}"}
    if isinstance(raw, list):
        return {"signalsAsOf": None, "prints": raw}
    if not isinstance(raw, dict):
        return {"signalsAsOf": None, "prints": [], "error": f"{path.name} is not a list of prints"}
    prints = raw.get("prints", raw.get("signals", raw.get("rows", [])))
    if not isinstance(prints, list):
        prints = []
    out = dict(raw)
    out["prints"] = prints
    return out


def is_skipped(row: dict) -> bool:
    family = str(row.get("family") or row.get("signalFamily") or "")
    typ = str(row.get("type") or row.get("signalType") or row.get("setup") or "")
    return bool(_SKIP_RE.search(f"{family} {typ}"))


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "—", "-", "n/a", "N/A", "null"):
        return None
    try:
        return float(text.replace("+", ""))
    except ValueError:
        return None


def _atm(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _signal_time(row: dict) -> str:
    raw = str(row.get("time") or row.get("signalTime") or "").strip()
    if not raw:
        return ""
    if _ZONE_RE.search(raw):
        return raw
    match = _TIME_RE.search(raw)
    if not match:
        return raw
    return f"{int(match.group(1)):02d}:{match.group(2)} ET"


def _when_key(row: dict, index: int) -> tuple:
    """Latest calendar date, then clock time, then file order."""
    day = str(row.get("date") or row.get("session") or "")
    match = _TIME_RE.search(str(row.get("time") or row.get("signalTime") or ""))
    minutes = int(match.group(1)) * 60 + int(match.group(2)) if match else -1
    return (day, minutes, index)


def select_latest(prints: list, side: str) -> list[dict]:
    """One card per ticker: the latest print, dropped when that print is weak."""
    best: dict[str, tuple] = {}
    for index, row in enumerate(prints or []):
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        key = _when_key(row, index)
        prev = best.get(ticker)
        if prev is None or key >= prev[0]:
            best[ticker] = (key, row, ticker)
    chosen: list[tuple] = []
    for key, row, ticker in best.values():
        if is_skipped(row):
            continue
        family = str(row.get("family") or row.get("signalFamily") or "").strip()
        typ = str(row.get("type") or row.get("signalType") or row.get("setup") or "").strip()
        chosen.append(
            (
                key,
                {
                    "symbol": ticker,
                    "ticker": ticker,
                    "family": family,
                    "type": typ,
                    "signalTime": _signal_time(row),
                    "date": str(row.get("date") or row.get("session") or "") or None,
                    "deltaGex": _num(row.get("deltaGex", row.get("delta_gex", row.get("dgex")))),
                    "atm": _atm(row.get("atm", row.get("atmNotional"))),
                    "side": "short" if side == "short" else "long",
                },
            )
        )
    chosen.sort(key=lambda item: item[0], reverse=True)
    return [row for _key, row in chosen]


# Alert once price is within a dollar of the entry and has not crossed it yet.
APPROACH_DOLLARS = 1.0


def _cents(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value) + 1e-9, 2)


def _approach_gap(prices: tuple, entry: float, *, below: bool) -> Optional[float]:
    """Smallest dollar gap still on the near side of entry, inside the $1 band."""
    entry_px = _cents(entry)
    if entry_px is None:
        return None
    gaps = []
    for raw in prices:
        px = _cents(raw)
        if px is None:
            continue
        if below and entry_px - APPROACH_DOLLARS <= px < entry_px:
            gaps.append(round(entry_px - px, 2))
        elif (not below) and entry_px < px <= entry_px + APPROACH_DOLLARS:
            gaps.append(round(px - entry_px, 2))
    positive = [gap for gap in gaps if gap > 0]
    return min(positive) if positive else None


def _at_or_above(last: Optional[float], extreme: Optional[float], level: float) -> bool:
    return (last is not None and last >= level) or (extreme is not None and extreme >= level)


def _at_or_below(last: Optional[float], extreme: Optional[float], level: float) -> bool:
    return (last is not None and last <= level) or (extreme is not None and extreme <= level)


def _strictly_above(last: Optional[float], extreme: Optional[float], level: float) -> bool:
    return (last is not None and last > level) or (extreme is not None and extreme > level)


def _strictly_below(last: Optional[float], extreme: Optional[float], level: float) -> bool:
    return (last is not None and last < level) or (extreme is not None and extreme < level)


def _quote_price(quote: dict) -> Optional[float]:
    for key in ("regularMarketPrice", "postMarketPrice", "preMarketPrice", "bid", "ask"):
        price = _num((quote or {}).get(key))
        if price is not None and price > 0:
            return price
    return None


def apply_levels(
    card: dict,
    *,
    last: Optional[float],
    change_pct: Optional[float],
    today_high: Optional[float],
    today_low: Optional[float],
    prev_high: Optional[float],
    prev_low: Optional[float],
    prev_date: Optional[str],
) -> dict:
    """Attach prior-day levels. Entry/stop follow the side. Nothing is invented."""
    out = dict(card)
    side = out.get("side") or "long"
    out["price"] = last
    out["changePct"] = change_pct
    out["todayHigh"] = today_high
    out["todayLow"] = today_low
    out["prevDayHigh"] = prev_high
    out["prevDayLow"] = prev_low
    out["prevDayDate"] = prev_date
    out["tradedThroughStop"] = False
    out["approaching"] = False
    out["distanceToEntry"] = None
    if prev_high is None or prev_low is None:
        out["entry"] = None
        out["stop"] = None
        out["triggered"] = False
        out["stopSideTraded"] = False
        out["levelsPending"] = True
        out["status"] = "WAITING"
        out["statusNote"] = "Prior-day high/low still loading"
        return out

    if side == "short":
        entry, stop = prev_low, prev_high
        broke_entry = _at_or_below(last, today_low, entry)
        through_stop = _strictly_above(last, today_high, stop)
        pending_note = "Waiting for a break of the previous-day low"
        stop_note = "Stop side already traded — price above the previous-day high before entry"
    else:
        entry, stop = prev_high, prev_low
        broke_entry = _at_or_above(last, today_high, entry)
        through_stop = _strictly_below(last, today_low, stop)
        pending_note = "Waiting for a break of the previous-day high"
        stop_note = "Stop side already traded — price under the previous-day low before entry"
    stop_before_entry = (not broke_entry) and through_stop

    out["entry"] = entry
    out["stop"] = stop
    out["levelsPending"] = False
    if broke_entry:
        out["triggered"] = True
        out["stopSideTraded"] = False
        out["approaching"] = False
        out["tradedThroughStop"] = bool(through_stop)
        out["status"] = "TRIGGERED"
        out["statusNote"] = "Entry triggered"
        if through_stop:
            out["statusNote"] = "Entry triggered · also traded through the stop"
        return out
    if stop_before_entry:
        out["triggered"] = False
        out["stopSideTraded"] = True
        out["approaching"] = False
        out["status"] = "STOP_SIDE"
        out["statusNote"] = stop_note
        return out
    gap = (
        _approach_gap((last, today_low), entry, below=False)
        if side == "short"
        else _approach_gap((last, today_high), entry, below=True)
    )
    out["triggered"] = False
    out["stopSideTraded"] = False
    if gap is not None:
        out["approaching"] = True
        out["distanceToEntry"] = gap
        out["status"] = "APPROACHING"
        if side == "short":
            out["statusNote"] = f"Within $1 of yesterday's low — take short entry · ${gap:.2f} left"
        else:
            out["statusNote"] = f"Within $1 of yesterday's high — take long entry · ${gap:.2f} left"
        return out
    out["status"] = "WAITING"
    out["statusNote"] = pending_note
    return out


def build_cards(prints: list, side: str, quotes: dict, levels: dict) -> list[dict]:
    cards = []
    for row in select_latest(prints, side):
        sym = row["symbol"]
        quote = (quotes or {}).get(sym) or {}
        level = (levels or {}).get(sym) or {}
        last = _quote_price(quote)
        if last is None:
            last = _num(level.get("price"))
        change = _num(quote.get("regularMarketChangePercent"))
        if change is None:
            change = _num(level.get("changePct"))
        today_high = _num(quote.get("regularMarketDayHigh"))
        if today_high is None:
            today_high = _num(level.get("todayHigh"))
        today_low = _num(quote.get("regularMarketDayLow"))
        if today_low is None:
            today_low = _num(level.get("todayLow"))
        cards.append(
            apply_levels(
                row,
                last=last,
                change_pct=change,
                today_high=today_high,
                today_low=today_low,
                prev_high=_num(level.get("prevDayHigh")),
                prev_low=_num(level.get("prevDayLow")),
                prev_date=(str(level.get("prevDayDate")) if level.get("prevDayDate") else None),
            )
        )
    return cards


def snapshot_section(quotes: dict, levels: dict) -> dict:
    bull_doc = load_intraday_file(BULL_PATH)
    rev_doc = load_intraday_file(REV_PATH)
    errors = [err for err in (bull_doc.get("error"), rev_doc.get("error")) if err]
    return {
        "intradayBullish": build_cards(bull_doc.get("prints") or [], "long", quotes, levels),
        "intradayReversals": build_cards(rev_doc.get("prints") or [], "short", quotes, levels),
        "intradayBullishAsOf": bull_doc.get("signalsAsOf"),
        "intradayReversalsAsOf": rev_doc.get("signalsAsOf"),
        "intradayBullishRule": BULLISH_RULE,
        "intradayReversalsRule": REVERSAL_RULE,
        "errors": errors,
    }


def listed_symbols() -> list[str]:
    syms: list[str] = []
    for path, side in ((BULL_PATH, "long"), (REV_PATH, "short")):
        doc = load_intraday_file(path)
        syms.extend(row["symbol"] for row in select_latest(doc.get("prints") or [], side))
    return sorted(set(syms))


def parse_daily_chart(payload: dict, today: date) -> Optional[dict]:
    """Previous completed regular-session high/low from a Yahoo v8 daily chart.

    Bars dated `today` are the session in progress and are not the previous day.
    Today's high/low and last price are taken from chart meta when Yahoo sends them.
    """
    try:
        result = ((payload or {}).get("chart") or {}).get("result") or []
        block = result[0] if result else None
    except (TypeError, IndexError, AttributeError):
        block = None
    if not isinstance(block, dict):
        return None
    meta = block.get("meta") or {}
    tz_name = meta.get("exchangeTimezoneName") or "America/New_York"
    try:
        tz = ZoneInfo(str(tz_name))
    except Exception:
        tz = ET
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0] or {}
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    stamps = block.get("timestamp") or []
    bars: list[tuple[date, float, float]] = []
    for i, stamp in enumerate(stamps):
        if i >= len(highs) or i >= len(lows):
            break
        high = _num(highs[i])
        low = _num(lows[i])
        if high is None or low is None or stamp is None:
            continue
        try:
            stamp_i = int(stamp)
            if stamp_i > 10**12:
                stamp_i //= 1000
            session = datetime.fromtimestamp(stamp_i, tz).date()
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        bars.append((session, high, low))
    prior = [bar for bar in bars if bar[0] < today]
    today_bar = next((bar for bar in reversed(bars) if bar[0] == today), None)
    prev_high = prev_low = None
    prev_date = None
    if prior:
        session, prev_high, prev_low = prior[-1]
        prev_date = session.isoformat()
    price = _num(meta.get("regularMarketPrice"))
    prev_close = _num(meta.get("chartPreviousClose") or meta.get("previousClose"))
    change = None
    if price is not None and prev_close is not None and prev_close > 0:
        change = (price / prev_close - 1.0) * 100.0
    today_high = _num(meta.get("regularMarketDayHigh"))
    today_low = _num(meta.get("regularMarketDayLow"))
    if today_bar is not None:
        if today_high is None:
            today_high = today_bar[1]
        if today_low is None:
            today_low = today_bar[2]
    out: dict[str, Any] = {}
    if prev_high is not None and prev_low is not None:
        out["prevDayHigh"] = prev_high
        out["prevDayLow"] = prev_low
        out["prevDayDate"] = prev_date
    if price is not None:
        out["price"] = price
    if change is not None:
        out["changePct"] = change
    if today_high is not None:
        out["todayHigh"] = today_high
    if today_low is not None:
        out["todayLow"] = today_low
    return out or None
