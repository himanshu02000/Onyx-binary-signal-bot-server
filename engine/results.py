"""Result checker + signal store interface.

SCORING (same convention as every backtest): after a signal's expiry,
win/loss is marked from the actual chart outcome — expiry price is
the close of the 1-minute bar at entry + (expiry - 1) minutes.
CALL wins if expiry price > entry price, PUT wins if expiry price <
entry price. A TIE IS A LOSS (as binary brokers score it). A missing
expiry bar (weekend gap, end of data) = VOID, excluded from the win
rate.

STORAGE: the backend skeleton owns the real database (see
``DbSignalStore`` below — it wraps the ``insert_signal`` /
``update_signal_result`` functions in ~/workspace/signal-bot/server/db.py,
which the backend agent built in parallel). This module also ships a
local JSONL store so the engine runs and can be proven WITHOUT the
backend existing yet.

SCHEMA GAPS — RESOLVED Nov 24 by the build coordinator (were open items
from the engine build; fixed in supabase/schema.sql + db.py):
  1. ``signals.strategy`` CHECK now includes 'ha_fade'
     ('elite','turbo','prime','ha_fade') — HA-fade signals CAN be
     written to the db.
  2. ``signals.result`` CHECK now includes 'void'
     ('win','loss','pending','void'), and db.py's
     ``update_signal_result`` assert accepts 'void' too.
  3. db.py's ``pair`` example is 'EUR/USD' (with a slash); the engine
     uses 'EURUSD' internally and the adapter converts on write.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .candles import CandleStore
from .types import Candle, Direction, Signal


class SignalStore(Protocol):
    """What the backend must provide for the engine's signals.

    The real contract is db.py's ``insert_signal`` /
    ``update_signal_result`` + supabase/schema.sql ``signals`` table:
      id (db-assigned), pair TEXT ('EUR/USD' with a slash),
      broker TEXT ('quotex'), strategy TEXT ('elite'/'turbo'/'prime'/
      'ha_fade'),
      direction TEXT ('call'/'put', lowercase),
      entry TIMESTAMPTZ, expiry TEXT ('1 min'/'5 min'),
      market TEXT ('real' this phase), is_beta BOOL (True in beta),
      result TEXT ('win'/'loss'/'pending'/'void').
    No strategy-logic columns. Ever.
    """

    def save_signal(self, signal: Signal) -> None: ...
    def save_result(
        self, signal_id: str, result: str, expiry_price: float | None
    ) -> None: ...
    def pending_signals(self) -> list[dict]: ...


# Engine strategy name -> db strategy value.
_DB_STRATEGY = {"Elite": "elite", "Turbo": "turbo", "Prime": "prime", "HA-fade": "ha_fade"}


class DbSignalStore:
    """SignalStore backed by the backend's db.py (Supabase or the
    local SQLite fallback). Maps engine signals onto
    ``db.insert_signal`` / ``db.update_signal_result``.
    """

    def __init__(self, fallback: JsonFileSignalStore | None = None):
        import importlib.util
        import sys
        from pathlib import Path as _Path

        server_dir = _Path(__file__).resolve().parent.parent
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        spec = importlib.util.find_spec("db")
        if spec is None:
            raise RuntimeError(
                "DbSignalStore needs server/db.py on the path; "
                "use JsonFileSignalStore instead"
            )
        import db as _db

        self._db = _db
        self._engine_to_db_id: dict[str, int] = {}
        self.fallback = fallback
        self.skipped_ha_fade = 0

    def save_signal(self, signal: Signal) -> None:
        db_strategy = _DB_STRATEGY.get(signal.strategy)
        if db_strategy is None:
            self.skipped_ha_fade += 1
            if self.fallback is not None:
                self.fallback.save_signal(signal)
            return
        pair = f"{signal.pair[:3]}/{signal.pair[3:]}"  # EURUSD -> EUR/USD
        row = self._db.insert_signal(
            pair=pair,
            broker=signal.broker.value,
            strategy=db_strategy,
            direction=signal.direction.value.lower(),
            entry=signal.entry_time.isoformat(),
            expiry=f"{signal.expiry_min} min",
            market=signal.market.value,
            sent_at=signal.created_at.isoformat(),
            is_beta=True,
        )
        self._engine_to_db_id[signal.id] = row["id"]

    def save_result(
        self, signal_id: str, result: str, expiry_price: float | None
    ) -> None:
        db_id = self._engine_to_db_id.get(signal_id)
        if db_id is None:
            if self.fallback is not None:
                self.fallback.save_result(signal_id, result, expiry_price)
            return
        self._db.update_signal_result(db_id, result.lower())

    def pending_signals(self) -> list[dict]:
        return []  # the runner tracks pending; db is write-only here


class JsonFileSignalStore:
    """Local JSONL outbox: data/signals.jsonl + data/results.jsonl.

    Append-only, human-readable, enough to prove the engine end to
    end with no database. NOT the production store.
    """

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.signals_path = self.data_dir / "signals.jsonl"
        self.results_path = self.data_dir / "results.jsonl"

    def save_signal(self, signal: Signal) -> None:
        with open(self.signals_path, "a") as fh:
            fh.write(json.dumps(signal.to_public_dict()) + "\n")

    def save_result(
        self, signal_id: str, result: str, expiry_price: float | None
    ) -> None:
        with open(self.results_path, "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "id": signal_id,
                        "result": result,
                        "expiry_price": expiry_price,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                + "\n"
            )

    def pending_signals(self) -> list[dict]:
        return []  # local store is append-only; the runner tracks pending

    def summarize(self) -> dict:
        """Win rates per strategy from the local files."""
        results = {}
        if self.results_path.exists():
            for line in open(self.results_path):
                r = json.loads(line)
                results[r["id"]] = r["result"]
        by_strategy: dict[str, dict[str, int]] = {}
        if self.signals_path.exists():
            for line in open(self.signals_path):
                s = json.loads(line)
                res = results.get(s["id"])
                if res not in ("WIN", "LOSS"):
                    continue
                bucket = by_strategy.setdefault(
                    s["strategy"], {"trades": 0, "wins": 0}
                )
                bucket["trades"] += 1
                if res == "WIN":
                    bucket["wins"] += 1
        return {
            name: {
                "trades": v["trades"],
                "wins": v["wins"],
                "win_rate_pct": (
                    round(100 * v["wins"] / v["trades"], 2)
                    if v["trades"]
                    else None
                ),
            }
            for name, v in sorted(by_strategy.items())
        }


def score_signal(signal: Signal, store: CandleStore) -> str:
    """Score ONE signal against the 1-minute closes in the store.

    Returns "WIN" / "LOSS" / "VOID" (missing expiry bar) / "PENDING"
    (expiry bar not in the store yet — live mode, not expired).
    """
    if signal.entry_price is None:
        return "PENDING"
    target = signal.entry_time + timedelta(minutes=signal.expiry_min - 1)
    exp_price = store.m1_close_at(signal.pair, target)
    if exp_price is None:
        # Is the expiry time already past? If the store's newest 1m
        # bar for this pair is beyond the target, the bar is missing
        # for good (gap) -> VOID. Otherwise still waiting -> PENDING.
        last = store.last(signal.pair, 1)
        if last is not None and last.start > target:
            return "VOID"
        return "PENDING"
    if signal.direction == Direction.CALL:
        win = exp_price > signal.entry_price
    else:
        win = exp_price < signal.entry_price
    return "WIN" if win else "LOSS"


def expiry_price_of(signal: Signal, store: CandleStore) -> float | None:
    target = signal.entry_time + timedelta(minutes=signal.expiry_min - 1)
    return store.m1_close_at(signal.pair, target)


class ResultChecker:
    """Tracks pending signals and scores them the moment their expiry
    1-minute bar arrives. Call ``on_1m_bar`` for every 1m close."""

    def __init__(self, store: SignalStore | None = None):
        self._pending: list[Signal] = []
        self.sink = store

    def watch(self, signal: Signal) -> None:
        self._pending.append(signal)
        if self.sink is not None:
            self.sink.save_signal(signal)

    def on_1m_bar(self, store: CandleStore, bar: Candle) -> list[tuple[Signal, str]]:
        """Score any pending signal whose expiry bar is this bar (or
        is now provably missing). Returns [(signal, result)] scored."""
        scored: list[tuple[Signal, str]] = []
        remaining: list[Signal] = []
        for sig in self._pending:
            if sig.pair != bar.pair:
                remaining.append(sig)
                continue
            result = score_signal(sig, store)
            if result == "PENDING":
                remaining.append(sig)
            else:
                scored.append((sig, result))
                if self.sink is not None:
                    self.sink.save_result(
                        sig.id, result, expiry_price_of(sig, store)
                    )
        self._pending = remaining
        return scored

    def flush_unscored_as_void(self) -> list[tuple[Signal, str]]:
        """End of replay: anything still pending has no expiry bar in
        the data (backtest convention: VOID, excluded from win rate)."""
        out = [(sig, "VOID") for sig in self._pending]
        if self.sink is not None:
            for sig, _ in out:
                self.sink.save_result(sig.id, "VOID", None)
        self._pending = []
        return out
