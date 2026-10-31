"""Live marks + alert badges for open positions."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ACCOUNT = 100_000.0
WEEKLY_TARGET = 3500.0


def parse_occ(occ: str) -> Optional[dict]:
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", (occ or "").replace(" ", "").upper())
    if not m:
        return None
    root, yymmdd, cp, strike_raw = m.groups()
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    return {
        "root": root,
        "expiry": date(2000 + yy, mm, dd),
        "cp": cp,
        "strike": int(strike_raw) / 1000.0,
        "expiry_str": f"{2000+yy:04d}-{mm:02d}-{dd:02d}",
    }


def _mid_from_legs(legs: list, strike: float) -> Optional[float]:
    best = None
    for leg in legs:
        try:
            if abs(float(leg.get("strike", 0)) - strike) < 1e-6:
                best = leg
                break
        except Exception:
            continue
    if not best:
        # also match by contractSymbol when present
        return None
    bid, ask, last = best.get("bid"), best.get("ask"), best.get("lastPrice")
    try:
        if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
            return (float(bid) + float(ask)) / 2.0
        if last is not None:
            return float(last)
    except Exception:
        return None
    return None


def option_mid_yahoo(yahoo, occ: str) -> Optional[float]:
    meta = parse_occ(occ)
    if not meta:
        return None
    exp = meta["expiry"]
    strike = meta["strike"]
    # Yahoo expirationDates are often midnight UTC; OCC calendar day can be ±1 vs listed.
    candidates = []
    try:
        bare = yahoo._get(  # type: ignore[attr-defined]
            f"https://query2.finance.yahoo.com/v7/finance/options/{meta['root']}"
        )
        exps = ((bare.get("optionChain") or {}).get("result") or [{}])[0].get("expirationDates") or []
        for u in exps:
            d = datetime.fromtimestamp(int(u), tz=ET).date()
            if abs((d - exp).days) <= 1:
                candidates.append(int(u))
    except Exception:
        candidates = []
    if not candidates:
        candidates = [int(datetime(exp.year, exp.month, exp.day, 16, 0, tzinfo=ET).timestamp())]
    for exp_unix in candidates:
        try:
            chain = yahoo.options_chain(meta["root"], exp_unix)
        except Exception:
            continue
        try:
            result = (chain.get("optionChain") or {}).get("result") or []
            if not result:
                continue
            opts = result[0].get("options") or []
            if not opts:
                continue
            legs = opts[0].get("calls" if meta["cp"] == "C" else "puts") or []
        except Exception:
            continue
        # Prefer exact OCC contractSymbol match
        for leg in legs:
            if str(leg.get("contractSymbol") or "").upper() == occ.upper():
                bid, ask, last = leg.get("bid"), leg.get("ask"), leg.get("lastPrice")
                try:
                    if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                        return (float(bid) + float(ask)) / 2.0
                    if last is not None:
                        return float(last)
                except Exception:
                    pass
        mid = _mid_from_legs(legs, strike)
        if mid is not None:
            return mid
    return None


def load_exit_state() -> dict:
    for cand in (
        Path("/workspace/exit_alert_state.json"),
        Path(__file__).parent / "exit_alert_state.json",
    ):
        try:
            if cand.is_file():
                return json.loads(cand.read_text())
        except Exception:
            pass
    return {"seen": {}, "latest": []}


def active_kinds_for(pid: int, state: dict) -> list[str]:
    kinds: list[str] = []
    for key in (state.get("seen") or {}):
        if key.startswith(f"{pid}:"):
            parts = key.split(":", 2)
            if len(parts) >= 2 and parts[1] not in kinds:
                kinds.append(parts[1])
    for a in state.get("latest") or []:
        if a.get("id") == pid and a.get("kind") and a["kind"] not in kinds:
            kinds.append(a["kind"])
    return kinds


def kinds_for_ticker(ticker: str, state: dict) -> list[str]:
    t = ticker.upper()
    kinds: list[str] = []
    for a in state.get("latest") or []:
        if str(a.get("ticker", "")).upper() == t and a.get("kind") and a["kind"] not in kinds:
            kinds.append(a["kind"])
    return kinds


def default_rules(signal_type: str, side: str, structure: str) -> dict:
    st = (signal_type or "").upper()
    is_spread = (structure or "").lower() in ("spread", "1em", "0.5em", "debit")
    if "REV" in st or "ABSORB" in st or side == "put":
        return {
            "profit_target_pct": 100.0 if not is_spread else 80.0,
            "time_exit": "friday",
            "stop_hint": "D+1 high (reversal short)",
            "side": "put",
        }
    return {
        "profit_target_pct": 100.0 if not is_spread else 70.0,
        "time_exit": "friday",
        "stop_hint": "signal-week low (bullish)",
        "side": "call",
    }


def enrich_positions(yahoo, quotes: dict, positions: list[dict], closed_week: list[dict] | None = None) -> dict:
    state = load_exit_state()
    enriched = []
    total_cost = 0.0
    total_pnl = 0.0
    today = datetime.now(ET).date()
    closed_week = closed_week or []
    realised = 0.0
    for c in closed_week:
        try:
            realised += float(c.get("close_pnl") or 0)
        except Exception:
            pass

    for pos in positions:
        fill = float(pos["fill_debit"])
        qty = int(pos["quantity"])
        cost = fill * 100 * qty
        long_occ = pos["long_occ"]
        short_occ = pos.get("short_occ")
        lm = option_mid_yahoo(yahoo, long_occ)
        sm = None
        if short_occ and short_occ != long_occ:
            sm = option_mid_yahoo(yahoo, short_occ)
        if lm is not None and sm is not None:
            mark = lm - sm
        else:
            mark = lm
        pnl_unit = None if mark is None else (mark - fill) * 100
        pnl_total = None if pnl_unit is None else pnl_unit * qty
        ret_pct = None if mark is None or fill <= 0 else (mark / fill - 1.0) * 100.0
        und = None
        q = quotes.get(pos["ticker"].upper()) or quotes.get(pos["ticker"])
        if q:
            for k in ("regularMarketPrice", "postMarketPrice", "preMarketPrice"):
                if q.get(k) is not None:
                    try:
                        und = float(q[k])
                        break
                    except Exception:
                        pass
        kinds = active_kinds_for(int(pos["id"]), state)
        stop = pos.get("stop_underlying")
        side = pos.get("side") or "call"
        pt = float(pos.get("profit_target_pct") or 100)
        target_mark = fill * (1.0 + pt / 100.0)
        if und is not None and stop is not None:
            try:
                sf = float(stop)
                if (side == "put" and und > sf) or (side == "call" and und < sf):
                    if "STOP_HIT" not in kinds:
                        kinds.append("STOP_HIT")
            except Exception:
                pass
        if mark is not None and mark >= target_mark and "TARGET_HIT" not in kinds:
            kinds.append("TARGET_HIT")
        if today.weekday() == 4 and "EXIT_FRIDAY" not in kinds:
            kinds.append("EXIT_FRIDAY")
        exp = pos.get("expiry")
        if exp:
            try:
                ed = date.fromisoformat(str(exp)[:10])
                if (ed - today).days <= 2 and "EXIT_EXPIRY" not in kinds:
                    kinds.append("EXIT_EXPIRY")
            except Exception:
                pass
        if mark is not None and fill > 0 and mark <= fill * 0.5 and "ADVERSE_50" not in kinds:
            kinds.append("ADVERSE_50")

        row = {
            **pos,
            "mark": mark,
            "underlying": und,
            "cost": cost,
            "pnlTotal": pnl_total,
            "pnlPct": ret_pct,
            "alertKinds": kinds,
            "targetMark": target_mark,
        }
        enriched.append(row)
        if pos.get("status") == "open":
            total_cost += cost
            if pnl_total is not None:
                total_pnl += pnl_total

    week_pnl = total_pnl + realised
    summary = {
        "account": ACCOUNT,
        "riskDeployed": round(total_cost, 2),
        "riskPct": round(100.0 * total_cost / ACCOUNT, 2),
        "unrealizedPnl": round(total_pnl, 2),
        "realisedWeek": round(realised, 2),
        "weekPnl": round(week_pnl, 2),
        "weeklyTarget": WEEKLY_TARGET,
        "weeklyTargetLow": 3000.0,
        "weeklyTargetHigh": 4000.0,
        "targetProgressPct": max(0.0, min(150.0, 100.0 * week_pnl / WEEKLY_TARGET)),
        "openCount": sum(1 for p in enriched if p.get("status") == "open"),
    }
    return {"positions": enriched, "summary": summary}
