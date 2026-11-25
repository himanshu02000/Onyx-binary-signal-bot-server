"""Shared types for the ONYX signal engine.

BETA MODE: every signal the engine emits carries status="test" (or
"demo"). No signal is delivered to any user. See README.md.

SECRECY: a Signal carries ONLY what a trader needs — pair, direction,
entry, expiry, strategy name, broker, market. No strategy logic, no
zone/level/threshold ever leaves this package.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Market(str, Enum):
    """Which market a candle/signal came from.

    This phase (owner decision, Nov 24 2026): REAL only. OTC is
    "coming soon" — the field exists so OTC can be added later without
    changing the signal shape, but nothing in this phase emits OTC.
    """

    REAL = "real"
    OTC = "otc"


class Broker(str, Enum):
    QUOTEX = "quotex"
    HISTDATA = "histdata"      # stored test candles (replay only)
    TWELVEDATA = "twelvedata"  # free real-market feed (seed/backfill)
    FINNHUB = "finnhub"        # free Finnhub websocket (TEST FEED ONLY)


class Direction(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


# Real-market forex pairs this phase. Six pairs, all in the Histdata
# backtest data. NO gold (XAU/USD excluded: binary is forex-only for
# the owner) and NO OTC pairs (coming soon).
REAL_PAIRS = ["EURUSD", "GBPUSD", "EURJPY", "USDJPY", "AUDUSD", "USDCAD"]

# Session window (UTC) used by every backtest: entries 00:00-21:00.
SESSION_START_HOUR = 0
SESSION_END_HOUR = 21

# Payout the owner actually gets on Quotex real pairs (~88%) and the
# breakeven win rate that implies. Every strategy tested so far LOSES
# against this (best ~51%). Do not claim any strategy wins.
PAYOUT_REAL = 0.88
BREAKEVEN_REAL = 1 / (1 + PAYOUT_REAL)  # 0.5319 -> 53.2%


def in_session(entry_time: datetime) -> bool:
    """True if an entry time falls inside the 00:00-21:00 UTC window."""
    return SESSION_START_HOUR <= entry_time.hour <= SESSION_END_HOUR


@dataclass(frozen=True)
class Candle:
    """One complete OHLC candle. Only complete candles ever enter the store."""

    pair: str
    tf_min: int
    start: datetime  # UTC, candle open time
    open: float
    high: float
    low: float
    close: float
    market: Market = Market.REAL
    broker: Broker = Broker.HISTDATA
    volume: float | None = None  # summed tick volume (Finnhub feed); None when unknown

    @property
    def end(self) -> datetime:
        from datetime import timedelta

        return self.start + timedelta(minutes=self.tf_min)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)


@dataclass
class Signal:
    """A signal the engine emits. PUBLIC shape — see module docstring.

    ``entry_price`` is None only in live mode before the entry candle
    opens; in test mode (replay) it is always filled. ``entry_time``
    is the time the trade is entered (open of the entry candle).
    """

    pair: str
    broker: Broker
    strategy: str
    direction: Direction
    entry_time: datetime
    expiry_min: int
    market: Market = Market.REAL
    entry_price: float | None = None
    status: str = "test"  # BETA MODE: "test"/"demo" only. Never "live".
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_public_dict(self) -> dict:
        """The ONLY fields that may ever leave the server.

        No zone, level, threshold, sweep, FVG or any other trace of
        how the signal was found.
        """
        return {
            "id": self.id,
            "pair": self.pair,
            "broker": self.broker.value,
            "strategy": self.strategy,
            "direction": self.direction.value,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": self.entry_price,
            "expiry_min": self.expiry_min,
            "market": self.market.value,
            "status": self.status,
        }
