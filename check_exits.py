#!/usr/bin/env python3
"""Exit-alert monitor for open options positions.

Usage:
  DATABASE_URL=... python /workspace/check_exits.py
  # or: reads /workspace/.secrets/options_db_url
  # offline/fixture: python check_exits.py --positions-file /tmp/open_positions.json

Stdout: JSON object
  {
    "ok": true,
    "checked_at": "...",
    "open": N,
    "alerts": [ { id, ticker, kind, message, ... }, ... ],
    "new_alerts": [ ... ]   # after dedupe vs /workspace/exit_alert_state.json
  }
Exit codes: 0 always when ran; 2 on hard failure.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# Allow importing dashboard helpers when run from /workspace
DASH = Path("/workspace/live-options-dashboard")
sys.path.insert(0, str(DASH))

IST = ZoneInfo("Asia/Calcutta")
ET = ZoneInfo("America/New_York")
STATE_PATH = Path("/workspace/exit_alert_state.json")
ZONE_PATHS = [
    Path("/workspace/zone_full.json"),
    Path("/workspace/signals_state.json"),
]

# Prefer secret file if DATABASE_URL unset
if not os.environ.get("DATABASE_URL"):
    sec = Path("/workspace/.secrets/options_db_url")
    if sec.is_file():
        os.environ["DATABASE_URL"] = sec.read_text().strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"seen": {}}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, indent=2))


def cover_levels_from_zone() -> dict[str, float]:
    """Map ticker -> cover_above from Zone Monitor captures if present."""
    out: dict[str, float] = {}
    for p in ZONE_PATHS:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        # Heuristics across known shapes
        candidates = []
        if isinstance(data, dict):
            for key in ("in_a_short", "open_shorts", "reversals", "positions", "signals"):
                v = data.get(key)
                if isinstance(v, list):
                    candidates.extend(v)
                elif isinstance(v, dict):
                    candidates.extend(v.values() if all(isinstance(x, dict) for x in v.values()) else [v])
            if "tickers" in data and isinstance(data["tickers"], dict):
                for sym, info in data["tickers"].items():
                    if isinstance(info, dict):
                        info = {**info, "symbol": sym}
                        candidates.append(info)
        for item in candidates:
            if not isinstance(item, dict):
                continue
            sym = (item.get("symbol") or item.get("ticker") or item.get("sym") or "").upper()
            cover = item.get("cover_above") or item.get("cover") or item.get("cover_level")
            if sym and cover is not None:
                try:
                    out[sym] = float(cover)
                except Exception:
                    pass
    return out


def yahoo_quote(session, symbol: str) -> Optional[float]:
    try:
        r = session.get(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={"symbols": symbol},
            timeout=15,
        )
        if not r.ok:
            return None
        res = ((r.json().get("quoteResponse") or {}).get("result")) or []
        if not res:
            return None
        q = res[0]
        for k in ("regularMarketPrice", "postMarketPrice", "preMarketPrice"):
            if q.get(k) is not None:
                return float(q[k])
    except Exception:
        return None
    return None


def option_mid_from_occ(session, occ: str) -> Optional[float]:
    """Best-effort: use Yahoo options via underlying + expiry/strike parse from OCC."""
    # OCC: ROOT + YYMMDD + C/P + strike*1000 (8 digits)
    import re
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", occ.replace(" ", ""))
    if not m:
        return None
    root, yymmdd, cp, strike_raw = m.groups()
    strike = int(strike_raw) / 1000.0
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    year = 2000 + yy
    try:
        exp = date(year, mm, dd)
    except Exception:
        return None
    # unix expiry noon ET approx
    exp_dt = datetime(year, mm, dd, 16, 0, tzinfo=ET)
    exp_unix = int(exp_dt.timestamp())
    try:
        r = session.get(
            f"https://query2.finance.yahoo.com/v7/finance/options/{root}",
            params={"date": exp_unix},
            timeout=20,
        )
        if not r.ok:
            return None
        result = (r.json().get("optionChain") or {}).get("result") or []
        if not result:
            return None
        opts = result[0].get("options") or []
        if not opts:
            return None
        legs = opts[0].get("calls" if cp == "C" else "puts") or []
        best = None
        for leg in legs:
            if abs(float(leg.get("strike", 0)) - strike) < 1e-6:
                best = leg
                break
        if not best:
            return None
        bid, ask = best.get("bid"), best.get("ask")
        last = best.get("lastPrice")
        if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
            return (float(bid) + float(ask)) / 2.0
        if last is not None:
            return float(last)
    except Exception:
        return None
    return None


def spread_mark(session, long_occ: str, short_occ: Optional[str]) -> Optional[float]:
    lm = option_mid_from_occ(session, long_occ)
    if lm is None:
        return None
    if not short_occ or short_occ == long_occ:
        return lm
    sm = option_mid_from_occ(session, short_occ)
    if sm is None:
        return lm
    return lm - sm


def friday_of_week(d: date) -> date:
    # Monday=0 … Friday=4
    return d.fromordinal(d.toordinal() + (4 - d.weekday()))


def evaluate_position(pos: dict, session, covers: dict[str, float], today_et: date) -> list[dict]:
    alerts = []
    pid = pos["id"]
    ticker = pos["ticker"].upper()
    fill = float(pos["fill_debit"])
    qty = int(pos["quantity"])
    long_occ = pos["long_occ"]
    short_occ = pos.get("short_occ")
    is_spread = bool(short_occ) and short_occ != long_occ
    mark = spread_mark(session, long_occ, short_occ)
    und = yahoo_quote(session, ticker)
    stop = pos.get("stop_underlying")
    pt_pct = float(pos.get("profit_target_pct") or (80 if is_spread else 100))
    # For spreads max theoretical ≈ width - debit; approximate target as fill * (1 + pt/100) for singles,
    # and for spreads use fill + pt% of (estimated max). Without width, use 70-80% of debit recovered as proxy: mark >= fill * (pt/100) when pt<100 means take profit at that % of debit? 
    # Rulebook: +100% for singles (mark >= 2*fill), ~70-80% of max for spreads.
    # Practical: singles target_mark = fill * (1 + pt/100); spreads target_mark = fill * (pt/100) if pt<=100 treating as % of max≈debit*something — use mark >= fill * (1 + pt/100) for singles; for spreads mark >= fill + (max_est * pt/100) with max_est = fill * 1.5 fallback OR simply mark/fill - 1 >= pt/100 * 0.75
    if is_spread:
        # profit target hit when unrealized return >= profit_target_pct (default 80 means +80% on debit)
        target_mult = 1.0 + (pt_pct / 100.0) * 0.8  # ~80% of a "100% on debit" style
        # clearer: treat profit_target_pct as percent of debit gain for spreads too when set to 70-80
        target_mark = fill * (1.0 + pt_pct / 100.0)
    else:
        target_mark = fill * (1.0 + pt_pct / 100.0)

    side = pos.get("side") or "call"
    # STOP: underlying breach
    if und is not None and stop is not None:
        stop_f = float(stop)
        hit = (side == "put" and und > stop_f) or (side == "call" and und < stop_f)
        if hit:
            alerts.append({
                "id": pid, "ticker": ticker, "kind": "STOP_HIT",
                "message": f"{ticker} underlying {und:.2f} breached stop {stop_f:.2f}",
                "underlying": und, "stop": stop_f, "mark": mark,
            })

    # TARGET
    if mark is not None and fill > 0 and mark >= target_mark:
        ret = (mark / fill - 1.0) * 100.0
        alerts.append({
            "id": pid, "ticker": ticker, "kind": "TARGET_HIT",
            "message": f"{ticker} option mark {mark:.2f} ≥ target {target_mark:.2f} ({ret:.0f}% on debit)",
            "mark": mark, "fill": fill, "target": target_mark,
        })

    # BIG adverse -50%
    if mark is not None and fill > 0 and mark <= fill * 0.5:
        alerts.append({
            "id": pid, "ticker": ticker, "kind": "ADVERSE_50",
            "message": f"{ticker} option mark {mark:.2f} ≤ 50% of fill {fill:.2f}",
            "mark": mark, "fill": fill,
        })

    # TIME EXIT: Friday for 1w / 2 days before expiry
    expiry = pos.get("expiry")
    time_exit = (pos.get("time_exit") or "friday").lower()
    exp_d = None
    if expiry:
        try:
            exp_d = date.fromisoformat(str(expiry)[:10])
        except Exception:
            exp_d = None
    if time_exit == "friday" and today_et.weekday() == 4:
        alerts.append({
            "id": pid, "ticker": ticker, "kind": "EXIT_FRIDAY",
            "message": f"{ticker} Friday time-exit (1-week hold)",
            "mark": mark,
        })
    if exp_d and (exp_d - today_et).days <= 2:
        alerts.append({
            "id": pid, "ticker": ticker, "kind": "EXIT_EXPIRY",
            "message": f"{ticker} within 2 days of expiry {exp_d.isoformat()}",
            "expiry": exp_d.isoformat(), "mark": mark,
        })

    # COVER level for reversal shorts
    cover = pos.get("cover_level") or covers.get(ticker)
    if cover is not None and und is not None and side == "put":
        if und >= float(cover):
            alerts.append({
                "id": pid, "ticker": ticker, "kind": "COVER_HIT",
                "message": f"{ticker} underlying {und:.2f} ≥ cover {float(cover):.2f}",
                "underlying": und, "cover": float(cover), "mark": mark,
            })

    for a in alerts:
        a["signal_type"] = pos.get("signal_type")
        a["quantity"] = qty
        a["checked_at"] = now_iso()
    return alerts


def main() -> int:
    positions_file = None
    args = sys.argv[1:]
    if "--positions-file" in args:
        i = args.index("--positions-file")
        if i + 1 >= len(args):
            print(json.dumps({"ok": False, "error": "--positions-file needs a path"}))
            return 2
        positions_file = args[i + 1]

    if positions_file:
        try:
            opens = json.loads(Path(positions_file).read_text())
            if isinstance(opens, dict):
                opens = opens.get("positions") or opens.get("open") or []
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"positions-file: {e}"}))
            return 2
    else:
        try:
            from db import list_positions, ensure_schema, database_url
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"import: {e}"}))
            return 2
        if not database_url():
            print(json.dumps({"ok": False, "error": "DATABASE_URL missing"}))
            return 2
        try:
            ensure_schema()
            opens = list_positions("open")
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2

    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    try:
        sess.get("https://fc.yahoo.com", timeout=10)
    except Exception:
        pass

    covers = cover_levels_from_zone()
    today_et = datetime.now(ET).date()
    all_alerts: list[dict] = []
    for pos in opens:
        try:
            all_alerts.extend(evaluate_position(pos, sess, covers, today_et))
        except Exception as e:
            all_alerts.append({
                "id": pos.get("id"), "ticker": pos.get("ticker"), "kind": "ERROR",
                "message": str(e),
            })

    st = load_state()
    seen = st.setdefault("seen", {})
    new_alerts = []
    for a in all_alerts:
        key = f"{a.get('id')}:{a.get('kind')}:{a.get('message','')[:80]}"
        if key not in seen:
            new_alerts.append(a)
            seen[key] = {"first_seen": now_iso(), "count": 1}
        else:
            seen[key]["count"] = int(seen[key].get("count", 1)) + 1
            seen[key]["last_seen"] = now_iso()
    # prune closed keys older — keep last 500
    if len(seen) > 500:
        items = sorted(seen.items(), key=lambda kv: kv[1].get("last_seen") or kv[1].get("first_seen") or "")
        st["seen"] = dict(items[-500:])
    save_state(st)

    out = {
        "ok": True,
        "checked_at": now_iso(),
        "checked_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "open": len(opens),
        "alerts": all_alerts,
        "new_alerts": new_alerts,
        "alert_kinds": sorted({a["kind"] for a in all_alerts}),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
