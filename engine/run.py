"""Engine runner: TEST MODE (replay stored candles) and LIVE MODE.

TEST MODE (default):
    python -m engine.run --mode test
Replays the stored Histdata 1-minute candles through the candle
store and the strategies, scores every signal with the result
checker, and prints each strategy's win rate next to the published
backtest number it should approximately reproduce. Nothing here
touches the network and no credentials are needed.

LIVE MODE:
    QUOTEX_EMAIL=... QUOTEX_SSID=... python -m engine.run --mode live
Streams live Quotex 1-minute candles (real pairs only), runs the
strategies, writes signals with status="test" to the local outbox,
and scores them after expiry. No signal is delivered to any user.

FINNHUB MODE (temporary test feed):
    FINNHUB_API_KEY=... python -m engine.run --mode finnhub
Streams live forex ticks from Finnhub's free websocket, aggregates
them into 1-minute candles, runs the same strategies unchanged, and
writes signals with status="test" / is_beta=true to the SAME store
the API reads (db.py via DbSignalStore, JSONL outbox as fallback),
so they show up in GET /api/signals for testing. Quotex remains the
production feed; Finnhub is test-phase only (free tier is
personal-use). No signal is delivered as a paid/premium signal.

Run from ~/workspace/signal-bot/server (so relative imports work):
    .venv/bin/python -m engine.run --mode test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.candles import CandleStore
from engine.feed import HistdataFileFeed, TwelveDataFeed
from engine.finnhub_feed import FinnhubConfig, FinnhubFeed, FinnhubKeyMissing
from engine.quotex_feed import QuotexConfig, QuotexFeed, QuotexSessionMissing
from engine.results import DbSignalStore, JsonFileSignalStore, ResultChecker
from engine.strategies import build_strategies
from engine.types import (
    REAL_PAIRS,
    Broker,
    Candle,
    Direction,
    Market,
    Signal,
)

# Published backtest numbers the test run should approximately
# reproduce (forex-only, real market, no gold). Sources:
#   backtest/results/RESULTS_ELITE_BINARY.md,
#   backtest/results/RESULTS_FOREX_ONLY.md,
#   backtest/results/RESULTS_DT_FULL.md (Prime "none" rows).
PUBLISHED = {
    "Elite": "47.0-50.2% combined (L 47.04-48.36% / O up to 50.18% "
              "1h->5m); forex-only best 49.50% (1h->5m overlap)",
    "Prime": "49.07% published (EUR/USD, 5m/5m, short file) — but that "
              "number embeds a 2-bar fractal lookahead (a swing at the "
              "touch candle is confirmed with future bars). The engine "
              "is live-faithful (no lookahead): 44.73% on 883 trades, "
              "matching an independent no-lookahead re-run of the "
              "backtest code (44.78%, 882 trades). Use --dataset short "
              "--strategies prime",
    "HA-fade": "forex-only 2-candle: 1m 47.54%, 5m 49.87%, "
               "15m 50.23%, 30m 50.95%, 1h 51.17%",
    "Turbo": "NEVER BACKTESTED — no published number; any win rate "
             "here is a first measurement, not a claim",
}


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="ONYX signal engine runner")
    p.add_argument("--mode", choices=["test", "live", "finnhub"], default="test")
    p.add_argument("--pairs", default=",".join(REAL_PAIRS),
                   help="comma-separated pairs, e.g. EURUSD,GBPUSD")
    p.add_argument("--strategies", default="elite,prime,ha_fade",
                   help="comma-separated: elite,prime,ha_fade,turbo")
    p.add_argument("--dataset", choices=["ext", "short"], default="ext",
                   help="ext = 15.5mo files; short = Aug-Nov 2026 files "
                        "(the Prime backtest's dataset)")
    p.add_argument("--elite-trigger", choices=["overlap", "literal"],
                   default="overlap")
    p.add_argument("--prime-expiry", type=int, default=5,
                   choices=[1, 5, 15])
    p.add_argument("--ha-tfs", default="15,30,60",
                   help="comma-separated minutes, e.g. 15,30,60")
    p.add_argument("--limit", type=int, default=0,
                   help="max 1m bars per pair (0 = all; for quick runs)")
    p.add_argument("--data-dir", default="data",
                   help="outbox dir for signals/results JSONL")
    p.add_argument("--out", default="",
                   help="also write the test summary JSON here")
    return p.parse_args(argv)


class Engine:
    """Wires store + strategies + result checker. One event loop for
    test replay and live streaming: every 1m bar goes through
    ``on_1m_bar`` in the same order."""

    def __init__(self, pairs, strategies, outbox_dir, broker: Broker,
                 store=None):
        self.pairs = pairs
        self.store = CandleStore(pairs)
        self.strategies = strategies
        self.broker = broker
        # Default store is the local JSONL outbox (test + live modes).
        # Finnhub mode passes a DbSignalStore so signals land in the
        # same DB the API serves (JSONL as fallback).
        self.checker = ResultChecker(
            store if store is not None else JsonFileSignalStore(outbox_dir)
        )
        self.emitted: list[Signal] = []
        self.scored: list[tuple[Signal, str]] = []
        self.voided_at_end = 0

    def on_1m_bar(self, bar: Candle) -> None:
        closed = self.store.add_1m(bar)
        fresh: list[Signal] = []
        for candle in closed:
            for strat in self.strategies:
                fresh.extend(strat.on_closed_candle(self.store, candle))
        for strat in self.strategies:
            fresh.extend(strat.on_1m_bar(self.store, bar))
        for sig in fresh:
            sig.broker = self.broker  # histdata in test, quotex live
            self.emitted.append(sig)
            self.checker.watch(sig)
        self.scored.extend(self.checker.on_1m_bar(self.store, bar))

    def finish_replay(self) -> None:
        voids = self.checker.flush_unscored_as_void()
        self.voided_at_end = len(voids)
        self.scored.extend(voids)

    def summary(self) -> dict:
        by_strategy: dict[str, dict] = defaultdict(
            lambda: {"emitted": 0, "trades": 0, "wins": 0, "void": 0}
        )
        by_key: dict[str, dict] = defaultdict(
            lambda: {"trades": 0, "wins": 0}
        )
        for sig in self.emitted:
            by_strategy[sig.strategy]["emitted"] += 1
        for sig, result in self.scored:
            b = by_strategy[sig.strategy]
            if result in ("WIN", "LOSS"):
                b["trades"] += 1
                if result == "WIN":
                    b["wins"] += 1
                key = f"{sig.strategy} exp{sig.expiry_min}m {sig.direction.value}"
                by_key[key]["trades"] += 1
                if result == "WIN":
                    by_key[key]["wins"] += 1
            elif result == "VOID":
                b["void"] += 1
        out = {}
        for name, b in sorted(by_strategy.items()):
            wr = (
                round(100 * b["wins"] / b["trades"], 2)
                if b["trades"] else None
            )
            out[name] = {**b, "win_rate_pct": wr,
                         "published": PUBLISHED.get(name, "")}
        out["_by_expiry_direction"] = {
            k: {**v, "win_rate_pct": round(100 * v["wins"] / v["trades"], 2)}
            for k, v in sorted(by_key.items()) if v["trades"]
        }
        out["_voided_at_end_of_data"] = self.voided_at_end
        return out


def run_test(args) -> dict:
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    strategies = build_strategies(
        names=[s.strip() for s in args.strategies.split(",")],
        elite_trigger=args.elite_trigger,
        prime_expiry_min=args.prime_expiry,
        ha_tfs=[int(x) for x in args.ha_tfs.split(",")],
    )
    feed = HistdataFileFeed(pairs, dataset=args.dataset)
    engine = Engine(pairs, strategies, args.data_dir, Broker.HISTDATA)
    t0 = time.time()
    counts: dict[str, int] = defaultdict(int)
    for pair, bar in feed.iter_merged():
        if args.limit and counts[pair] >= args.limit:
            continue
        counts[pair] += 1
        engine.on_1m_bar(bar)
    engine.finish_replay()
    summary = engine.summary()
    summary["_meta"] = {
        "mode": "test",
        "dataset": args.dataset,
        "pairs": pairs,
        "bars_per_pair": dict(counts),
        "seconds": round(time.time() - t0, 1),
        "note": "BETA: status=test only. No strategy here beats the "
                "53.2% breakeven — see published references.",
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2))
    return summary


async def run_live(args) -> None:
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    strategies = build_strategies(
        names=[s.strip() for s in args.strategies.split(",")],
        elite_trigger=args.elite_trigger,
        prime_expiry_min=args.prime_expiry,
        ha_tfs=[int(x) for x in args.ha_tfs.split(",")],
    )
    engine = Engine(pairs, strategies, args.data_dir, Broker.QUOTEX)
    config = QuotexConfig.from_env()
    config.require()  # clear error if session env vars are missing
    feed = QuotexFeed(config, pairs=pairs)

    # Seed history from Quotex (warmup for the strategies).
    await feed.connect()
    for pair in pairs:
        try:
            for c in await feed.seed_history(pair):
                engine.store.add_closed(c)
            print(f"seeded {pair}: history loaded", flush=True)
        except Exception as exc:
            print(f"seed {pair} failed ({exc}); continuing live", flush=True)
    for pair in pairs:
        await feed.subscribe(pair)

    # Optional free real-market feed as a backfill source (seed only;
    # it cannot sustain live 1m polling — see feed.py).
    td = TwelveDataFeed()
    if td.available:
        print("Twelve Data feed available (seed/backfill only)", flush=True)

    def on_candle(candle: Candle) -> None:
        engine.on_1m_bar(candle)
        for sig in engine.emitted[-5:]:
            pass  # signals are already in the outbox via the checker
        print(
            f"{candle.start.isoformat()} {candle.pair} 1m close "
            f"{candle.close} | emitted={len(engine.emitted)} "
            f"scored={len(engine.scored)}",
            flush=True,
        )

    print("LIVE MODE (BETA): signals written with status=test only. "
          "No delivery to users.", flush=True)
    try:
        await feed.run(on_candle)
    finally:
        await feed.close()


async def run_finnhub(args, on_engine=None) -> None:
    """FINNHUB MODE (temporary test feed).

    Same strategies, same scoring as live mode — only the candle
    source differs (Finnhub websocket ticks -> 1m candles). Signals
    are written with status="test" / is_beta=true to the same DB the
    API reads (DbSignalStore; local JSONL outbox as fallback), so
    they show up in GET /api/signals for testing. Nothing here is
    ever a paid/premium signal and nothing places a trade.

    on_engine: optional callback(engine) invoked once the Engine is
    built, so the API can keep a reference for on-demand Generate
    (POST /api/signals/generate). It does not change the feed loop.
    """
    config = FinnhubConfig.from_env()
    config.require()  # clear error if the key is missing — first thing
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    strategies = build_strategies(
        names=[s.strip() for s in args.strategies.split(",")],
        elite_trigger=args.elite_trigger,
        prime_expiry_min=args.prime_expiry,
        ha_tfs=[int(x) for x in args.ha_tfs.split(",")],
    )
    store = DbSignalStore(JsonFileSignalStore(args.data_dir))
    engine = Engine(pairs, strategies, args.data_dir, Broker.FINNHUB,
                    store=store)
    if on_engine is not None:
        on_engine(engine)
    feed = FinnhubFeed(config, pairs=pairs)

    # Finnhub free has NO historical candle endpoint, so seed history
    # from Twelve Data when its key is set (same pattern as live mode's
    # optional backfill feed). Without a seed the strategies need live
    # warmup before they can fire (Prime needs ~1500 1m bars).
    td = TwelveDataFeed()
    if td.available:
        for pair in pairs:
            try:
                for c in td.fetch_1m(pair):
                    engine.store.add_closed(c)
                print(f"seeded {pair}: history loaded (twelvedata)",
                      flush=True)
            except Exception as exc:
                print(f"seed {pair} failed ({exc}); continuing live",
                      flush=True)
    else:
        print("WARNING: no TWELVEDATA_API_KEY — no history seed. "
              "Strategies need live warmup before signals fire.",
              flush=True)

    def on_candle(candle: Candle) -> None:
        engine.on_1m_bar(candle)
        print(
            f"{candle.start.isoformat()} {candle.pair} 1m close "
            f"{candle.close} | emitted={len(engine.emitted)} "
            f"scored={len(engine.scored)}",
            flush=True,
        )

    print("FINNHUB MODE (TEST ONLY, BETA): Finnhub is the temporary "
          "test feed — Quotex remains the production feed. Signals "
          "written with status=test / is_beta=true to the API store. "
          "No delivery as paid/premium signals.", flush=True)
    try:
        await feed.run(on_candle)
    finally:
        await feed.close()


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        if args.mode == "test":
            run_test(args)
        elif args.mode == "finnhub":
            asyncio.run(run_finnhub(args))
        else:
            asyncio.run(run_live(args))
    except (FinnhubKeyMissing, QuotexSessionMissing) as exc:
        # Missing credentials fail CLEANLY: one clear line on stderr,
        # exit code 2, no traceback, no prompt.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
