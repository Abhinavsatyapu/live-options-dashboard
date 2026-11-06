"""Signal cards must paint without waiting on Yahoo."""
from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path

# Fake local credentials only. Never read or print real secrets.
os.environ["DASH_ADMIN_PASSWORD"] = "test-admin-local"
os.environ["DASH_GUEST_PASSWORD"] = "test-guest-local"
os.environ["SESSION_SECRET"] = "unit-test-session-secret"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("FORCE_SECURE_COOKIE", None)

import requests  # noqa: E402

import app as appmod  # noqa: E402
import db as dbmod  # noqa: E402
from trade_config import REVERSAL_BEARISH, WEEKLY_SIGNALS  # noqa: E402

dbmod.database_url = lambda: None

ROOT = Path(__file__).resolve().parents[1]


class SnapshotShellTests(unittest.TestCase):
    def test_shell_matches_saved_signals_and_does_not_touch_network(self):
        calls = []
        real = requests.sessions.Session.send

        def blocked(self, request, **kwargs):
            calls.append(getattr(request, "url", "?"))
            raise AssertionError("snapshot shell tried to use the network")

        requests.sessions.Session.send = blocked
        try:
            t0 = time.perf_counter()
            data = appmod.build_shell_snapshot()
            dt = time.perf_counter() - t0
        finally:
            requests.sessions.Session.send = real

        self.assertLess(dt, 1.0)
        self.assertEqual(calls, [])
        self.assertTrue(data["ok"])
        self.assertTrue(data["quotesPending"])
        self.assertFalse(data["quotesLive"])
        self.assertEqual(data["signalsAsOf"], "2026-10-30 close")
        self.assertEqual(data["refreshSec"], 120.0)
        self.assertIn("SPY negative gamma", data["banner"]["text"])
        self.assertEqual(
            [r["symbol"] for r in data["weeklyBullish"]],
            [s["symbol"] for s in WEEKLY_SIGNALS if s["section"] == "bullish"],
        )
        self.assertEqual(
            [r["symbol"] for r in data["weeklySqueeze"]],
            [s["symbol"] for s in WEEKLY_SIGNALS if s["section"] == "squeeze"],
        )
        self.assertEqual(
            [r["symbol"] for r in data["reversalBearish"]],
            [s["symbol"] for s in REVERSAL_BEARISH],
        )
        self.assertEqual(
            [r["symbol"] for r in data["watchlist"]],
            [w["symbol"] for w in appmod.WATCHLIST],
        )
        early = appmod.load_early().get("sure_shots") or []
        self.assertEqual([r["sym"] for r in data["earlySureShots"]], [s["sym"] for s in early])
        self.assertGreater(len(data["earlySureShots"]), 0)
        self.assertGreater(len(data["weeklyBullish"]), 0)
        self.assertGreater(len(data["weeklySqueeze"]), 0)
        self.assertGreater(len(data["reversalBearish"]), 0)
        card = data["weeklyBullish"][0]
        self.assertTrue(card.get("long_occ"))
        self.assertIsNone(card.get("price"))

    def test_page_polls_every_two_minutes(self):
        html = (ROOT / "static" / "index.html").read_text()
        self.assertIn("REFRESH_MS = 120000", html)
        self.assertNotIn("setInterval(refresh, 5000)", html)
        self.assertNotIn("55000", html)
        self.assertNotIn("Connecting…", html)

    def test_failed_refresh_keeps_signal_cards(self):
        shell = appmod.build_shell_snapshot()
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
                live = appmod._cache["live"]
        finally:
            appmod.build_snapshot = real
        self.assertTrue(live)
        self.assertEqual(
            [r["sym"] for r in data["earlySureShots"]],
            [r["sym"] for r in shell["earlySureShots"]],
        )
        self.assertTrue(any(str(e).startswith("refresh:") for e in data["errors"]))

    def test_endpoint_returns_cards_while_yahoo_is_blocked(self):
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
                self.assertIn("no-store", res.headers.get("cache-control", ""))
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertIn("no-store", page.headers.get("cache-control", ""))
                self.assertIn("Loading signals", page.text)
                body = res.json()
                self.assertTrue(body["ok"])
                self.assertTrue(body["quotesPending"])
                self.assertGreater(len(body["earlySureShots"]), 0)
                self.assertGreater(len(body["weeklySqueeze"]), 0)
                self.assertGreater(len(body["reversalBearish"]), 0)
                self.assertEqual(body["refreshSec"], 120)
                t1 = time.perf_counter()
                again = client.get("/api/snapshot")
                self.assertLess(time.perf_counter() - t1, 2.0)
                self.assertGreater(len(again.json()["weeklyBullish"]), 0)
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
