"""Unit tests for the Finnhub test feed (finnhub_feed.py).

No network, no real key — every test feeds SYNTHETIC Finnhub tick
messages into the aggregator / message handler and checks the
1-minute candles that come out. Run from server/:

    .venv/bin/python -m unittest engine.test_finnhub_feed -v
"""

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from .finnhub_feed import (
    PAIR_TO_SYMBOL,
    SYMBOL_TO_PAIR,
    FinnhubConfig,
    FinnhubFeed,
    FinnhubKeyMissing,
    TickAggregator,
    parse_trade_message,
)
from .types import REAL_PAIRS, Broker, Market


def _ms(hour: int, minute: int, second: int = 0) -> int:
    """Epoch ms for 2026-11-25 HH:MM:SS UTC (a Wednesday; market open)."""
    dt = datetime(2026, 11, 25, hour, minute, second, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _trade_msg(*ticks) -> dict:
    """Build a synthetic Finnhub trade message from (symbol, price, ts_ms, volume)."""
    return {
        "type": "trade",
        "data": [
            {"s": s, "p": p, "t": t, **({} if v is None else {"v": v})}
            for (s, p, t, v) in ticks
        ],
    }


class TestTickAggregator(unittest.TestCase):
    def setUp(self):
        self.agg = TickAggregator()

    def test_basic_ohlcv(self):
        # Four ticks inside 10:00, then one tick in 10:01 closes it.
        self.assertIsNone(self.agg.ingest("EURUSD", 1.0852, _ms(10, 0, 5), 2))
        self.assertIsNone(self.agg.ingest("EURUSD", 1.0855, _ms(10, 0, 20), 3))
        self.assertIsNone(self.agg.ingest("EURUSD", 1.0850, _ms(10, 0, 40), 1))
        self.assertIsNone(self.agg.ingest("EURUSD", 1.0853, _ms(10, 0, 55), 4))
        candle = self.agg.ingest("EURUSD", 1.0854, _ms(10, 1, 5), 1)
        self.assertIsNotNone(candle)
        self.assertEqual(candle.pair, "EURUSD")
        self.assertEqual(candle.tf_min, 1)
        self.assertEqual(
            candle.start, datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(candle.open, 1.0852)
        self.assertEqual(candle.high, 1.0855)
        self.assertEqual(candle.low, 1.0850)
        self.assertEqual(candle.close, 1.0853)
        self.assertEqual(candle.volume, 10.0)
        self.assertEqual(candle.market, Market.REAL)
        self.assertEqual(candle.broker, Broker.FINNHUB)

    def test_tick_on_exact_boundary_belongs_to_new_minute(self):
        self.agg.ingest("EURUSD", 1.1000, _ms(10, 0, 30), 1)
        candle = self.agg.ingest("EURUSD", 1.1001, _ms(10, 1, 0), 1)
        self.assertIsNotNone(candle)
        self.assertEqual(candle.close, 1.1000)
        self.assertEqual(
            candle.start, datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
        )

    def test_late_tick_ignored(self):
        self.agg.ingest("EURUSD", 1.1000, _ms(10, 0, 30), 1)
        self.agg.ingest("EURUSD", 1.1001, _ms(10, 1, 5), 1)  # closes 10:00
        # A straggler from 10:00 arriving after the rollover: ignored.
        self.assertIsNone(self.agg.ingest("EURUSD", 9.9999, _ms(10, 0, 59), 1))
        self.assertEqual(self.agg.close_due(_ms(10, 1, 30)), [])

    def test_gap_minutes_get_no_fabricated_candle(self):
        self.agg.ingest("EURUSD", 1.1000, _ms(10, 0, 30), 1)
        # Next tick jumps to 10:03 — 10:01 and 10:02 had no ticks.
        candle = self.agg.ingest("EURUSD", 1.1005, _ms(10, 3, 10), 1)
        self.assertIsNotNone(candle)
        self.assertEqual(
            candle.start, datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
        )
        # Only ONE candle so far (10:00); nothing fabricated for 10:01/10:02.
        self.assertEqual(self.agg.close_due(_ms(10, 3, 30)), [])
        # 10:03 bucket closes when its minute ends.
        closed = self.agg.close_due(_ms(10, 4, 0))
        self.assertEqual(len(closed), 1)
        self.assertEqual(
            closed[0].start, datetime(2026, 11, 25, 10, 3, tzinfo=timezone.utc)
        )
        self.assertEqual(closed[0].open, 1.1005)

    def test_close_due_flushes_quiet_minute(self):
        self.agg.ingest("GBPUSD", 1.2700, _ms(10, 0, 10), 1)
        self.assertEqual(self.agg.close_due(_ms(10, 0, 59)), [])
        closed = self.agg.close_due(_ms(10, 1, 0))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].pair, "GBPUSD")
        self.assertEqual(closed[0].close, 1.2700)
        self.assertEqual(self.agg.close_due(_ms(10, 2, 0)), [])

    def test_missing_volume_counts_ticks(self):
        self.agg.ingest("EURUSD", 1.1000, _ms(10, 0, 5), None)
        self.agg.ingest("EURUSD", 1.1002, _ms(10, 0, 6), None)
        candle = self.agg.ingest("EURUSD", 1.1001, _ms(10, 1, 1), None)
        self.assertEqual(candle.volume, 2.0)  # 2 ticks, no "v" field

    def test_discard_drops_forming_buckets(self):
        self.agg.ingest("EURUSD", 1.1000, _ms(10, 0, 5), 1)
        self.agg.discard()
        self.assertEqual(self.agg.close_due(_ms(10, 5, 0)), [])

    def test_pairs_are_independent(self):
        self.agg.ingest("EURUSD", 1.1000, _ms(10, 0, 5), 1)
        self.agg.ingest("GBPUSD", 1.2700, _ms(10, 0, 6), 1)
        candle = self.agg.ingest("EURUSD", 1.1001, _ms(10, 1, 1), 1)
        self.assertEqual(candle.pair, "EURUSD")
        # GBPUSD bucket still forming, untouched by EURUSD's rollover.
        self.assertEqual(self.agg.close_due(_ms(10, 1, 0))[0].pair, "GBPUSD")


class TestMessageParsing(unittest.TestCase):
    def test_trade_message(self):
        ticks = parse_trade_message(
            _trade_msg(
                ("OANDA:EUR_USD", 1.0852, _ms(10, 0, 5), 2),
                ("OANDA:GBP_USD", 1.2700, _ms(10, 0, 6), 1),
            )
        )
        self.assertEqual(ticks, [
            ("EURUSD", 1.0852, _ms(10, 0, 5), 2.0),
            ("GBPUSD", 1.2700, _ms(10, 0, 6), 1.0),
        ])

    def test_unknown_symbol_skipped(self):
        ticks = parse_trade_message(
            _trade_msg(
                ("OANDA:EUR_USD", 1.0852, _ms(10, 0, 5), 1),
                ("OANDA:XAU_USD", 2650.0, _ms(10, 0, 5), 1),  # gold: not a pair
                ("BINANCE:BTCUSDT", 90000.0, _ms(10, 0, 5), 1),
            )
        )
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0][0], "EURUSD")

    def test_malformed_ticks_skipped_not_fatal(self):
        ticks = parse_trade_message({
            "type": "trade",
            "data": [
                {"s": "OANDA:EUR_USD", "p": 1.0852, "t": _ms(10, 0, 5)},
                {"s": "OANDA:EUR_USD", "p": "not-a-price", "t": _ms(10, 0, 6)},
                {"s": "OANDA:EUR_USD", "t": _ms(10, 0, 7)},  # no price
                {"p": 1.0852, "t": _ms(10, 0, 8)},  # no symbol
            ],
        })
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0][1], 1.0852)

    def test_non_trade_messages_parse_to_nothing(self):
        self.assertEqual(parse_trade_message({"type": "ping"}), [])
        self.assertEqual(parse_trade_message({"type": "error", "msg": "x"}), [])
        self.assertEqual(parse_trade_message({"type": "trade", "data": []}), [])
        self.assertEqual(parse_trade_message({}), [])
        self.assertEqual(parse_trade_message({"data": []}), [])


class TestFeedHandler(unittest.TestCase):
    def setUp(self):
        self.feed = FinnhubFeed(
            FinnhubConfig(api_key="fake-key-for-unit-tests"),
            pairs=["EURUSD"],
        )

    def test_handle_trade_message_closes_candle(self):
        closed, pong = self.feed._handle_message(_trade_msg(
            ("OANDA:EUR_USD", 1.0852, _ms(10, 0, 5), 1),
            ("OANDA:EUR_USD", 1.0855, _ms(10, 0, 55), 1),
        ))
        self.assertEqual(closed, [])
        self.assertFalse(pong)
        closed, pong = self.feed._handle_message(_trade_msg(
            ("OANDA:EUR_USD", 1.0853, _ms(10, 1, 2), 1),
        ))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].close, 1.0855)
        self.assertFalse(pong)

    def test_handle_ping_needs_pong(self):
        closed, pong = self.feed._handle_message({"type": "ping"})
        self.assertEqual(closed, [])
        self.assertTrue(pong)

    def test_handle_error_and_garbage_never_crashes(self):
        for payload in (
            {"type": "error", "msg": "Symbol not found."},
            {},
            {"type": "trade"},
            "not-a-dict",
            None,
        ):
            closed, pong = self.feed._handle_message(payload)
            self.assertEqual(closed, [])
            self.assertFalse(pong)

    def test_disconnect_state(self):
        self.feed._handle_message(_trade_msg(
            ("OANDA:EUR_USD", 1.0852, _ms(10, 0, 5), 1),
        ))
        self.feed._aggregator.discard()  # what run() does on a drop
        self.assertEqual(self.feed._aggregator.close_due(_ms(10, 5, 0)), [])


class TestConfig(unittest.TestCase):
    def test_missing_key_raises_clear_error(self):
        with self.assertRaises(FinnhubKeyMissing) as ctx:
            FinnhubConfig(api_key="").require()
        self.assertIn("FINNHUB_API_KEY is not set", str(ctx.exception))

    def test_from_env_reads_key(self):
        with mock.patch.dict(os.environ, {"FINNHUB_API_KEY": "env-key"}):
            self.assertEqual(FinnhubConfig.from_env().api_key, "env-key")

    def test_from_env_without_key_requires(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(FinnhubKeyMissing):
                FinnhubConfig.from_env().require()


class TestSymbolMapping(unittest.TestCase):
    def test_all_real_pairs_map_and_back(self):
        self.assertEqual(len(PAIR_TO_SYMBOL), len(REAL_PAIRS))
        for pair in REAL_PAIRS:
            symbol = PAIR_TO_SYMBOL[pair]
            self.assertTrue(symbol.startswith("OANDA:"))
            self.assertEqual(SYMBOL_TO_PAIR[symbol], pair)
        # No gold, no OTC symbols.
        self.assertNotIn("OANDA:XAU_USD", SYMBOL_TO_PAIR)


class TestFinnhubToEngineEndToEnd(unittest.TestCase):
    """Synthetic Finnhub ticks -> 1m candles -> strategies -> DB.

    No network, no real key. Proves the whole Finnhub-mode chain
    offline: message -> candle -> CandleStore -> HA-fade signal ->
    DbSignalStore -> the same SQLite store GET /api/signals reads.
    """

    def test_ticks_become_db_signals(self):
        import random
        import tempfile
        from pathlib import Path

        from .results import DbSignalStore, JsonFileSignalStore
        from .run import Engine
        from .strategies import build_strategies
        from .types import Broker

        rng = random.Random(7)
        tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(tmp.name) / "e2e.db")
        env = dict(os.environ)
        env.pop("SUPABASE_URL", None)
        env.pop("SUPABASE_KEY", None)
        env["ONYX_DB_PATH"] = db_path
        with mock.patch.dict(os.environ, env, clear=True):
            import db as _db

            store = DbSignalStore(JsonFileSignalStore(tmp.name))
            engine = Engine(
                ["EURUSD"],
                build_strategies(names=["ha_fade"], ha_tfs=[15]),
                tmp.name,
                Broker.FINNHUB,
                store=store,
            )
            feed = FinnhubFeed(
                FinnhubConfig(api_key="fake-key-for-unit-tests"),
                pairs=["EURUSD"],
            )
            # 1200 minutes of ticks, price trending in 30-minute blocks
            # so two same-colour 15m HA candles keep forming. (HA-fade
            # has a 50-candle warmup per TF, so 15m needs 750+ min
            # before its first signal — same reason the live mode
            # seeds history from Twelve Data.)
            price = 1.0850
            emitted_candles = []

            def on_candle(candle):
                emitted_candles.append(candle)
                engine.on_1m_bar(candle)

            for minute in range(1200):
                direction = 1 if (minute // 30) % 2 == 0 else -1
                for second in range(0, 60, 5):
                    price += direction * 0.0002 + rng.uniform(-0.00005, 0.00005)
                    closed, _ = feed._handle_message(_trade_msg(
                        ("OANDA:EUR_USD", round(price, 5), _ms(10, 0) + (minute * 60 + second) * 1000, 1),
                    ))
                    for c in closed:
                        on_candle(c)
                # Flush the minute's bucket on the timer path.
                for c in feed._aggregator.close_due(_ms(10, 0) + (minute + 1) * 60 * 1000):
                    on_candle(c)

            self.assertGreater(len(emitted_candles), 1000,
                               "expected ~1200 1m candles from 1200 min of ticks")
            ha_signals = [s for s in engine.emitted if s.strategy == "HA-fade"]
            self.assertGreater(len(ha_signals), 0,
                               "HA-fade should fire on trending 15m candles")
            self.assertTrue(all(s.status == "test" for s in engine.emitted))
            self.assertTrue(all(s.broker == Broker.FINNHUB for s in engine.emitted))
            rows = _db.get_signals(broker="finnhub")
            self.assertGreater(len(rows), 0, "signals must reach the API store")
            self.assertTrue(all(r["is_beta"] for r in rows))
            self.assertTrue(all(r["broker"] == "finnhub" for r in rows))
            self.assertTrue(all(r["market"] == "real" for r in rows))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
