"""Server-side strategies — Elite, Prime, HA-fade, Turbo.

SECRET: the rules below exist ONLY in this file. Signals carry only
pair / direction / entry / expiry / strategy / broker (see types.py).
Never import anything from here into the app or into a signal payload.

FAITHFULNESS: Elite, Prime and HA-fade are mechanical ports of the
backtest scripts in ~/workspace/signal-bot/backtest/ (same rules,
same assumptions A1-A11 / A1-A7, same session window 00:00-21:00 UTC,
same expiry convention — expiry price is the close of the 1-minute
bar at entry + (expiry - 1) minutes, tie = loss). Test mode replays
the same Histdata files so the win rates can be compared against the
published reports.

BETA / HONESTY: every strategy tested so far LOSES vs the 53.2%
breakeven at 88% payout (best ~51%). Turbo was NEVER backtested.
Nothing here is a winning strategy until a test proves otherwise.

LIVE TIMING: a signal is armed when a candle closes and is FINALISED
on the first 1-minute bar of the entry candle (entry price = that
bar's open). That is the moment the signal goes out live — at the
entry candle's open, not at its close.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .candles import CandleStore
from .types import (
    SESSION_END_HOUR,
    SESSION_START_HOUR,
    Broker,
    Candle,
    Direction,
    Market,
    Signal,
)

BROKER = Broker.QUOTEX  # live source; test mode overrides per-signal below


class BaseStrategy:
    """Pending-entry handling shared by Elite / Prime / HA-fade.

    A setup fires at a candle close, but the trade is entered at the
    OPEN of the next candle. The strategy arms a pending entry; the
    runner finalises it on the first 1-minute bar of the entry candle.
    If that bar never arrives at exactly the expected time (weekend /
    feed gap), the pending entry is voided — never guessed.
    """

    name = "base"
    # Session window (UTC hours, inclusive) applied at ENTRY time.
    # Elite and HA-fade backtests use 00:00-21:00; Prime's backtest
    # has NO session filter, so PrimeStrategy sets this to None.
    session_window: tuple[int, int] | None = (
        SESSION_START_HOUR, SESSION_END_HOUR)

    def __init__(self):
        self._pending: list[dict] = []

    def arm_entry(
        self, pair: str, direction: Direction, entry_time: datetime, expiry_min: int
    ) -> None:
        self._pending.append(
            {
                "pair": pair,
                "direction": direction,
                "entry_time": entry_time,
                "expiry_min": expiry_min,
            }
        )

    def finalize_pending(self, store: CandleStore, bar_1m: Candle) -> list[Signal]:
        out: list[Signal] = []
        remaining: list[dict] = []
        for p in self._pending:
            if p["pair"] != bar_1m.pair:
                remaining.append(p)
                continue
            if bar_1m.start == p["entry_time"]:
                if (
                    self.session_window is None
                    or self.session_window[0]
                    <= p["entry_time"].hour
                    <= self.session_window[1]
                ):
                    out.append(
                        self._emit(
                            p["pair"],
                            p["direction"],
                            p["entry_time"],
                            p["expiry_min"],
                            entry_price=bar_1m.open,
                        )
                    )
                # else: session-skipped, dropped silently (backtest A10)
            elif bar_1m.start < p["entry_time"]:
                remaining.append(p)  # entry candle not open yet
            # else: entry candle never arrived on time -> VOID, dropped
        self._pending = remaining
        return out

    def take_next_pending(self, pair: str, now: datetime) -> dict | None:
        """Pop and return the earliest pending entry for pair with
        entry_time in the future (entry_time > now), else None.

        Used ONLY by on-demand generate: consuming the entry stops
        the automatic engine finalising the same setup again on the
        next 1-minute bar (no double-emit). Entries whose entry time
        already passed are left alone — the engine's finalize_pending()
        voids them on the next bar, as before."""
        future = [
            p for p in self._pending
            if p["pair"] == pair and p["entry_time"] > now
        ]
        if not future:
            return None
        nxt = min(future, key=lambda p: p["entry_time"])
        self._pending.remove(nxt)
        return nxt

    def _emit(
        self,
        pair: str,
        direction: Direction,
        entry_time: datetime,
        expiry_min: int,
        entry_price: float | None = None,
    ) -> Signal:
        return Signal(
            pair=pair,
            broker=BROKER,
            strategy=self.name,
            direction=direction,
            entry_time=entry_time,
            expiry_min=expiry_min,
            market=Market.REAL,
            entry_price=entry_price,
            status="test",
        )

    # Hooks the runner calls. Subclasses override what they need.
    def on_closed_candle(self, store: CandleStore, candle: Candle) -> list[Signal]:
        return []

    def on_1m_bar(self, store: CandleStore, bar: Candle) -> list[Signal]:
        return self.finalize_pending(store, bar)


# ----------------------------------------------------------------------
# ELITE — sweep + last FVG / order block (owner's strategy)
# Port of backtest_elite_binary.py (assumptions A1-A11).
# ----------------------------------------------------------------------
class EliteStrategy(BaseStrategy):
    """Elite: higher-TF sweep, then last FVG in the sweep leg, entry on
    retracement into the zone. Default trigger is OVERLAP (the genuine
    'price enters the zone' test); LITERAL is the backtest's primary
    variant — note the backtest found LITERAL never actually waits for
    a retracement (it fires on the first waiting candle)."""

    name = "Elite"
    TF_PAIRS = [(5, 1), (15, 2), (30, 3), (60, 5)]
    TOUCH_WINDOW = 10  # lower-TF candles after the sweep (A9)

    def __init__(self, trigger: str = "overlap"):
        super().__init__()
        assert trigger in ("overlap", "literal")
        self.trigger = trigger
        self._setups: list[dict] = []  # armed, waiting for a touch

    def on_closed_candle(self, store: CandleStore, candle: Candle) -> list[Signal]:
        htfs = {h for h, _ in self.TF_PAIRS}
        ltfs = {l for _, l in self.TF_PAIRS}
        if candle.tf_min in htfs:
            self._on_htf_close(store, candle)
        if candle.tf_min in ltfs:
            self._on_ltf_close(store, candle)
        return []

    def _on_htf_close(self, store: CandleStore, candle: Candle) -> None:
        htf = candle.tf_min
        prev = store.get(candle.pair, htf, 2)
        if len(prev) < 2:
            return
        prev_c = prev[-2]
        # A2: previous higher-TF candle must be time-adjacent.
        if candle.start - prev_c.start != timedelta(minutes=htf):
            return
        put_sweep = candle.high > prev_c.high and candle.close < prev_c.high
        call_sweep = candle.low < prev_c.low and candle.close > prev_c.low
        if not (put_sweep or call_sweep):
            return
        for ltf in [l for h, l in self.TF_PAIRS if h == htf]:
            # The sweep leg's LTF candles are those with start in
            # [sweep_open, sweep_close). Take one extra candidate and
            # filter by time: the 1m bar that triggered this HTF close
            # is already in the store and belongs to the NEXT bucket,
            # and a straddling LTF candle (e.g. a 2-min bar over a
            # 15-min boundary) belongs to neither leg nor wait (A3).
            leg = [
                c
                for c in store.get(candle.pair, ltf, htf // ltf + 1)
                if candle.start <= c.start < candle.end
            ]
            if len(leg) < 3:
                continue
            for direction in ("PUT", "CALL"):
                if direction == "PUT" and not put_sweep:
                    continue
                if direction == "CALL" and not call_sweep:
                    continue
                zone = self._last_fvg(leg, ltf, direction)
                if zone is None:
                    continue
                self._setups.append(
                    {
                        "pair": candle.pair,
                        "ltf": ltf,
                        "direction": Direction(direction),
                        "zone_lo": zone[0],
                        "zone_hi": zone[1],
                        "wait_start": candle.end,  # = sweep close
                        "waits_seen": 0,
                    }
                )

    @staticmethod
    def _last_fvg(
        leg: list[Candle], ltf: int, direction: str
    ) -> tuple[float, float] | None:
        """Last 3-candle FVG fully inside the sweep leg (A3/A4).

        PUT (bearish):  high[k-2] < low[k], zone [high[k-2], low[k]].
        CALL (bullish): low[k-2] > high[k], zone [high[k], low[k-2]].
        Triples must be time-consecutive; the last one wins.
        """
        zone = None
        for k in range(2, len(leg)):
            a, _, c = leg[k - 2], leg[k - 1], leg[k]
            if (c.start - a.start) != timedelta(minutes=2 * ltf):
                continue
            if direction == "PUT" and a.high < c.low:
                zone = (a.high, c.low)
            elif direction == "CALL" and a.low > c.high:
                zone = (c.high, a.low)
        return zone

    def _on_ltf_close(self, store: CandleStore, candle: Candle) -> None:
        remaining: list[dict] = []
        for s in self._setups:
            if s["pair"] != candle.pair or s["ltf"] != candle.tf_min:
                remaining.append(s)
                continue
            if candle.start < s["wait_start"]:
                remaining.append(s)
                continue
            s["waits_seen"] += 1
            if s["waits_seen"] > self.TOUCH_WINDOW:
                continue  # expired untouched (A9)
            zlo, zhi = s["zone_lo"], s["zone_hi"]
            # A6: invalidation takes precedence over the trigger.
            if s["direction"] == Direction.PUT:
                invalid = candle.close > zhi
                trigger = (
                    (candle.high >= zlo and candle.low <= zhi)
                    if self.trigger == "overlap"
                    else (candle.low <= zhi)
                )
            else:
                invalid = candle.close < zlo
                trigger = (
                    (candle.high >= zlo and candle.low <= zhi)
                    if self.trigger == "overlap"
                    else (candle.high >= zlo)
                )
            if invalid:
                continue  # setup dead, no trade
            if trigger:
                self.arm_entry(
                    s["pair"],
                    s["direction"],
                    candle.end,  # entry at open of next LTF candle
                    s["ltf"],  # expiry = lower-TF duration
                )
                continue  # first touch only
            remaining.append(s)
        self._setups = remaining


# ----------------------------------------------------------------------
# PRIME — DT Concept pullback (Prince Jat). REAL market only.
# Port of backtest_pullback.py (assumptions A1-A7), 5m chart.
# ----------------------------------------------------------------------
class PrimeStrategy(BaseStrategy):
    """Prime: trend from market structure (HH+HL / LL+LH, fractal
    window 2), largest pullback body-to-body projected forward from
    the swing extreme, enter with the trend at the zone. Same zone can
    give several entries (one per touch event).

    LOOKAHEAD NOTE (found Nov 24 2026, verified): the published
    backtest's 49.07% embeds a 2-bar fractal lookahead — a swing at
    the touch candle is only confirmed by candles i+1/i+2, which the
    backtest already had. A live engine cannot know that. This port
    uses only swings confirmed by data up to the touch candle, and
    reproduces an independent no-lookahead re-run of the backtest
    code almost exactly (engine 44.73% / 883 trades vs re-run
    44.78% / 882 trades). The 49.07% is therefore NOT the number a
    live Prime can expect; 44.7% is the honest live-faithful figure.
    No session filter (the Prime backtest has none)."""

    name = "Prime"
    CHART_TF = 5
    LOOKBACK = 120  # max candles back for the trend leg (5m)
    ATR_LEN = 14
    ATR_MULT = 1.0  # pullback must be >= 1.0 x ATR(14)
    WARMUP = 60
    # NO session filter: the Prime backtest (backtest_pullback.py) has
    # none — entries at 22:00-23:00 UTC count. (Elite/HA-fade do filter.)
    session_window: tuple[int, int] | None = None

    def __init__(self, expiry_min: int = 5):
        super().__init__()
        self.expiry_min = expiry_min
        self._in_touch: dict[str, bool] = {}

    def on_closed_candle(self, store: CandleStore, candle: Candle) -> list[Signal]:
        if candle.tf_min != self.CHART_TF:
            return []
        pair = candle.pair
        df = store.get(pair, self.CHART_TF, 300)
        if len(df) < self.WARMUP:
            return []
        i = len(df) - 1
        close = [c.close for c in df]
        high = [c.high for c in df]
        low = [c.low for c in df]
        sh, sl = self._swings(high, low)
        sh_before = [j for j in sh if j <= i]
        sl_before = [j for j in sl if j <= i]
        if len(sh_before) < 2 or len(sl_before) < 2:
            self._in_touch[pair] = False
            return []
        sh2, sh1 = sh_before[-1], sh_before[-2]
        sl2, sl1 = sl_before[-1], sl_before[-2]
        # A1: trend from market structure.
        up = close[sh2] > close[sh1] and close[sl2] > close[sl1]
        down = close[sh2] < close[sh1] and close[sl2] < close[sl1]
        if not (up or down):
            self._in_touch[pair] = False
            return []
        # A2: largest pullback (closes) over the trend leg.
        leg_start = sl1 if up else sh1
        lo = max(leg_start, i - self.LOOKBACK)
        seg = close[lo : i + 1]
        if len(seg) < 5:
            self._in_touch[pair] = False
            return []
        if up:
            peak = seg[0]
            p = 0.0
            for v in seg:
                peak = max(peak, v)
                p = max(p, peak - v)
        else:
            trough = seg[0]
            p = 0.0
            for v in seg:
                trough = min(trough, v)
                p = max(p, v - trough)
        atr = self._atr(df)
        # A4: only meaningful pullbacks count.
        if atr is None or p < self.ATR_MULT * atr:
            self._in_touch[pair] = False
            return []
        # A3: entry zone.
        if up:
            zone = close[sh2] - p
            touched = low[i] <= zone
            valid = close[i] >= close[sl1]  # A6
            direction = Direction.CALL
        else:
            zone = close[sl2] + p
            touched = high[i] >= zone
            valid = close[i] <= close[sh1]  # A6
            direction = Direction.PUT
        # A5: consecutive touching candles = ONE touch event.
        if touched and valid and not self._in_touch.get(pair, False):
            self.arm_entry(pair, direction, candle.end, self.expiry_min)
            self._in_touch[pair] = True
        elif not touched:
            self._in_touch[pair] = False
        return []

    @staticmethod
    def _swings(high: list[float], low: list[float]):
        """Fractal swing highs/lows, window 2 (same as backtest)."""
        n = len(high)
        sh, sl = [], []
        w = 2
        for k in range(w, n - w):
            if (
                high[k] > high[k - 1]
                and high[k] > high[k + 1]
                and high[k] >= high[k - w]
                and high[k] >= high[k + w]
            ):
                sh.append(k)
            if (
                low[k] < low[k - 1]
                and low[k] < low[k + 1]
                and low[k] <= low[k - w]
                and low[k] <= low[k + w]
            ):
                sl.append(k)
        return sh, sl

    def _atr(self, df: list[Candle]) -> float | None:
        trs = []
        for k, c in enumerate(df):
            prev_close = df[k - 1].close if k > 0 else c.close
            trs.append(
                max(
                    c.high - c.low,
                    abs(c.high - prev_close),
                    abs(c.low - prev_close),
                )
            )
        if len(trs) < self.ATR_LEN:
            return None
        return sum(trs[-self.ATR_LEN :]) / self.ATR_LEN


# ----------------------------------------------------------------------
# HA-FADE — Heikin Ashi fade (owner's strategy).
# Port of backtest_heikinashi_fade.py (assumptions A1-A7).
# ----------------------------------------------------------------------
class HAFadeStrategy(BaseStrategy):
    """HA-fade: two consecutive same-colour Heikin Ashi candles ->
    trade the OPPOSITE direction on the next candle (two greens ->
    PUT, two reds -> CALL). Entry at the open of the third candle,
    expiry one candle."""

    name = "HA-fade"
    WARMUP = 50

    def __init__(self, tfs: list[int] | None = None):
        super().__init__()
        # Default 15/30/60m: the 1m variant fires ~700 signals/day and
        # wins least (forex-only 47.5%); 30m/1h are the best tested
        # (forex-only ~51%). Configurable, but defaults stay sane.
        self.tfs = tfs or [15, 30, 60]
        self._state: dict[tuple[str, int], dict] = {}

    def on_closed_candle(self, store: CandleStore, candle: Candle) -> list[Signal]:
        if candle.tf_min not in self.tfs:
            return []
        tf = candle.tf_min
        key = (candle.pair, tf)
        st = self._state.setdefault(
            key, {"ha_open": None, "ha_close": None, "color": 0,
                  "start": None, "count": 0}
        )
        ha_close = (candle.open + candle.high + candle.low + candle.close) / 4.0
        ha_open = candle.open if st["ha_open"] is None else (
            st["ha_open"] + st["ha_close"]
        ) / 2.0
        color = 1 if ha_close > ha_open else (-1 if ha_close < ha_open else 0)
        st["count"] += 1
        prev_color, prev_start = st["color"], st["start"]
        st.update({"ha_open": ha_open, "ha_close": ha_close,
                   "color": color, "start": candle.start})
        if st["count"] <= self.WARMUP:
            return []
        # A3: the two signal candles must be exactly tf apart.
        if prev_start is None or candle.start - prev_start != timedelta(minutes=tf):
            return []
        if prev_color == 0 or color == 0 or prev_color != color:
            return []  # doji breaks the sequence (A1)
        direction = Direction.PUT if color == 1 else Direction.CALL
        self.arm_entry(candle.pair, direction, candle.end, tf)
        return []


# ----------------------------------------------------------------------
# TURBO — *** UNPROVEN / NEVER BACKTESTED ***
# Documented conservative interpretation of the owner's description
# (from a TWL video): 1-min candles, trade the NEXT candle (1-min
# expiry); buyers/sellers at a level + a gap at the next open
# ("excitement gap") = the next candle follows that direction; skip
# 50-50s and level-breaking gaps; entry in the last seconds of the
# current candle.
#
# MECHANICAL READING USED HERE (an interpretation, not a test):
#  * Level = a recent fractal swing high/low (window 2, last 30 1m
#    candles). The previous candle must have bounced off it: its low
#    within 0.5x ATR(14) of a swing low (CALL) or its high within
#    0.5x ATR of a swing high (PUT).
#  * Conviction: the previous candle's body must be >= 0.5x ATR(14)
#    in the trade direction. Weak / doji candles are 50-50s: skipped.
#  * Excitement gap: the new candle opens beyond the previous close
#    in the same direction by >= 0.3x ATR(14). A tiny gap is a 50-50:
#    skipped. A gap that jumps THROUGH the nearest swing extreme on
#    the other side is a level-breaking gap: skipped.
#  * Entry = the open of the gap candle (the gap is only known when
#    that candle opens, so "last seconds of the current candle" is
#    implemented as "at the open of the next candle").
#  * Expiry 1 minute. Real market only, like everything this phase.
#
# This strategy is DISABLED by default (ENABLED_BY_DEFAULT = False)
# and the runner only loads it when explicitly asked. It must be
# backtested on stored candles before it is ever trusted.
# ----------------------------------------------------------------------
class TurboStrategy(BaseStrategy):
    name = "Turbo"
    ENABLED_BY_DEFAULT = False
    LOOKBACK = 30
    ATR_LEN = 14
    BODY_MULT = 0.5   # min body, in ATRs, for conviction
    GAP_MULT = 0.3    # min gap, in ATRs, for an "excitement gap"
    LEVEL_TOL_MULT = 0.5  # how close to the level the bounce must be

    def on_1m_bar(self, store: CandleStore, bar: Candle) -> list[Signal]:
        out = self.finalize_pending(store, bar)  # no-op for Turbo
        prev_list = store.get(bar.pair, 1, 2)
        if len(prev_list) < 2:
            return out
        prev = prev_list[0]  # last closed 1m candle before this bar
        if prev.start >= bar.start:
            return out
        hist = store.get(bar.pair, 1, self.LOOKBACK + 1)
        if len(hist) < 15:
            return out
        atr = self._atr(hist)
        if atr is None or atr <= 0:
            return out
        gap = bar.open - prev.close
        # Recent swing levels (fractal window 2) from candles before bar.
        sh, sl = PrimeStrategy._swings(
            [c.high for c in hist[:-1]], [c.low for c in hist[:-1]]
        )
        swing_highs = [hist[j].high for j in sh]
        swing_lows = [hist[j].low for j in sl]
        if not swing_highs or not swing_lows:
            return out
        tol = self.LEVEL_TOL_MULT * atr
        if (
            prev.is_bullish
            and prev.body >= self.BODY_MULT * atr
            and swing_lows
            and abs(prev.low - max(swing_lows)) <= tol
            and gap >= self.GAP_MULT * atr
            and bar.open <= max(swing_highs)  # skip level-breaking gaps
        ):
            out.append(
                self._emit(bar.pair, Direction.CALL, bar.start, 1,
                           entry_price=bar.open)
            )
        elif (
            prev.is_bearish
            and prev.body >= self.BODY_MULT * atr
            and swing_highs
            and abs(prev.high - min(swing_highs)) <= tol
            and gap <= -self.GAP_MULT * atr
            and bar.open >= min(swing_lows)  # skip level-breaking gaps
        ):
            out.append(
                self._emit(bar.pair, Direction.PUT, bar.start, 1,
                           entry_price=bar.open)
            )
        return out

    @staticmethod
    def _atr(df: list[Candle]) -> float | None:
        trs = []
        for k, c in enumerate(df):
            prev_close = df[k - 1].close if k > 0 else c.close
            trs.append(
                max(c.high - c.low, abs(c.high - prev_close),
                    abs(c.low - prev_close))
            )
        if len(trs) < 14:
            return None
        return sum(trs[-14:]) / 14


def build_strategies(
    names: list[str] | None = None,
    elite_trigger: str = "overlap",
    prime_expiry_min: int = 5,
    ha_tfs: list[int] | None = None,
) -> list[BaseStrategy]:
    """Build the strategy set. Default: Elite + Prime + HA-fade.
    Turbo is NOT in the default set — it is unproven (see above)."""
    wanted = names or ["elite", "prime", "ha_fade"]
    out: list[BaseStrategy] = []
    if "elite" in wanted:
        out.append(EliteStrategy(trigger=elite_trigger))
    if "prime" in wanted:
        out.append(PrimeStrategy(expiry_min=prime_expiry_min))
    if "ha_fade" in wanted:
        out.append(HAFadeStrategy(tfs=ha_tfs))
    if "turbo" in wanted:
        out.append(TurboStrategy())
    return out
