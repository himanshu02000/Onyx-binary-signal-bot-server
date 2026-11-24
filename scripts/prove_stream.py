"""
prove_stream.py — PROVE the live Quotex candle stream works.

Connects with a Quotex DEMO session (env vars only) and prints live
1-minute candles for REAL forex pairs for 60 seconds.

    QUOTEX_EMAIL=... QUOTEX_SSID=... python scripts/prove_stream.py

Required env vars:
  QUOTEX_EMAIL  demo-account email
  QUOTEX_SSID   session token for that account

There is NO password here. This script never asks for one, never
reads one from a file, and never places a trade (read-only demo
stream). If the env vars are missing it exits with a clear message
instead of crashing or prompting.

How to get the values (parent agent / owner): the Quotex demo login
lives in the Secure Vault (email Moneyfesting1000dollars@gmail.com).
Sign in to Quotex once in the browser via the vault path, copy the
session SSID from the logged-in browser session, and export it as
QUOTEX_SSID together with QUOTEX_EMAIL. The SSID expires — if the
stream rejects it, obtain a fresh one the same way.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.quotex_feed import QuotexConfig, QuotexFeed, QuotexSessionMissing
from engine.types import REAL_PAIRS, Candle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pairs",
        default="EURUSD,GBPUSD",
        help="comma-separated REAL pairs to stream (default: EURUSD,GBPUSD)",
    )
    parser.add_argument(
        "--seconds", type=int, default=60, help="how long to stream"
    )
    args = parser.parse_args()

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    bad = [p for p in pairs if p not in REAL_PAIRS]
    if bad:
        print(f"ERROR: not a real-pair in this phase: {bad}. "
              f"Real pairs are: {REAL_PAIRS}. OTC is coming soon.")
        return 2

    config = QuotexConfig.from_env()
    try:
        config.require()
    except QuotexSessionMissing as exc:
        print(f"Quotex session env vars not set: {exc}")
        print("Set QUOTEX_EMAIL and QUOTEX_SSID in the environment and "
              "run again. No password is needed or accepted here.")
        return 2

    seen: list[Candle] = []

    def on_candle(candle: Candle) -> None:
        seen.append(candle)
        print(
            f"{candle.start.isoformat()} {candle.pair} 1m "
            f"O={candle.open} H={candle.high} L={candle.low} "
            f"C={candle.close} market={candle.market.value} "
            f"broker={candle.broker.value}",
            flush=True,
        )

    async def _run() -> None:
        feed = QuotexFeed(config, pairs=pairs)
        try:
            await feed.run(on_candle, stop_after=args.seconds)
        finally:
            await feed.close()

    print(f"Connecting to Quotex (demo, read-only) for {pairs} ...",
          flush=True)
    t0 = time.time()
    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        print(f"STREAM FAILED: {exc}")
        return 1
    print(f"\nStream proof: {len(seen)} closed 1m candle(s) in "
          f"{time.time() - t0:.0f}s for {pairs}.")
    if not seen:
        print("WARNING: connected but no candles arrived — the stream "
              "is NOT proven. Check the pairs and the session.")
        return 1
    print("STREAM PROVEN: live Quotex candles received.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
