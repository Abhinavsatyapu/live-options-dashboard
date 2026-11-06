"""Intraday Bullish and Intraday Reversals pages stay fast when Yahoo is blocked."""
from __future__ import annotations

import os
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["DASH_ADMIN_PASSWORD"] = "test-admin-local"
os.environ["DASH_GUEST_PASSWORD"] = "test-guest-local"
os.environ["SESSION_SECRET"] = "unit-test-session-secret"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("FORCE_SECURE_COOKIE", None)

import requests  # noqa: E402

import app as appmod  # noqa: E402
import db as dbmod  # noqa: E402
from intraday_signals import (  # noqa: E402
    apply_levels,
    build_cards,
    load_intraday_file,
    parse_daily_chart,
    select_latest,
    BULL_PATH,
    REV_PATH,
)
from trade_config import REVERSAL_BEARISH  # noqa: E402

dbmod.database_url = lambda: None

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
BULL_SEED = [
    "ENPH",
    "KDP",
    "PYPL",
    "VCYT",
    "ABSI",
    "GE",
    "FSLR",
    "BEAM",
    "PTRN",
    "SQQQ",
    "PBE",
    "KYMR",
]
LEVEL_FIELDS = (
    "symbol",
    "family",
    "type",
    "deltaGex",
    "atm",
    "signalTime",
    "price",
    "prevDayHigh",
    "prevDayLow",
    "entry",
    "stop",
    "triggered",
    "stopSideTraded",
    "status",
)


class IntradayRuleTests(unittest.TestCase):
    def test_seed_file_is_the_latest_strong_prints_only(self):
        doc = load_intraday_file(BULL_PATH)
        self.assertEqual(doc["signalsAsOf"], "2026-11-06 Ticker Levels")
        tickers = [row["ticker"] for row in doc["prints"]]
        self.assertEqual(tickers, BULL_SEED)
        self.assertNotIn("NRIX", tickers)
        for row in doc["prints"]:
            self.assertNotIn("FADE", row["type"].upper())
            self.assertNotIn("NEUTRAL", row["type"].upper())
            self.assertIsNone(row["atm"])
        by_ticker = {row["ticker"]: row for row in doc["prints"]}
        self.assertEqual(by_ticker["ENPH"]["family"], "IGN BOOK")
        self.assertEqual(by_ticker["ENPH"]["type"], "IGN-B")
        self.assertEqual(by_ticker["ENPH"]["time"], "10:09")
        self.assertEqual(by_ticker["ENPH"]["deltaGex"], 0.35)
        self.assertEqual(by_ticker["KDP"]["type"], "CONTINUATION")
        self.assertEqual(by_ticker["KDP"]["deltaGex"], -0.03)
        self.assertEqual(by_ticker["PBE"]["deltaGex"], 0.0)
        self.assertEqual(by_ticker["SQQQ"]["type"], "EXHAUSTION")

    def test_reversals_file_starts_empty(self):
        doc = load_intraday_file(REV_PATH)
        self.assertEqual(doc["prints"], [])
        self.assertIsNone(doc.get("signalsAsOf"))
        weekly = {row["symbol"] for row in REVERSAL_BEARISH}
        self.assertIn("SMR", weekly)
        self.assertIn("TER", {row["symbol"] for row in appmod.WATCHLIST})
        banned = weekly | {"CRDO", "TER"}
        self.assertTrue(banned.isdisjoint({row.get("ticker") for row in doc["prints"]}))

    def test_skip_filter_and_latest_print(self):
        prints = [
            {"ticker": "ENPH", "date": "2026-11-06", "time": "08:05", "family": "TRACKER FIRE", "type": "book B Neutral", "deltaGex": 0.1},
            {"ticker": "ENPH", "date": "2026-11-06", "time": "09:35", "family": "TRACKER FIRE", "type": "book A IGNITION", "deltaGex": 0.2},
            {"ticker": "ENPH", "date": "2026-11-06", "time": "10:09", "family": "IGN BOOK", "type": "IGN-B", "deltaGex": 0.35},
            {"ticker": "KDP", "date": "2026-11-06", "time": "09:31", "family": "FLOW STATE", "type": "FADE", "deltaGex": 0.5},
            {"ticker": "KDP", "date": "2026-11-06", "time": "10:05", "family": "FLOW STATE", "type": "CONTINUATION", "deltaGex": -0.03},
            {"ticker": "SQQQ", "date": "2026-11-06", "time": "09:10", "family": "FLOW STATE", "type": "FADE", "deltaGex": -0.2},
            {"ticker": "SQQQ", "date": "2026-11-06", "time": "09:32", "family": "FLOW STATE", "type": "EXHAUSTION", "deltaGex": -0.13},
            {"ticker": "NRIX", "date": "2026-11-06", "time": "09:40", "family": "FLOW STATE", "type": "FADE", "deltaGex": 0.4},
            {"ticker": "NRIX", "date": "2026-11-06", "time": "10:20", "family": "FLOW STATE", "type": "FADE", "deltaGex": 0.8},
            {"ticker": "ZZ", "date": "2026-11-06", "time": "09:00", "family": "FLOW STATE", "type": "EXHAUSTION", "deltaGex": 0.1},
            {"ticker": "ZZ", "date": "2026-11-06", "time": "11:00", "family": "TRACKER FIRE", "type": "Book A Neutral", "deltaGex": 0.1},
            {"ticker": "KEEP", "date": "2026-11-06", "time": "09:01", "family": "TRACKER FIRE", "type": "book A IGNITION", "deltaGex": 0.4},
        ]
        cards = build_cards(prints, "long", {}, {})
        self.assertEqual([c["symbol"] for c in cards], ["ENPH", "KDP", "SQQQ", "KEEP"])
        enph = cards[0]
        self.assertEqual(enph["type"], "IGN-B")
        self.assertEqual(enph["family"], "IGN BOOK")
        self.assertEqual(enph["signalTime"], "10:09 ET")
        self.assertEqual(enph["deltaGex"], 0.35)
        self.assertNotIn("NRIX", [c["symbol"] for c in cards])
        self.assertNotIn("ZZ", [c["symbol"] for c in cards])
        self.assertTrue(all("FADE" not in c["type"].upper() for c in cards))
        self.assertTrue(all("NEUTRAL" not in c["type"].upper() for c in cards))

    def test_bullish_entry_is_prior_high_and_stop_is_prior_low(self):
        base = {"symbol": "ENPH", "side": "long", "family": "IGN BOOK", "type": "IGN-B"}
        triggered = apply_levels(
            base, last=30.2, change_pct=1.0, today_high=31.0, today_low=29.4,
            prev_high=30.5, prev_low=28.0, prev_date="2026-11-05",
        )
        self.assertEqual(triggered["entry"], 30.5)
        self.assertEqual(triggered["stop"], 28.0)
        self.assertEqual(triggered["prevDayHigh"], 30.5)
        self.assertEqual(triggered["prevDayLow"], 28.0)
        self.assertTrue(triggered["triggered"])
        self.assertFalse(triggered["stopSideTraded"])
        self.assertEqual(triggered["status"], "TRIGGERED")

        by_last = apply_levels(
            base, last=30.5, change_pct=None, today_high=30.5, today_low=29.0,
            prev_high=30.5, prev_low=28.0, prev_date="2026-11-05",
        )
        self.assertTrue(by_last["triggered"])

        waiting = apply_levels(
            base, last=29.2, change_pct=None, today_high=29.8, today_low=28.4,
            prev_high=30.5, prev_low=28.0, prev_date="2026-11-05",
        )
        self.assertFalse(waiting["triggered"])
        self.assertFalse(waiting["stopSideTraded"])
        self.assertEqual(waiting["status"], "WAITING")
        self.assertEqual(waiting["entry"], 30.5)
        self.assertEqual(waiting["stop"], 28.0)

        stopped = apply_levels(
            base, last=29.0, change_pct=None, today_high=29.4, today_low=27.6,
            prev_high=30.5, prev_low=28.0, prev_date="2026-11-05",
        )
        self.assertFalse(stopped["triggered"])
        self.assertTrue(stopped["stopSideTraded"])
        self.assertEqual(stopped["status"], "STOP_SIDE")
        self.assertIn("under the previous-day low", stopped["statusNote"])

        pending = apply_levels(
            base, last=29.0, change_pct=None, today_high=None, today_low=None,
            prev_high=None, prev_low=None, prev_date=None,
        )
        self.assertIsNone(pending["entry"])
        self.assertIsNone(pending["stop"])
        self.assertIsNone(pending["prevDayHigh"])
        self.assertFalse(pending["triggered"])
        self.assertTrue(pending["levelsPending"])

    def test_reversal_entry_is_prior_low_and_stop_is_prior_high(self):
        base = {"symbol": "SAMPLE", "side": "short", "family": "FLOW STATE", "type": "EXHAUSTION"}
        triggered = apply_levels(
            base, last=9.4, change_pct=-1.0, today_high=10.2, today_low=8.8,
            prev_high=11.0, prev_low=9.0, prev_date="2026-11-05",
        )
        self.assertEqual(triggered["entry"], 9.0)
        self.assertEqual(triggered["stop"], 11.0)
        self.assertTrue(triggered["triggered"])
        self.assertEqual(triggered["status"], "TRIGGERED")

        above = apply_levels(
            base, last=11.4, change_pct=None, today_high=11.6, today_low=10.2,
            prev_high=11.0, prev_low=9.0, prev_date="2026-11-05",
        )
        self.assertFalse(above["triggered"])
        self.assertTrue(above["stopSideTraded"])
        self.assertEqual(above["status"], "STOP_SIDE")
        self.assertIn("above the previous-day high", above["statusNote"])
        self.assertEqual(above["entry"], 9.0)
        self.assertEqual(above["stop"], 11.0)

    def test_prior_session_bar_is_the_day_before_today(self):
        def stamp(y, m, d):
            return int(datetime(y, m, d, 9, 30, tzinfo=ET).timestamp())

        payload = {
            "chart": {
                "result": [{
                    "meta": {
                        "exchangeTimezoneName": "America/New_York",
                        "regularMarketPrice": 12.5,
                        "regularMarketDayHigh": 13.0,
                        "regularMarketDayLow": 11.0,
                        "chartPreviousClose": 12.0,
                    },
                    "timestamp": [stamp(2026, 11, 4), stamp(2026, 11, 5), stamp(2026, 11, 6)],
                    "indicators": {"quote": [{"high": [20.0, 30.5, None], "low": [18.0, 28.0, 11.0]}]},
                }]
            }
        }
        parsed = parse_daily_chart(payload, date(2026, 11, 6))
        self.assertEqual(parsed["prevDayHigh"], 30.5)
        self.assertEqual(parsed["prevDayLow"], 28.0)
        self.assertEqual(parsed["prevDayDate"], "2026-11-05")
        self.assertEqual(parsed["todayHigh"], 13.0)
        self.assertEqual(parsed["todayLow"], 11.0)
        self.assertEqual(parsed["price"], 12.5)
        self.assertIsNone(parse_daily_chart({"chart": {"result": []}}, date(2026, 11, 6)))

    def test_select_latest_is_used_for_display_order(self):
        rows = select_latest(
            [
                {"ticker": "BBB", "date": "2026-11-06", "time": "09:00", "family": "FLOW STATE", "type": "IGNITION"},
                {"ticker": "AAA", "date": "2026-11-06", "time": "10:00", "family": "IGN BOOK", "type": "IGN-A"},
            ],
            "long",
        )
        self.assertEqual([row["symbol"] for row in rows], ["AAA", "BBB"])
        self.assertEqual(rows[0]["type"], "IGN-A")


class IntradaySnapshotTests(unittest.TestCase):
    def test_shell_returns_seed_without_network(self):
        calls = []
        real = requests.sessions.Session.send

        def blocked(self, request, **kwargs):
            calls.append(getattr(request, "url", "?"))
            raise AssertionError("intraday shell tried to use the network")

        requests.sessions.Session.send = blocked
        try:
            t0 = time.perf_counter()
            data = appmod.build_shell_snapshot()
            dt = time.perf_counter() - t0
        finally:
            requests.sessions.Session.send = real

        self.assertLess(dt, 1.0)
        self.assertEqual(calls, [])
        self.assertEqual([c["symbol"] for c in data["intradayBullish"]], BULL_SEED)
        self.assertEqual(data["intradayReversals"], [])
        self.assertEqual(data["intradayBullishAsOf"], "2026-11-06 Ticker Levels")
        self.assertIsNone(data["intradayReversalsAsOf"])
        self.assertEqual(data["signalsAsOf"], "2026-10-30 close")
        self.assertEqual(
            [c["symbol"] for c in data["reversalBearish"]],
            [row["symbol"] for row in REVERSAL_BEARISH],
        )
        card = data["intradayBullish"][0]
        for key in LEVEL_FIELDS:
            self.assertIn(key, card)
        self.assertEqual(card["symbol"], "ENPH")
        self.assertEqual(card["family"], "IGN BOOK")
        self.assertEqual(card["type"], "IGN-B")
        self.assertIsNone(card["price"])
        self.assertIsNone(card["prevDayHigh"])
        self.assertIsNone(card["prevDayLow"])
        self.assertIsNone(card["entry"])
        self.assertIsNone(card["stop"])
        self.assertFalse(card["triggered"])
        self.assertGreater(len(data["earlySureShots"]), 0)

    def test_saved_levels_fill_entry_and_stop_without_yahoo(self):
        levels = {
            "ENPH": {
                "prevDayHigh": 30.5,
                "prevDayLow": 28.0,
                "prevDayDate": "2026-11-05",
                "todayHigh": 31.0,
                "todayLow": 29.4,
            }
        }
        quotes = {
            "ENPH": {
                "regularMarketPrice": 30.2,
                "regularMarketDayHigh": 31.0,
                "regularMarketDayLow": 29.4,
                "regularMarketChangePercent": 1.1,
            }
        }
        data = appmod._assemble_snapshot(quotes, {}, [], quotes_pending=False, daily_levels=levels)
        card = next(c for c in data["intradayBullish"] if c["symbol"] == "ENPH")
        self.assertEqual(card["price"], 30.2)
        self.assertEqual(card["entry"], 30.5)
        self.assertEqual(card["stop"], 28.0)
        self.assertEqual(card["prevDayHigh"], 30.5)
        self.assertEqual(card["prevDayLow"], 28.0)
        self.assertTrue(card["triggered"])
        self.assertEqual(data["intradayReversals"], [])
        self.assertEqual(data["signalsAsOf"], "2026-10-30 close")

    def test_failed_refresh_keeps_intraday_cards(self):
        shell = appmod.build_shell_snapshot()
        shell["intradayBullish"][0]["entry"] = 123.45
        shell["intradayBullish"][0]["prevDayHigh"] = 123.45
        shell["intradayBullish"][0]["stop"] = 100.0
        appmod._store_snapshot(shell, live=True)
        real = appmod.build_snapshot

        def boom(*_a, **_k):
            raise RuntimeError("yahoo down")

        appmod.build_snapshot = boom
        try:
            with appmod._refresh_lock:
                appmod._refreshing = False
            appmod._kick_refresh()
            for _ in range(50):
                with appmod._refresh_lock:
                    if not appmod._refreshing:
                        break
                time.sleep(0.05)
            with appmod._cache_lock:
                data = appmod._cache["data"]
        finally:
            appmod.build_snapshot = real
        self.assertEqual(data["intradayBullish"][0]["symbol"], "ENPH")
        self.assertEqual(data["intradayBullish"][0]["entry"], 123.45)
        self.assertEqual(data["intradayBullish"][0]["prevDayHigh"], 123.45)
        self.assertEqual(data["intradayReversals"], [])

    def test_prior_session_budget_returns_last_good(self):
        release = threading.Event()
        appmod.yahoo._last_good_daily["ENPH"] = {
            "prevDayHigh": 10.0,
            "prevDayLow": 8.0,
            "prevDayDate": "2026-11-05",
        }
        real = appmod.yahoo._daily_one

        def hang(_symbol):
            release.wait(5)
            return None

        appmod.yahoo._daily_one = hang
        try:
            t0 = time.perf_counter()
            out = appmod.yahoo.prior_sessions(["ENPH"], budget_sec=0.4)
            dt = time.perf_counter() - t0
        finally:
            release.set()
            appmod.yahoo._daily_one = real
            appmod.yahoo._last_good_daily.pop("ENPH", None)
        self.assertLess(dt, 2.0)
        self.assertEqual(out["ENPH"]["prevDayHigh"], 10.0)
        self.assertEqual(out["ENPH"]["prevDayLow"], 8.0)

    def test_pages_and_snapshot_are_fast_when_yahoo_is_blocked(self):
        from fastapi.testclient import TestClient

        started = threading.Event()
        release = threading.Event()
        real = appmod.build_snapshot

        def hanging(*_a, **_k):
            started.set()
            release.wait(20)
            data = appmod.build_shell_snapshot()
            data["quotesPending"] = False
            return data

        appmod.build_snapshot = hanging
        with appmod._refresh_lock:
            appmod._refreshing = False
        with appmod._cache_lock:
            appmod._cache["data"] = None
            appmod._cache["ts"] = 0
            appmod._cache["live"] = False
        try:
            with TestClient(appmod.app) as client:
                self.assertTrue(started.wait(3), "background refresh did not start")
                locked = client.get("/intraday-bullish", follow_redirects=False)
                self.assertIn(locked.status_code, (302, 303))
                self.assertIn("/login", locked.headers.get("location", ""))
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json(), {"ok": True})
                login = client.post(
                    "/login",
                    data={"username": "abhinav", "password": "test-admin-local"},
                    follow_redirects=False,
                )
                self.assertIn(login.status_code, (200, 303))
                t0 = time.perf_counter()
                res = client.get("/api/snapshot")
                dt = time.perf_counter() - t0
                self.assertLess(dt, 2.0, f"snapshot took {dt:.2f}s")
                self.assertEqual(res.status_code, 200)
                body = res.json()
                self.assertEqual([c["symbol"] for c in body["intradayBullish"]], BULL_SEED)
                self.assertEqual(body["intradayReversals"], [])
                self.assertEqual(body["intradayBullishAsOf"], "2026-11-06 Ticker Levels")
                self.assertEqual(body["signalsAsOf"], "2026-10-30 close")
                self.assertEqual(body["refreshSec"], 120)
                self.assertTrue(body["quotesPending"])
                card = body["intradayBullish"][0]
                for key in LEVEL_FIELDS:
                    self.assertIn(key, card)
                self.assertGreater(len(body["reversalBearish"]), 0)
                self.assertGreater(len(body["earlySureShots"]), 0)
                weekly_rev = {row["symbol"] for row in body["reversalBearish"]}
                self.assertIn("SMR", weekly_rev)
                self.assertIn("TER", {row["symbol"] for row in body["watchlist"]})
                self.assertTrue((weekly_rev | {"CRDO", "TER"}).isdisjoint({c["symbol"] for c in body["intradayReversals"]}))

                bull = client.get("/intraday-bullish")
                self.assertEqual(bull.status_code, 200)
                self.assertIn("Intraday Bullish", bull.text)
                self.assertIn('path === "/intraday-bullish"', bull.text)
                self.assertIn("REFRESH_MS = 120000", bull.text)
                self.assertIn("Early ★", bull.text)
                rev = client.get("/intraday-reversals")
                self.assertEqual(rev.status_code, 200)
                self.assertIn("Intraday Reversals", rev.text)
                self.assertIn("No intraday reversal signals", rev.text)
                self.assertIn('path === "/intraday-reversals"', rev.text)
                home = client.get("/")
                self.assertEqual(home.status_code, 200)
                self.assertIn('href="/intraday-bullish"', home.text)
                self.assertIn('href="/intraday-reversals"', home.text)
                self.assertIn('data-tab="early"', home.text)
                self.assertIn('data-tab="reversal"', home.text)
        finally:
            release.set()
            for _ in range(100):
                with appmod._refresh_lock:
                    busy = appmod._refreshing
                if not busy:
                    break
                time.sleep(0.05)
            appmod.build_snapshot = real


if __name__ == "__main__":
    unittest.main()
