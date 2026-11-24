"""pyquotex client wrapper — Quotex demo-account candle stream (Way 1).

READ-ONLY. DEMO account only. This module NEVER places trades: it
does not import, call or expose any buy/sell/order method, and the
account is forced to PRACTICE mode.

LOGIN — env vars ONLY, never a file, never a prompt:
  QUOTEX_EMAIL   demo-account email (e.g. the vault demo account)
  QUOTEX_SSID    session token for that account (SSID)
  QUOTEX_USER_AGENT (optional) browser user-agent string for the session

There is NO password here, and none is ever asked for. pyquotex's own
``credentials()`` helper prompts for an email/password and writes
settings/config.ini — this module NEVER calls it. Likewise
``set_session()`` would persist the SSID token to session.json on
disk — this module NEVER calls it either: the token is injected into
the client's in-memory ``session_data`` only, and pyquotex's
session-file directory is pointed at a fresh temporary directory so
nothing credential-like is ever written into the repo or home.

How auth works: with a token in ``session_data``, pyquotex's
``connect()`` skips its email/password ``authenticate()`` step and
authorises the websocket with the SSID. If the token is expired or
rejected, connect fails and the caller must obtain a fresh SSID via
the vault/browser path — this module cannot and will not recover a
login by itself.

SCOPE (Nov 24 owner decision): REAL pairs only. No OTC symbols are
subscribed, no OTC candles are built, no OTC signal is emitted.
The ``market`` label on every candle/signal is "real" this phase;
the field is kept so OTC can be added later without changing shape.

RESILIENCE: the stream auto-reconnects with exponential backoff.
If Quotex drops, the feed reports itself unavailable and the engine
keeps running on the real-market feed — it NEVER emits a signal
from stale or missing candles.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .types import REAL_PAIRS, Broker, Candle, Market

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class QuotexSessionMissing(RuntimeError):
    """Raised when the required session env vars are not set."""


class QuotexConfig:
    """Session config, from the environment only."""

    def __init__(
        self,
        email: str | None = None,
        ssid: str | None = None,
        user_agent: str | None = None,
    ):
        self.email = email or os.environ.get("QUOTEX_EMAIL")
        self.ssid = ssid or os.environ.get("QUOTEX_SSID")
        self.user_agent = (
            user_agent or os.environ.get("QUOTEX_USER_AGENT") or DEFAULT_USER_AGENT
        )

    @classmethod
    def from_env(cls) -> "QuotexConfig":
        return cls()

    def missing(self) -> list[str]:
        out = []
        if not self.email:
            out.append("QUOTEX_EMAIL")
        if not self.ssid:
            out.append("QUOTEX_SSID")
        return out

    def require(self) -> None:
        missing = self.missing()
        if missing:
            raise QuotexSessionMissing(
                "Quotex session env vars not set: "
                + ", ".join(missing)
                + ". Set them in the environment (Render env vars in "
                "production) — this module never asks for a password and "
                "never reads credentials from a file."
            )


class QuotexFeed:
    """Live 1-minute candle stream for the real forex pairs.

    Usage:
        feed = QuotexFeed(QuotexConfig.from_env(), pairs=REAL_PAIRS)
        await feed.connect()
        await feed.run(on_candle=...)   # blocks; reconnects forever
    """

    def __init__(
        self,
        config: QuotexConfig | None = None,
        pairs: list[str] | None = None,
    ):
        self.config = config or QuotexConfig.from_env()
        # REAL pairs only. No OTC symbols, ever, in this phase.
        self.pairs = pairs or list(REAL_PAIRS)
        self.client = None
        self.connected = False
        self.last_tick_at: float | None = None
        self._current_1m: dict[str, dict] = {}  # pair -> forming bucket
        self._session_dir = Path(tempfile.mkdtemp(prefix="onyx-quotex-"))

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Connect with the SSID session token (no password anywhere)."""
        self.config.require()
        from pyquotex import config as _pq_config
        from pyquotex.stable_api import Quotex

        # Point pyquotex's session-file directory at a temp dir so its
        # load_session() can never write session.json into the repo.
        # (The token itself is only ever held in memory — see below.)
        _pq_config.base_dir = self._session_dir

        client = Quotex(
            email=self.config.email,
            password="",  # unused: token auth skips authenticate()
            lang="en",
            user_agent=self.config.user_agent,
            root_path=str(self._session_dir),
            user_data_dir=str(self._session_dir / "browser"),
            asset_default=self.pairs[0],
            period_default=60,
        )
        # Inject the session token IN MEMORY ONLY. Never set_session()
        # (that would persist the token to session.json on disk).
        client.session_data = {
            "token": self.config.ssid,
            "cookies": None,
            "user_agent": self.config.user_agent,
        }
        client.set_account_mode("PRACTICE")  # DEMO account, always
        ok, reason = await client.connect()
        if not ok or not await client.check_connect():
            raise RuntimeError(f"Quotex connect failed: {reason}")
        self.client = client
        self.connected = True
        logger.info("Quotex connected (demo/PRACTICE): %s", reason)

    async def close(self) -> None:
        self.connected = False
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = None

    # ------------------------------------------------------------------
    # History seed + live ticks
    # ------------------------------------------------------------------
    async def seed_history(self, pair: str, count: int = 600) -> list[Candle]:
        """Backfill recent 1m candles so strategies have warmup data."""
        if self.client is None:
            raise RuntimeError("QuotexFeed not connected")
        raw = await self.client.get_candles(
            pair, time.time(), count, 60
        )
        out = []
        for row in raw or []:
            out.append(
                Candle(
                    pair=pair,
                    tf_min=1,
                    start=datetime.fromtimestamp(
                        int(row["time"]), tz=timezone.utc
                    ),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    market=Market.REAL,
                    broker=Broker.QUOTEX,
                )
            )
        return sorted(out, key=lambda c: c.start)

    async def subscribe(self, pair: str) -> None:
        if self.client is None:
            raise RuntimeError("QuotexFeed not connected")
        await self.client.start_candles_stream(pair, 60)

    def _ingest_tick(self, pair: str, tick) -> Candle | None:
        """Fold one realtime tick into the forming 1m bucket.

        pyquotex exposes the latest tick as [symbol, ts, price, dir].
        Returns the closed 1m candle when the bucket rolls over.
        """
        try:
            _symbol, ts, price, _direction = tick
        except (TypeError, ValueError):
            return None
        ts = int(ts)
        price = float(price)
        bucket = (ts // 60) * 60
        cur = self._current_1m.get(pair)
        if cur is None:
            self._current_1m[pair] = {
                "start": bucket, "open": price, "high": price,
                "low": price, "close": price,
            }
            return None
        if bucket == cur["start"]:
            cur["close"] = price
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            return None
        if bucket < cur["start"]:
            return None  # late tick for an old bucket: ignore
        closed = Candle(
            pair=pair, tf_min=1,
            start=datetime.fromtimestamp(cur["start"], tz=timezone.utc),
            open=cur["open"], high=cur["high"], low=cur["low"],
            close=cur["close"], market=Market.REAL, broker=Broker.QUOTEX,
        )
        self._current_1m[pair] = {
            "start": bucket, "open": price, "high": price,
            "low": price, "close": price,
        }
        return closed

    async def poll_once(self, on_candle) -> int:
        """One poll of the realtime ticks. Returns closed candles seen."""
        if self.client is None:
            return 0
        n = 0
        for pair in self.pairs:
            tick = await self.client.get_realtime_candles(pair)
            if not tick:
                continue
            self.last_tick_at = time.time()
            closed = self._ingest_tick(pair, tick)
            if closed is not None:
                n += 1
                on_candle(closed)
        return n

    # ------------------------------------------------------------------
    # Run loop with auto-reconnect
    # ------------------------------------------------------------------
    async def run(self, on_candle, stop_after: float | None = None) -> None:
        """Connect, subscribe, stream 1m candles until stop_after.

        Reconnects with exponential backoff (1s -> 60s cap) on any
        drop. While disconnected, no candles are emitted — the engine
        must keep running on the real-market feed instead.
        """
        deadline = time.time() + stop_after if stop_after else None
        backoff = 1.0
        while True:
            try:
                if not self.connected:
                    await self.connect()
                    for pair in self.pairs:
                        await self.subscribe(pair)
                    backoff = 1.0
                await self.poll_once(on_candle)
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # drop -> reconnect, never crash
                logger.warning("Quotex stream dropped (%s); reconnecting", exc)
                self.connected = False
                await self.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            if deadline is not None and time.time() >= deadline:
                return
