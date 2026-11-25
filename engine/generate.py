"""On-demand "Generate signal" — one 1-minute signal right now, per pair.

The user taps Generate on a pair instead of waiting for the automatic
engine to find a setup. This module evaluates against the LIVE engine's
own CandleStore and strategy state — it never starts a second engine
and never re-runs the automatic evaluation for a candle (no double-emit).

Two paths, in order:

1. REAL SETUP ("setup"): if one of the live strategies has a pending
   entry armed for this pair (a setup that fired on a closed candle
   and is waiting for its entry candle), consume it and return it as a
   1-minute signal. Consuming the pending entry is what stops the
   automatic engine finalising the same setup again. The direction
   and strategy name come from the setup itself; only the expiry is
   forced to 1 minute and the entry to the next minute open, which is
   the contract of this feature.
2. QUICK READ ("quick_read", strategy "Momentum"): if no strategy has
   a setup right now, read the fastest honest state — the Heikin Ashi
   colour of the last closed 1-minute candle (walking back over doji
   candles) — and return that direction as a 1-minute signal. It is
   labelled "Momentum" and source "quick_read" everywhere so it is
   NEVER presented as a strategy setup.

Entry price: the last closed 1-minute close, as a proxy for the next
minute's open (the true open is unknown until that candle opens).
The ResultChecker scores the signal from that entry price when the
entry candle closes — exactly once, through the same path as
automatic signals.

Session rule: a consumed setup is only returned if the forced entry
time falls inside that strategy's session window (00:00-21:00 UTC
for Elite / HA-fade; Prime has no window) — the same rule the
engine's finalize_pending() applies. The Momentum fallback has no
session filter: the point of Generate is a signal now.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .types import Direction, Market, Signal


def next_minute_open(now: datetime | None = None) -> datetime:
    """The open of the next 1-minute candle (UTC)."""
    now = now or datetime.now(timezone.utc)
    floored = now.replace(second=0, microsecond=0)
    return floored + timedelta(minutes=1)


def heikin_ashi_direction(store, pair: str) -> Direction | None:
    """Direction from the Heikin Ashi colour of the last closed 1m
    candle. HA is rebuilt from scratch over the last 12 closed 1m
    candles (seeded from the first bar's open — the colour converges
    within a couple of bars). Doji candles are skipped by walking
    back; if every candle in the window is a doji, the raw direction
    of the last candle is used (tie -> CALL, so a signal is always
    returned). Returns None only when the store has no 1m candles
    at all for the pair."""
    bars = store.get(pair, 1, 12)
    if not bars:
        return None
    ha_open = bars[0].open
    last_color = 0
    for c in bars:
        ha_close = (c.open + c.high + c.low + c.close) / 4.0
        color = 1 if ha_close > ha_open else (-1 if ha_close < ha_open else 0)
        if color != 0:
            last_color = color
        ha_open = (ha_open + ha_close) / 2.0
    if last_color != 0:
        return Direction.CALL if last_color == 1 else Direction.PUT
    last = bars[-1]
    return Direction.CALL if last.close >= last.open else Direction.PUT


def generate_now(engine, pair: str, now: datetime | None = None):
    """Generate one 1-minute signal for pair against the live engine.

    Returns (signal, source) with source "setup" or "quick_read", or
    None when the engine has no 1-minute candles yet for the pair
    (caller turns that into a 503). The returned signal is already
    registered with the engine's ResultChecker (same store + scoring
    path as automatic signals), so it appears in GET /api/signals
    and is scored exactly once. This function never calls
    engine.on_1m_bar() — the automatic evaluation is not re-run.
    """
    now = now or datetime.now(timezone.utc)
    store = engine.store
    last = store.last(pair, 1)
    if last is None:
        return None
    entry_time = next_minute_open(now)
    entry_price = last.close  # proxy for the next minute's open

    # Path 1: a real strategy setup, armed and waiting.
    for strat in engine.strategies:
        pend = strat.take_next_pending(pair, now)
        if pend is None:
            continue
        window = strat.session_window
        if window is not None and not (
            window[0] <= entry_time.hour <= window[1]
        ):
            continue  # out of session: finalize_pending() would drop it too
        sig = Signal(
            pair=pair,
            broker=engine.broker,
            strategy=strat.name,
            direction=pend["direction"],
            entry_time=entry_time,
            expiry_min=1,
            market=Market.REAL,
            entry_price=entry_price,
            status="test",
        )
        engine.emitted.append(sig)
        engine.checker.watch(sig)
        return sig, "setup"

    # Path 2: quick read — Heikin Ashi colour of the last 1m candle.
    direction = heikin_ashi_direction(store, pair)
    if direction is None:
        return None
    sig = Signal(
        pair=pair,
        broker=engine.broker,
        strategy="Momentum",
        direction=direction,
        entry_time=entry_time,
        expiry_min=1,
        market=Market.REAL,
        entry_price=entry_price,
        status="test",
    )
    engine.emitted.append(sig)
    engine.checker.watch(sig)
    return sig, "quick_read"
