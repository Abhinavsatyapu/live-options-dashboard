"""Option-chain 401s must not look like a quote outage."""
from __future__ import annotations

import os
import time
import unittest
from datetime import date
from pathlib import Path

os.environ["DASH_ADMIN_PASSWORD"] = "test-admin-local"
os.environ["DASH_GUEST_PASSWORD"] = "test-guest-local"
os.environ["SESSION_SECRET"] = "unit-test-session-secret"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("FORCE_SECURE_COOKIE", None)

import app as appmod  # noqa: E402
import db as dbmod  # noqa: E402
from trade_config import REVERSAL_BEARISH, WEEKLY_SIGNALS  # noqa: E402

dbmod.database_url = lambda: None

ROOT = Path(__file__).resolve().parents[1]


def _expected_jobs(today: date) -> set[tuple[str, str]]:
    needed: set[tuple[str, str]] = set()
    for row in WEEKLY_SIGNALS + REVERSAL_BEARISH:
        if row.get("skip"):
            continue
        if date.fromisoformat(row["expiry"]) > today:
            needed.add((row["symbol"], row["expiry"]))
    return needed


class OptionJobTests(unittest.TestCase):
    def test_skip_rows_are_not_chain_jobs_even_before_expiry(self):
        today = date(2026, 11, 1)
        jobs = set(appmod._option_jobs(today))
        self.assertEqual(jobs, _expected_jobs(today))
        self.assertNotIn(("ABT", "2026-11-06"), jobs)
        self.assertNotIn(("SPY", "2026-11-06"), jobs)
        self.assertNotIn(("WDAY", "2026-11-06"), jobs)
        self.assertNotIn(("RBLX", "2026-11-06"), jobs)
        # Non-skip chains are still requested, including both COHR expiries.
        self.assertIn(("COHR", "2026-11-06"), jobs)
        self.assertIn(("COHR", "2026-11-13"), jobs)
        self.assertIn(("WDC", "2026-11-06"), jobs)
        self.assertIn(("PURR", "2026-11-13"), jobs)

    def test_today_and_past_expiries_are_not_fetched(self):
        today = date(2026, 11, 6)
        jobs = set(appmod._option_jobs(today))
        self.assertEqual(jobs, _expected_jobs(today))
        # COHR's Nov 6 squeeze is done; the Nov 13 reversal put stays.
        self.assertNotIn(("COHR", "2026-11-06"), jobs)
        self.assertIn(("COHR", "2026-11-13"), jobs)
        self.assertIn(("SMR", "2026-11-13"), jobs)
        self.assertNotIn(("ABT", "2026-11-06"), jobs)
        self.assertNotIn(("SPY", "2026-11-06"), jobs)
        self.assertNotIn(("WDC", "2026-11-06"), jobs)

    def test_chain_401_stays_off_the_payload_and_cards_still_return(self):
        calls = []
        yahoo = appmod.yahoo
        real_chain = yahoo.options_chain
        real_quotes = yahoo.quotes
        real_prior = yahoo.prior_sessions

        def boom(symbol, expiry_unix, timeout=18, attempts=4):
            calls.append(symbol.upper())
            raise RuntimeError(
                f"Yahoo GET failed status=401 path=/v7/finance/options/{symbol}"
            )

        def quotes(symbols, budget_sec=None):
            return {
                s: {
                    "symbol": s,
                    "regularMarketPrice": 25.0,
                    "regularMarketChangePercent": 0.4,
                    "regularMarketDayHigh": 26.0,
                    "regularMarketDayLow": 24.0,
                }
                for s in symbols
            }

        def priors(symbols, budget_sec=None):
            return {
                s: {
                    "prevDayHigh": 30.0,
                    "prevDayLow": 20.0,
                    "prevDayDate": "2026-11-05",
                    "todayHigh": 26.0,
                    "todayLow": 24.0,
                    "price": 25.0,
                }
                for s in symbols
            }

        yahoo.options_chain = boom
        yahoo.quotes = quotes
        yahoo.prior_sessions = priors
        try:
            t0 = time.perf_counter()
            data = appmod.build_snapshot()
            dt = time.perf_counter() - t0
        finally:
            yahoo.options_chain = real_chain
            yahoo.quotes = real_quotes
            yahoo.prior_sessions = real_prior

        self.assertLess(dt, 3.0, f"snapshot build took {dt:.2f}s")
        blob = " ".join(str(e) for e in (data.get("errors") or []))
        self.assertNotIn("401", blob)
        self.assertNotIn("status=", blob)
        self.assertNotIn("/v7/finance/options", blob)
        self.assertTrue(data["optionMidsDelayed"])
        self.assertGreater(len(data["intradayBullish"]), 0)
        self.assertEqual(data["intradayBullish"][0]["price"], 25.0)
        self.assertEqual(data["intradayBullish"][0]["entry"], 30.0)
        self.assertEqual(data["intradayBullish"][0]["stop"], 20.0)
        self.assertEqual(data["spy"]["price"], 25.0)
        self.assertGreater(len(data["weeklyBullish"]), 0)
        self.assertGreater(len(data["reversalBearish"]), 0)
        self.assertEqual(data["signalsAsOf"], "2026-10-30 close")
        self.assertNotIn("ABT", calls)
        self.assertNotIn("SPY", calls)
        self.assertIn("COHR", calls)

    def test_last_good_chain_is_kept_when_yahoo_options_401s(self):
        yahoo = appmod.yahoo
        key = ("COHR", appmod.expiry_unix("2026-11-13"))
        saved = {
            "optionChain": {
                "result": [{
                    "quote": {},
                    "options": [{
                        "calls": [{"strike": 210, "bid": 1.0, "ask": 1.2}],
                        "puts": [{"strike": 210, "bid": 2.0, "ask": 2.4}],
                    }],
                }]
            }
        }
        yahoo._chain_cache.pop(key, None)
        yahoo._last_good_chains[key] = saved
        real = yahoo._get_json

        def boom(*_a, **_k):
            raise RuntimeError("Yahoo GET failed status=401 path=/v7/finance/options/COHR")

        yahoo._get_json = boom
        try:
            got = yahoo.options_chain("COHR", key[1], timeout=1, attempts=1)
        finally:
            yahoo._get_json = real
            yahoo._last_good_chains.pop(key, None)
            yahoo._chain_cache.pop(key, None)
        self.assertEqual(got, saved)

    def test_intraday_page_does_not_paint_a_raw_chain_401(self):
        html = (ROOT / "static" / "index.html").read_text()
        self.assertNotIn("Quotes delayed / limited — ", html)
        self.assertIn("Some option mids are delayed.", html)
        start = html.index("function applyIntradayQuoteWarn")
        end = html.index("function applyPlanQuoteWarn")
        body = html[start:end]
        self.assertNotIn("data.errors", body)
        self.assertNotIn("401", body)
        self.assertNotIn("/v7/finance/options", body)
        self.assertIn("Some underlying quotes are delayed.", body)
        self.assertIn("Prior-day high/low still loading for some names.", body)
        self.assertEqual(appmod.health(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
