"""Pluggable real-market feeds.

Three implementations:

1. ``HistdataFileFeed`` — TEST MODE. Replays the stored Histdata
   1-minute candles in ~/workspace/signal-bot/backtest/data/. No key,
   no network, works everywhere. This is what ``run.py --mode test``
   uses to prove the strategy code reproduces the backtests.

2. ``TwelveDataFeed`` — the picked FREE live-feed option for
   production (seed history + gap backfill ONLY — see the limits
   below). Activates only when TWELVEDATA_API_KEY is set in the
   environment; with no key the engine runs in test mode and live
   mode runs on Quotex alone.

3. ``FinnhubFeed`` (``finnhub_feed.py``) — TEMPORARY TEST FEED.
   Streams live forex ticks from Finnhub's free websocket and
   aggregates them into 1-minute candles. Used by
   ``run.py --mode finnhub`` while the Quotex feed is blocked (no
   QUOTEX_SSID). Quotex remains the production feed; Finnhub's free
   tier is personal-use — fine for testing, not for a sold product.

FREE-LIMIT MATH (Twelve Data Basic plan, free — verified Nov 24 2026
against their pricing page and third-party pricing trackers):
  * 8 API credits per minute, 800 credits per day, reset midnight UTC.
  * 1 standard time_series call = 1 credit per symbol.
  * Free tier is documented as non-commercial use on some trackers —
    check the licence on twelvedata.com/pricing before using it for a
    user-facing product; it is fine for testing and seeding.
  * 800 credits/day CANNOT sustain live 1-minute polling for 6 pairs
    (6 pairs x 1 call/min x 1440 min = 8,640/day). So this feed is
    used for (a) seeding history at startup and (b) backfilling gaps
    after a Quotex reconnect — e.g. 6 pairs every 15 minutes costs
    6 x 4 x 24 = 576 credits/day, inside the budget. Live 1-minute
    candles in this phase come from Quotex (Way 1). If Quotex drops,
    real-market signals PAUSE — the engine never emits a signal from
    stale or guessed candles.

Alternatives considered (not implemented): TraderMade free tier
(1,000 requests/month — too small for 6 pairs), Alpha Vantage free
(25 requests/day — far too small). Finnhub free WAS implemented
(Nov 25 2026, ``finnhub_feed.py``) — not via its paid candle
endpoint, but as a websocket tick stream aggregated into 1m
candles; it is the temporary TEST feed while Quotex is blocked,
not a production feed (personal-use licence).
"""

from __future__ import annotations

import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .types import Broker, Candle, Market

BACKTEST_DATA_DIR = Path(
    os.path.expanduser("~/workspace/signal-bot/backtest/data")
)

# Histdata file names: EURUSD_1m_ext.csv (15.5 months) and the short
# EURUSD_1m.csv (Aug-Nov 2026, the file the original Prime backtest
# used). "ext" is the default test dataset.
EXT_SUFFIX = "_1m_ext.csv"
SHORT_SUFFIX = "_1m.csv"


def _parse_histdata_time(raw: str) -> datetime:
    # "2025-08-01 00:00:00+00:00"
    dt = datetime.fromisoformat(raw.strip())
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class HistdataFileFeed:
    """Replay stored Histdata 1-minute candles (test mode)."""

    name = "histdata"
    broker = Broker.HISTDATA

    def __init__(
        self,
        pairs: list[str],
        data_dir: Path | str = BACKTEST_DATA_DIR,
        dataset: str = "ext",
    ):
        self.pairs = pairs
        self.data_dir = Path(data_dir)
        suffix = EXT_SUFFIX if dataset == "ext" else SHORT_SUFFIX
        self._files = {p: self.data_dir / f"{p}{suffix}" for p in pairs}
        missing = [str(f) for f in self._files.values() if not f.exists()]
        if missing:
            raise FileNotFoundError(
                f"Histdata files missing: {missing} (dataset={dataset!r})"
            )

    def iter_1m(self, pair: str):
        """Yield 1-minute Candles for one pair, oldest first."""
        with open(self._files[pair], newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                yield Candle(
                    pair=pair,
                    tf_min=1,
                    start=_parse_histdata_time(row["time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    market=Market.REAL,
                    broker=Broker.HISTDATA,
                )

    def iter_merged(self):
        """Yield (pair, Candle) across all pairs in time order."""
        import heapq

        iters = []
        for pair in self.pairs:
            it = self.iter_1m(pair)
            try:
                first = next(it)
            except StopIteration:
                continue
            iters.append((first.start, pair, first, it))
        heapq.heapify(iters)
        while iters:
            _, pair, candle, it = heapq.heappop(iters)
            yield pair, candle
            try:
                nxt = next(it)
            except StopIteration:
                continue
            heapq.heappush(iters, (nxt.start, pair, nxt, it))


class TwelveDataFeed:
    """Free Twelve Data feed — seed history + gap backfill only.

    Env var: TWELVEDATA_API_KEY. No key -> this feed is inert and
    ``available`` is False; the engine must not fail because of it.

    Rate guard: at most one call per symbol per 15 minutes, and a
    daily credit counter capped at 700 (leaving headroom under the
    800/day free cap). Calls past the guard raise RuntimeError
    instead of burning the daily budget.
    """

    name = "twelvedata"
    broker = Broker.TWELVEDATA
    BASE_URL = "https://api.twelvedata.com/time_series"
    MIN_INTERVAL_SECONDS = 15 * 60
    DAILY_CREDIT_CAP = 700

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("TWELVEDATA_API_KEY")
        self._last_call: dict[str, float] = {}
        self._credits_used_today = 0
        self._credits_day = datetime.now(timezone.utc).date()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def fetch_1m(
        self, pair: str, outputsize: int = 5000
    ) -> list[Candle]:
        """Fetch recent 1-minute candles for one forex pair.

        One call = 1 credit. Returns oldest-first closed candles.
        Raises RuntimeError when the rate guard blocks the call.
        """
        if not self.available:
            raise RuntimeError(
                "TwelveDataFeed has no API key (set TWELVEDATA_API_KEY)"
            )
        now = time.time()
        today = datetime.now(timezone.utc).date()
        if today != self._credits_day:
            self._credits_day = today
            self._credits_used_today = 0
        if self._credits_used_today >= self.DAILY_CREDIT_CAP:
            raise RuntimeError("Twelve Data daily credit cap reached")
        last = self._last_call.get(pair, 0)
        if now - last < self.MIN_INTERVAL_SECONDS:
            raise RuntimeError(
                f"Twelve Data rate guard: {pair} fetched too recently"
            )
        symbol = f"{pair[:3]}/{pair[3:]}"  # EURUSD -> EUR/USD
        url = (
            f"{self.BASE_URL}?symbol={symbol}&interval=1min"
            f"&outputsize={outputsize}&apikey={self.api_key}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "onyx-engine/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("status") == "error":
            raise RuntimeError(f"Twelve Data error: {payload.get('message')}")
        values = payload.get("values") or []
        self._last_call[pair] = now
        self._credits_used_today += 1
        candles = []
        for row in reversed(values):  # API returns newest first
            candles.append(
                Candle(
                    pair=pair,
                    tf_min=1,
                    start=_parse_histdata_time(row["datetime"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    market=Market.REAL,
                    broker=Broker.TWELVEDATA,
                )
            )
        return candles
