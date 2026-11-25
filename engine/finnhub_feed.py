"""Finnhub live forex feed — TEMPORARY TEST FEED ONLY.

Streams real-time forex ticks from Finnhub's free websocket and
aggregates them into 1-minute candles for the engine. Real pairs
only (the six in ``engine/types.py`` REAL_PAIRS) — no OTC, no gold.

TEST PHASE ONLY (owner decision, Nov 25 2026): Finnhub is a stand-in
while the Quotex live feed is blocked (the Quotex session cookie
cannot be read from the browser, so no QUOTEX_SSID exists yet).
**Quotex remains the production feed.** Finnhub's free tier is
personal-use — it is fine for testing the bot while it earns from
ads only, but it must NOT be the feed for a sold premium product;
check the licence on finnhub.io before any commercial use.

How it works: Finnhub's free websocket pushes tick-by-tick trades
(no historical candle endpoint on the free tier — the paid candle
endpoint is never used). This module subscribes to the six pairs as
``OANDA:EUR_USD`` symbols and builds the 1-minute candles itself
(open = first tick, high/low from ticks, close = last tick,
volume = summed trade volume, or tick count when a trade carries
no volume). The engine's CandleStore then builds 2/3/5/15/30/60m
from those 1m candles, exactly as it does from Quotex.

LOGIN — env var ONLY, never a file, never a prompt, never logged:
  FINNHUB_API_KEY   free API key from finnhub.io/register
The key travels only in the websocket URL query string; it is never
printed, never written to disk, and never appears in any log line.

RESILIENCE: the stream auto-reconnects with exponential backoff
(1s -> 60s cap) and resubscribes. On any drop the forming 1-minute
buckets are DISCARDED — a bucket that spans a disconnect is
incomplete and must never become a candle. While disconnected, no
candles are emitted and no signals are made from stale data — the
same rule as QuotexFeed.

FREE-TIER LIMITS (verified Nov 25 2026): 50 symbols per socket,
60 REST calls/min. This module makes ZERO REST calls — websocket
only — and subscribes to 6 symbols, well inside the limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from .types import REAL_PAIRS, Broker, Candle, Market

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.finnhub.io"

# pair -> Finnhub websocket symbol (OANDA forex feed).
PAIR_TO_SYMBOL = {p: f"OANDA:{p[:3]}_{p[3:]}" for p in REAL_PAIRS}
SYMBOL_TO_PAIR = {s: p for p, s in PAIR_TO_SYMBOL.items()}

_MS_PER_MINUTE = 60_000


class FinnhubKeyMissing(RuntimeError):
    """Raised when FINNHUB_API_KEY is not set."""


class FinnhubConfig:
    """API-key config, from the environment only."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY")

    @classmethod
    def from_env(cls) -> "FinnhubConfig":
        return cls()

    def require(self) -> None:
        if not self.api_key:
            raise FinnhubKeyMissing(
                "FINNHUB_API_KEY is not set. Get a free key at "
                "https://finnhub.io/register (no credit card) and set "
                "it as the FINNHUB_API_KEY environment variable "
                "(Render env vars in production) — this module never "
                "asks for a key and never reads one from a file."
            )


class TickAggregator:
    """Fold Finnhub trade ticks into complete 1-minute candles.

    Pure logic, no network — unit-tested directly. One forming
    bucket per pair. A tick is (price, timestamp_ms, volume).
    """

    def __init__(self):
        # pair -> {"start_ms", "open", "high", "low", "close", "volume"}
        self._forming: dict[str, dict] = {}

    def ingest(
        self, pair: str, price: float, ts_ms: int, volume: float | None
    ) -> Candle | None:
        """Fold one tick in. Returns the closed 1m candle when the
        tick rolls the bucket into a new minute, else None."""
        bucket_ms = (ts_ms // _MS_PER_MINUTE) * _MS_PER_MINUTE
        vol = 1.0 if volume is None else float(volume)
        cur = self._forming.get(pair)
        if cur is None:
            self._forming[pair] = {
                "start_ms": bucket_ms, "open": price, "high": price,
                "low": price, "close": price, "volume": vol,
            }
            return None
        if bucket_ms == cur["start_ms"]:
            cur["close"] = price
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["volume"] += vol
            return None
        if bucket_ms < cur["start_ms"]:
            return None  # late tick for an old bucket: ignore
        # Bucket rolled over: close the old one, start the new one.
        # Minutes in between had no ticks — NO candle is fabricated
        # for them; the store simply has a gap there.
        closed = self._close_bucket(pair, cur)
        self._forming[pair] = {
            "start_ms": bucket_ms, "open": price, "high": price,
            "low": price, "close": price, "volume": vol,
        }
        return closed

    def close_due(self, now_ms: int | None = None) -> list[Candle]:
        """Close every forming bucket whose minute has ended.

        Called on a timer so a quiet minute still closes on time
        even when no new tick arrives to roll it over.
        """
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        closed = []
        for pair in list(self._forming):
            cur = self._forming[pair]
            if now_ms >= cur["start_ms"] + _MS_PER_MINUTE:
                closed.append(self._close_bucket(pair, cur))
                del self._forming[pair]
        return closed

    def discard(self) -> None:
        """Drop all forming buckets (feed dropped mid-minute).

        A bucket that spans a disconnect is incomplete — it must
        never become a candle. Called on every reconnect.
        """
        self._forming.clear()

    def _close_bucket(self, pair: str, bucket: dict) -> Candle:
        return Candle(
            pair=pair,
            tf_min=1,
            start=datetime.fromtimestamp(
                bucket["start_ms"] / 1000, tz=timezone.utc
            ),
            open=bucket["open"],
            high=bucket["high"],
            low=bucket["low"],
            close=bucket["close"],
            volume=bucket["volume"],
            market=Market.REAL,
            broker=Broker.FINNHUB,
        )


def parse_trade_message(payload: dict) -> list[tuple[str, float, int, float | None]]:
    """Extract (pair, price, ts_ms, volume) ticks from one Finnhub
    websocket message. Returns [] for anything that is not a trade
    message (ping, subscription acks, errors, malformed payloads).
    Unknown symbols are skipped; a bad tick never kills the batch.
    """
    if not isinstance(payload, dict) or payload.get("type") != "trade":
        return []
    out = []
    for tick in payload.get("data") or []:
        try:
            pair = SYMBOL_TO_PAIR.get(tick["s"])
            if pair is None:
                continue
            price = float(tick["p"])
            ts_ms = int(tick["t"])
            volume = tick.get("v")
            volume = None if volume is None else float(volume)
        except (KeyError, TypeError, ValueError):
            continue
        out.append((pair, price, ts_ms, volume))
    return out


class FinnhubFeed:
    """Live 1-minute candle stream from the Finnhub websocket.

    Usage:
        feed = FinnhubFeed(FinnhubConfig.from_env(), pairs=REAL_PAIRS)
        await feed.run(on_candle=...)   # blocks; reconnects forever
    """

    name = "finnhub"
    broker = Broker.FINNHUB

    def __init__(
        self,
        config: FinnhubConfig | None = None,
        pairs: list[str] | None = None,
    ):
        self.config = config or FinnhubConfig.from_env()
        # REAL pairs only. No OTC symbols, ever, in this phase.
        self.pairs = pairs or list(REAL_PAIRS)
        self._aggregator = TickAggregator()
        self._ws = None
        self.connected = False
        self.last_tick_at: float | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Connect and subscribe to the six pairs (key from env)."""
        self.config.require()
        from websockets.asyncio.client import connect as ws_connect

        # The key is in the URL query string only; it is never logged.
        url = f"{WS_URL}?token={self.config.api_key}"
        self._ws = await ws_connect(url, ping_interval=20, ping_timeout=10)
        for pair in self.pairs:
            await self._ws.send(json.dumps({
                "type": "subscribe",
                "symbol": PAIR_TO_SYMBOL[pair],
            }))
        self.connected = True
        logger.info(
            "Finnhub connected (test feed): subscribed to %d pairs",
            len(self.pairs),
        )

    async def close(self) -> None:
        self.connected = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ------------------------------------------------------------------
    # Message handling (sync — unit-tested without a network)
    # ------------------------------------------------------------------
    def _handle_message(self, payload: dict) -> tuple[list[Candle], bool]:
        """Handle one decoded websocket message.

        Returns (closed_candles, pong_needed). A ping needs a pong
        reply; trade ticks are folded into the aggregator; anything
        else is ignored (subscription acks, errors, malformed data).
        """
        if not isinstance(payload, dict):
            return [], False
        if payload.get("type") == "ping":
            return [], True
        if payload.get("type") == "error":
            logger.warning("Finnhub error message: %s", payload.get("msg"))
            return [], False
        closed = []
        for pair, price, ts_ms, volume in parse_trade_message(payload):
            self.last_tick_at = time.time()
            candle = self._aggregator.ingest(pair, price, ts_ms, volume)
            if candle is not None:
                closed.append(candle)
        return closed, False

    # ------------------------------------------------------------------
    # Run loop with auto-reconnect
    # ------------------------------------------------------------------
    async def run(self, on_candle, stop_after: float | None = None) -> None:
        """Connect, subscribe, stream 1m candles until stop_after.

        Reconnects with exponential backoff (1s -> 60s cap) on any
        drop. Forming buckets are discarded on disconnect, so no
        candle is ever built from stale or partial ticks. A 1s recv
        timeout keeps the loop waking to close due buckets even in
        a quiet minute.
        """
        deadline = time.time() + stop_after if stop_after else None
        backoff = 1.0
        while True:
            try:
                if not self.connected:
                    await self.connect()
                    backoff = 1.0
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    raw = None
                if raw is not None:
                    try:
                        payload = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                    closed, pong_needed = self._handle_message(payload)
                    if pong_needed:
                        await self._ws.send(json.dumps({"type": "pong"}))
                    for candle in closed:
                        on_candle(candle)
                # Close any bucket whose minute ended without a new tick.
                for candle in self._aggregator.close_due():
                    on_candle(candle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # drop -> reconnect, never crash
                logger.warning("Finnhub stream dropped (%s); reconnecting", exc)
                self.connected = False
                self._aggregator.discard()
                await self.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            if deadline is not None and time.time() >= deadline:
                return
