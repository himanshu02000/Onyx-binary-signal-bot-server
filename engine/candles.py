"""Candle store: in-memory + bounded history of live candles.

The store is fed 1-minute candles and builds every higher timeframe
the strategies need (2m, 3m, 5m, 15m, 30m, 60m) by aggregation.

COMPLETE-CANDLE RULE (backtest assumption A1, all strategies): a
higher-timeframe candle counts only if it contains ALL of its 1-minute
bars. A bucket missing even one 1-minute bar (weekend edge, holiday,
feed gap) is dropped entirely — never used by a strategy and never
scored. This is what makes the engine's numbers comparable to the
published backtests.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

from .types import Candle, Market

# Timeframes (minutes) the store builds. 1m is the base feed.
TIMEFRAMES = (1, 2, 3, 5, 15, 30, 60)

# How many closed candles to keep per (pair, timeframe). Bounded so a
# long live run cannot grow memory without limit. 2000 x 1m bars cover
# Prime's 300-candle 5m lookback (300 x 5 = 1500 1m bars).
KEEP = {1: 2000, 2: 1000, 3: 700, 5: 400, 15: 200, 30: 200, 60: 200}

# 1-minute closes kept for expiry scoring (result checker). Expiries
# here are at most 15 minutes, so 3 hours of 1m closes is plenty.
M1_CLOSE_KEEP_MINUTES = 180


def _bucket_start(ts: datetime, tf_min: int) -> datetime:
    """Floor a UTC timestamp to its tf-minute bucket."""
    epoch_min = int(ts.timestamp() // 60)
    floored = (epoch_min // tf_min) * tf_min
    return datetime.fromtimestamp(floored * 60, tz=timezone.utc)


class CandleStore:
    """Per-pair candle history, built from the 1-minute feed."""

    def __init__(self, pairs: list[str] | None = None,
                 build_tfs: tuple[int, ...] = TIMEFRAMES):
        """build_tfs: which higher timeframes to aggregate from the 1m
        feed (default: all). A diagnostic can exclude a TF here and
        feed pre-built candles for it via add_closed() instead —
        e.g. to replay the backtest's 5m file verbatim, incomplete
        bars and all, and check the strategy logic in isolation."""
        self.build_tfs = tuple(build_tfs)
        self._closed: dict[tuple[str, int], deque] = {}
        # Partial (still forming) bucket per (pair, tf): [bucket_start, bars]
        self._partial: dict[tuple[str, int], tuple[datetime, list[Candle]]] = {}
        # (pair, 1m-bar start) -> close, pruned to the last 3 hours.
        self._m1_closes: dict[tuple[str, datetime], float] = {}
        self.pairs = list(pairs or [])

    # ------------------------------------------------------------------
    # Feeding
    # ------------------------------------------------------------------
    def add_1m(self, bar: Candle) -> list[Candle]:
        """Feed one complete 1-minute bar.

        Returns every higher-timeframe candle that closed because of
        this bar (in time order, oldest first), including the 1m bar
        itself. A bucket that closes incomplete is dropped silently.
        """
        assert bar.tf_min == 1, "add_1m takes 1-minute bars only"
        closed: list[Candle] = [bar]
        self._remember_m1_close(bar)
        # 1m itself is always stored, even if build_tfs excludes it.
        for tf in dict.fromkeys((1,) + tuple(self.build_tfs)):
            c = self._add_to_tf(bar, tf)
            if c is not None:
                closed.append(c)
        closed.sort(key=lambda c: (c.start, c.tf_min))
        return closed

    def add_closed(self, candle: Candle) -> None:
        """Feed an already-built closed candle (history backfill)."""
        key = (candle.pair, candle.tf_min)
        dq = self._closed.setdefault(key, deque(maxlen=KEEP[candle.tf_min]))
        if dq and dq[-1].start >= candle.start:
            return  # duplicate or out of order: ignore
        dq.append(candle)
        if candle.tf_min == 1:
            self._remember_m1_close(candle)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def get(self, pair: str, tf_min: int, n: int | None = None) -> list[Candle]:
        """Last n closed candles for (pair, tf), oldest first."""
        dq = self._closed.get((pair, tf_min), deque())
        items = list(dq)
        return items[-n:] if n is not None else items

    def last(self, pair: str, tf_min: int) -> Candle | None:
        dq = self._closed.get((pair, tf_min), deque())
        return dq[-1] if dq else None

    def get_range(
        self, pair: str, tf_min: int, start: datetime, end: datetime
    ) -> list[Candle]:
        """Closed candles with start in [start, end), oldest first."""
        return [
            c
            for c in self._closed.get((pair, tf_min), deque())
            if start <= c.start < end
        ]

    def m1_close_at(self, pair: str, ts: datetime) -> float | None:
        """Close of the 1-minute bar starting at ts, or None if unknown."""
        return self._m1_closes.get((pair, ts))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _remember_m1_close(self, bar: Candle) -> None:
        self._m1_closes[(bar.pair, bar.start)] = bar.close
        cutoff = bar.start - timedelta(minutes=M1_CLOSE_KEEP_MINUTES)
        for key in [k for k in self._m1_closes if k[1] < cutoff]:
            del self._m1_closes[key]

    def _add_to_tf(self, bar: Candle, tf: int) -> Candle | None:
        """Aggregate one 1m bar into its tf bucket. Returns the candle
        that closed (if any) when the bucket rolls over — only when the
        bucket held exactly tf 1-minute bars (complete-candle rule)."""
        if tf == 1:
            self.add_closed(bar)
            return None
        key = (bar.pair, tf)
        bucket = _bucket_start(bar.start, tf)
        current = self._partial.get(key)
        if current is None:
            self._partial[key] = (bucket, [bar])
            return None
        cur_bucket, bars = current
        if bucket == cur_bucket:
            bars.append(bar)
            return None
        # Bucket rolled over: finalize the old bucket.
        del self._partial[key]
        self._partial[key] = (bucket, [bar])
        if len(bars) != tf:
            return None  # incomplete bucket: dropped (A1)
        closed = Candle(
            pair=bar.pair,
            tf_min=tf,
            start=cur_bucket,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            market=bar.market,
            broker=bar.broker,
        )
        self.add_closed(closed)
        return closed
