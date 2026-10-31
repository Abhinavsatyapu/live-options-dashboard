#!/usr/bin/env python3
"""
Early high-conviction scorer for Zone Monitor captures.

Standing rule:
  - Bullish ONLY from Weekly (BULLISH / ★ / squeeze)
  - Bearish ONLY from Reversals
  - Never Weekly BEARISH

Sure-shot tiers (see /workspace/backtest/early_rules.json for backtest notes):
  Weekly Tier S: Friday-confirmed BULLISH ★ or SQ+VC3 ★ → trade next week
  Weekly Tier A (forming): Thu ★-path alert — NOT full size until Friday confirms wall_up≥2
  Reversal Tier S: D+1 ABSORBED CALLS after low-first + lean gates → enter D+1 close / D+2 break
  Reversal Tier A (forming): watching with fat event lean — wait for D+1 ABSORBED

Usage:
  python /workspace/early_signals.py [/path/to/zone_full.json]
  → prints JSON + writes /workspace/backtest/early_current.json
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path("/workspace")
DEFAULT_ZONE = ROOT / "zone_full.json"
OUT = ROOT / "backtest" / "early_current.json"
RULES_OUT = ROOT / "backtest" / "early_rules.json"


def parse_lean(s: Any) -> float:
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", str(s or "").replace("×", ""))
    return float(m.group(1)) if m else 0.0


def parse_pct(s: Any) -> Optional[float]:
    t = str(s or "").strip()
    if t in ("", "—", "-", "open", "n/a", "N/A"):
        return None
    try:
        return float(t.replace("%", "").replace("+", "").replace(",", ""))
    except ValueError:
        return None


def parse_call_put(s: str) -> tuple[int, int]:
    m = re.search(r"(\d+)\s*/\s*(\d+)", str(s or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def parse_wall(s: str) -> tuple[int, int]:
    m = re.search(r"(\d+)\s*/\s*(\d+)", str(s or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def parse_xm(row: dict) -> list[float]:
    lst = row.get("daily_xm_list")
    if lst:
        out = []
        for x in lst:
            try:
                out.append(float(str(x).replace("+", "")))
            except ValueError:
                out.append(0.0)
        return out
    out = []
    for p in str(row.get("daily_xm") or "").split():
        try:
            out.append(float(p.replace("+", "")))
        except ValueError:
            out.append(0.0)
    return out


def regime_flags(regime: str) -> dict:
    r = str(regime or "")
    return {
        "below_trend": "20<50" in r,
        "long_up": "50>200" in r,
        "long_down": "50<200" in r,
    }


def _f(x) -> Optional[float]:
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


RULES_DOC = {
    "standing_rule": "Bullish only from Weekly; bearish only from Reversals; never Weekly BEARISH.",
    "weekly_tier_S": {
        "when": "Friday close (or after Friday wall_up known)",
        "rule": "Label is BULLISH ★ OR squeeze cell is SQ+VC3 ★ (prefer counter-trend 20<50).",
        "entry": "Friday close or Monday open of the following week",
        "structure": "BULLISH ★ → ATM call ~1w; SQ+VC3 ★ → ATM/+1EM call debit ~1w; IV>100% → debit spread",
        "evidence": {
            "measured_BULLISH_star": "lean+wall measured n6 100% +6.1% (06/22–10/17, 13 tickers)",
            "measured_SQ_VC3_star": "counter+sq+VC3 n26 73% +2.85%; live-forward ★ 4/4 +3.60%",
            "live_forward_BULLISH_star_sep14_oct19": "n7 next_up 43% in this short window — size down; prefer SQ+VC3 ★ when available",
        },
        "days_early_vs_crowd": "0–1 session (act at print / Mon open before mid-week chase)",
    },
    "weekly_tier_A_forming": {
        "when": "Thursday close (Mon–Thu partial lean)",
        "rule": "20<50 AND 50>200 AND cum lean Mon–Thu ≥ 2.0× AND call days ≥ 2",
        "action": "WATCH / stage only — do NOT full-size until Friday confirms wall_up≥2 and official BULLISH/★ or SQ+VC3 ★",
        "evidence": {
            "loo_next_up": "43.9% (18/41) — BELOW 75% bar; not a sure-shot entry alone",
            "note": "Wall-up (≥2) is the missing midweek ingredient; unobservable until Friday 10:00 ET prints",
        },
        "days_early": "1 trading day of preparation before Friday confirmation",
    },
    "reversal_tier_S": {
        "when": "D+1 complete",
        "rule": "Event lean ≥1.5×, D+1 breaks EVENT LOW before high with lean ≥1.7×, D+1 flow = ABSORBED CALLS",
        "entry": "D+1 close (1 session early) or official D+2 first bar below D+1 low",
        "stop": "D+1 high",
        "structure": "ATM put ~1–2w (spread if ATM debit very rich / IV>100%)",
        "size": "FULL",
        "evidence": {
            "frozen_rule_flow": "ABSORBED CALLS n17 82% win +5.55% mean (06/18–10/29)",
            "capture_sample": "n3 unique (META +0.8%, HIMS −3.1%, SMR armed) — thin; lean on frozen n17",
        },
        "days_early": "1 session vs D+2 official entry if using D+1 close",
    },
    "reversal_tier_A_forming": {
        "when": "On WATCHING after event day",
        "rule": "Event lean ≥ 2.5× (star preferred); wait for D+1 ABSORBED before Tier S",
        "action": "Alert only",
    },
    "validation": {
        "weekly_midweek": "Leave-one-week-out Sep14–Oct19: no midweek-only rule reached ≥75% next-week UP with n≥5",
        "weekly_sure_shot": "Use Friday-confirmed ★ / SQ+VC3 ★ (measured stats), not raw midweek lean",
        "reversal": "Sure-shot = ABSORBED CALLS filter on frozen SHORT rule",
    },
}


def score_weekly(zone: dict) -> list[dict]:
    weeks = sorted(zone.get("weekly_by_week") or {})
    if not weeks:
        return []
    latest = weeks[-1]
    rows = zone["weekly_by_week"][latest]
    sq = [s for s in (zone.get("squeeze_rows") or []) if s.get("week") == latest]
    sq_map = {s["sym"]: s.get("cell") or "" for s in sq}

    out = []
    # Tier S from Friday labels
    for r in rows:
        lab = r.get("next_week") or "—"
        flags = regime_flags(r.get("regime"))
        wu, _ = parse_wall(r.get("wall_up_dn"))
        lean = parse_lean(r.get("lean"))
        cd, _ = parse_call_put(r.get("call_put_days"))
        cell = sq_map.get(r["sym"], "")
        is_star = "BULLISH" in lab and "★" in lab and wu >= 2
        is_sq_star = "SQ+VC3" in cell and "★" in cell
        if is_star or is_sq_star:
            conf = 0.9 if is_sq_star else 0.82
            # haircut if live-forward window was soft for plain ★
            if is_star and not is_sq_star:
                conf = 0.78
            structure = "bull_call_spread_1em" if is_sq_star else "atm_call"
            out.append(
                {
                    "side": "bullish",
                    "tier": "S",
                    "source": "weekly",
                    "sym": r["sym"],
                    "week": latest,
                    "label": lab if is_star else cell,
                    "setup": "SQ+VC3 ★" if is_sq_star else "BULLISH ★",
                    "confidence": conf,
                    "entry": "Mon open (or Fri close) of week after signal Friday",
                    "stop": "signal-week low / thesis invalidation",
                    "structure": structure,
                    "why": f"Friday-confirmed {'SQ+VC3 ★' if is_sq_star else 'BULLISH ★'}; lean {lean}× wall_up {wu} calls {cd}",
                    "days_early": "0–1 vs slow reaction",
                    "regime": r.get("regime"),
                    "close": r.get("close"),
                    "lean": lean,
                    "wall_up": wu,
                }
            )

    # Tier A forming from partial week (use latest week's XM as if evaluating Thu)
    # If latest week is complete Friday, forming applies to CURRENT in-progress week only when
    # daily_xm has <5 days or caller passes asof midweek. Still emit Thu-path on complete week
    # as retrospective + any open next_wk names already covered by Tier S.
    for r in rows:
        flags = regime_flags(r.get("regime"))
        xm = parse_xm(r)
        if len(xm) < 4:
            cut = len(xm)
        else:
            cut = 4  # Thu
        partial = xm[:cut]
        cum = sum(partial)
        calls = sum(1 for x in partial if x >= 0.5)
        if flags["below_trend"] and flags["long_up"] and cum >= 2.0 and calls >= 2:
            # skip if already Tier S
            if any(x["sym"] == r["sym"] and x["tier"] == "S" for x in out):
                continue
            out.append(
                {
                    "side": "bullish",
                    "tier": "A",
                    "source": "weekly",
                    "sym": r["sym"],
                    "week": latest,
                    "label": r.get("next_week"),
                    "setup": "FORMING ★-path (Thu)",
                    "confidence": 0.45,
                    "entry": "DO NOT enter yet — wait Friday wall_up≥2 + official BULLISH/★ or SQ+VC3 ★",
                    "stop": "n/a (watch)",
                    "structure": "none_until_confirm",
                    "why": f"Thu-path: cum={cum:.1f} calls={calls} under 20<50 & 50>200 — LOO next_up only ~44%",
                    "days_early": "1 (prep only)",
                    "regime": r.get("regime"),
                    "close": r.get("close"),
                    "lean_partial": round(cum, 2),
                    "call_days_partial": calls,
                }
            )
    # Prefer S over A; sort by confidence
    out.sort(key=lambda x: (-x["confidence"], x["sym"]))
    return out


def score_reversals(zone: dict) -> list[dict]:
    sessions = sorted(zone.get("reversal_by_session") or {})
    if not sessions:
        return []
    latest = sessions[-1]
    p = zone["reversal_by_session"][latest]
    out = []

    # Tier S from short_now with absorbed size note OR events with ABSORBED + ARMED/IN
    for sn in p.get("short_now") or []:
        note = (sn.get("size_note") or "").lower()
        absorbed = "absorbed" in note
        chased = "puts" in note and "chase" in note
        trig = _f(re.search(r"(\d+(?:\.\d+)?)", str(sn.get("short_below") or "")).group(1) if re.search(r"(\d+(?:\.\d+)?)", str(sn.get("short_below") or "")) else None)
        stop = (lambda m: float(m.group(1)) if m else None)(re.search(r"(\d+(?:\.\d+)?)", str(sn.get("stop") or "")))
        if absorbed:
            out.append(
                {
                    "side": "bearish",
                    "tier": "S",
                    "source": "reversals",
                    "sym": sn["ticker"].replace(" ★", "").replace("★", "").strip(),
                    "session": latest,
                    "setup": "ABSORBED CALLS armed",
                    "confidence": 0.82,
                    "entry": f"short below {trig} (D+2 official) or D+1 close if already qualified",
                    "trigger": trig,
                    "stop": stop,
                    "structure": "atm_put",
                    "size": "FULL",
                    "why": "D+1 ABSORBED CALLS — frozen flow n17 82%/+5.55%",
                    "days_early": "1 if entering D+1 close vs D+2",
                }
            )
        elif not chased:
            # normal armed — not sure-shot; omit from sure-shot list
            pass

    for ev in p.get("events") or []:
        if (ev.get("d1_flow") or "") != "ABSORBED CALLS":
            continue
        state = (ev.get("state") or "").upper()
        if state not in ("ARMED", "IN", "WAITING D+1"):
            # still surface WAITING D+1 absorbed? rare — only if flow already labeled
            if "WAITING" in state:
                out.append(
                    {
                        "side": "bearish",
                        "tier": "A",
                        "source": "reversals",
                        "sym": ev["ticker"],
                        "session": latest,
                        "setup": "ABSORBED forming / wait",
                        "confidence": 0.7,
                        "entry": "wait for arm/trigger after D+1",
                        "trigger": None,
                        "stop": _f(ev.get("event_high")),
                        "structure": "atm_put",
                        "size": "FULL when armed",
                        "why": f"ABSORBED CALLS on D+1; state={ev.get('state')}; event lean {ev.get('event_lean_x')} D+1 {ev.get('d1_lean_x')}",
                        "days_early": "forming",
                    }
                )
            continue
        sym = ev["ticker"]
        if any(x["sym"] == sym and x["tier"] == "S" for x in out):
            continue
        if state in ("ARMED", "IN"):
            out.append(
                {
                    "side": "bearish",
                    "tier": "S",
                    "source": "reversals",
                    "sym": sym,
                    "session": latest,
                    "setup": f"ABSORBED CALLS {state}",
                    "confidence": 0.82,
                    "entry": "D+1 close or D+2 break of D+1 low",
                    "trigger": _f(ev.get("event_low")),
                    "stop": _f(ev.get("event_high")),
                    "structure": "atm_put",
                    "size": "FULL" if state != "IN" else "manage",
                    "why": f"ABSORBED event lean {ev.get('event_lean_x')} D+1 {ev.get('d1_lean_x')}",
                    "days_early": "1 vs D+2",
                    "star": bool(ev.get("star")),
                }
            )

    # Tier A: watching with fat event lean from latest events map
    lean_by_ticker = {}
    for ev in p.get("events") or []:
        lean_by_ticker[ev["ticker"]] = {
            "event_lean_x": parse_lean(ev.get("event_lean_x")),
            "d1_flow": ev.get("d1_flow"),
            "state": ev.get("state"),
            "star": bool(ev.get("star")),
        }
    for w in p.get("watching") or []:
        tk = w["ticker"]
        info = lean_by_ticker.get(tk) or {}
        el = info.get("event_lean_x") or 0
        if el >= 2.5 and (info.get("d1_flow") != "PUTS CHASED"):
            if any(x["sym"] == tk for x in out):
                continue
            out.append(
                {
                    "side": "bearish",
                    "tier": "A",
                    "source": "reversals",
                    "sym": tk,
                    "session": latest,
                    "setup": "WATCHING fat event lean",
                    "confidence": 0.55,
                    "entry": f"arms if {w.get('arms_if_breaks')} before {w.get('before')} — wait D+1 ABSORBED for Tier S",
                    "trigger": _f(w.get("arms_if_breaks")),
                    "stop": _f(w.get("before")),
                    "structure": "none_until_absorbed",
                    "size": "alert",
                    "why": f"event lean {el}× on watch list",
                    "days_early": "forming",
                }
            )

    out.sort(key=lambda x: (-x["confidence"], x["sym"]))
    return out


def score(zone: dict) -> dict:
    bull = score_weekly(zone)
    bear = score_reversals(zone)
    # Sure-shot only list (tier S)
    sure = [x for x in bull + bear if x.get("tier") == "S"]
    forming = [x for x in bull + bear if x.get("tier") == "A"]
    return {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "standing_rule": RULES_DOC["standing_rule"],
        "sure_shots": sure,
        "forming_watch": forming,
        "counts": {"sure": len(sure), "forming": len(forming)},
        "rules": RULES_DOC,
    }


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ZONE
    zone = json.loads(path.read_text())
    result = score(zone)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2))
    RULES_OUT.write_text(json.dumps(RULES_DOC, indent=2))
    print(json.dumps({
        "sure_shots": result["sure_shots"],
        "forming_watch": result["forming_watch"],
        "counts": result["counts"],
        "wrote": str(OUT),
    }, indent=2))


if __name__ == "__main__":
    main()
