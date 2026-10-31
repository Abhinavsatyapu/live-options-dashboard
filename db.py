"""Neon Postgres helpers for positions + earnings cache."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

_SECRET_FILE = Path("/workspace/.secrets/options_db_url")


def database_url() -> Optional[str]:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        return url
    try:
        if _SECRET_FILE.is_file():
            return _SECRET_FILE.read_text().strip() or None
    except Exception:
        pass
    return None


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL not configured")
    return psycopg.connect(url, row_factory=dict_row)


def _ser(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def row_dict(r: dict) -> dict:
    return {k: _ser(v) for k, v in r.items()}


def ensure_schema() -> None:
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS positions (
          id BIGSERIAL PRIMARY KEY,
          ticker TEXT NOT NULL,
          signal_type TEXT NOT NULL,
          side TEXT NOT NULL CHECK (side IN ('call','put')),
          structure TEXT NOT NULL DEFAULT 'atm',
          long_occ TEXT NOT NULL,
          short_occ TEXT,
          quantity INT NOT NULL CHECK (quantity > 0),
          fill_debit NUMERIC(12,4) NOT NULL,
          entry_date DATE NOT NULL,
          expiry DATE,
          stop_underlying NUMERIC(12,4),
          profit_target_pct NUMERIC(8,4) NOT NULL DEFAULT 100,
          time_exit TEXT DEFAULT 'friday',
          cover_level NUMERIC(12,4),
          status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
          closed_at TIMESTAMPTZ,
          close_reason TEXT,
          close_pnl NUMERIC(14,2),
          notes TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS positions_status_idx ON positions(status)",
        """
        CREATE TABLE IF NOT EXISTS earnings_cache (
          symbol TEXT PRIMARY KEY,
          next_earnings DATE,
          fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          raw JSONB
        )
        """,
    ]
    with _connect() as conn:
        with conn.cursor() as cur:
            for s in stmts:
                cur.execute(s)
        conn.commit()


def list_positions(status: Optional[str] = "open") -> list[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT * FROM positions WHERE status=%s ORDER BY entry_date DESC, id DESC",
                    (status,),
                )
            else:
                cur.execute("SELECT * FROM positions ORDER BY entry_date DESC, id DESC")
            return [row_dict(r) for r in cur.fetchall()]


def get_position(pid: int) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM positions WHERE id=%s", (pid,))
            r = cur.fetchone()
            return row_dict(r) if r else None


def create_position(data: dict) -> dict:
    cols = [
        "ticker", "signal_type", "side", "structure", "long_occ", "short_occ",
        "quantity", "fill_debit", "entry_date", "expiry", "stop_underlying",
        "profit_target_pct", "time_exit", "cover_level", "notes",
    ]
    vals = [data.get(c) for c in cols]
    placeholders = ",".join(["%s"] * len(cols))
    sql = f"INSERT INTO positions ({','.join(cols)}) VALUES ({placeholders}) RETURNING *"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
            row = cur.fetchone()
        conn.commit()
        return row_dict(row)


def update_position(pid: int, data: dict) -> Optional[dict]:
    allowed = {
        "ticker", "signal_type", "side", "structure", "long_occ", "short_occ",
        "quantity", "fill_debit", "entry_date", "expiry", "stop_underlying",
        "profit_target_pct", "time_exit", "cover_level", "notes", "status",
        "close_reason", "close_pnl",
    }
    sets = []
    vals = []
    for k, v in data.items():
        if k in allowed:
            sets.append(f"{k}=%s")
            vals.append(v)
    if not sets:
        return get_position(pid)
    sets.append("updated_at=NOW()")
    if data.get("status") == "closed" and "closed_at" not in data:
        sets.append("closed_at=NOW()")
    vals.append(pid)
    sql = f"UPDATE positions SET {', '.join(sets)} WHERE id=%s RETURNING *"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
            row = cur.fetchone()
        conn.commit()
        return row_dict(row) if row else None


def delete_position(pid: int) -> bool:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM positions WHERE id=%s", (pid,))
            n = cur.rowcount
        conn.commit()
        return n > 0


def get_earnings_cached(symbol: str, max_age_hours: float = 24.0) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM earnings_cache WHERE symbol=%s", (symbol.upper(),))
            r = cur.fetchone()
            if not r:
                return None
            fetched = r["fetched_at"]
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600.0
            if age_h > max_age_hours:
                return None
            return row_dict(r)


def upsert_earnings(symbol: str, next_earnings: Optional[date], raw: Any = None) -> dict:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO earnings_cache (symbol, next_earnings, fetched_at, raw)
                VALUES (%s, %s, NOW(), %s::jsonb)
                ON CONFLICT (symbol) DO UPDATE SET
                  next_earnings=EXCLUDED.next_earnings,
                  fetched_at=NOW(),
                  raw=EXCLUDED.raw
                RETURNING *
                """,
                (symbol.upper(), next_earnings, json.dumps(raw) if raw is not None else None),
            )
            row = cur.fetchone()
        conn.commit()
        return row_dict(row)
