"""Seed local-dev sample data (SQLite fallback only — never run against
production data). Inserts a handful of BETA real-market signals so the
API can be exercised end to end before the engine writes real ones.

Usage:  python seed.py [--reset]
  --reset  wipes existing signals first.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")

import db  # noqa: E402

SAMPLE_SIGNALS = [
    # (pair, broker, strategy, direction, entry_offset_min, expiry, result)
    ("EUR/USD", "quotex", "elite", "call", -120, "5 min", "win"),
    ("GBP/USD", "quotex", "turbo", "put", -90, "1 min", "loss"),
    ("EUR/JPY", "olymp_trade", "prime", "call", -60, "5 min", "win"),
    ("USD/JPY", "quotex", "elite", "put", -45, "15 min", "pending"),
    ("AUD/USD", "pocket_option", "turbo", "call", -30, "1 min", "pending"),
    ("EUR/USD", "quotex", "prime", "put", -15, "5 min", "pending"),
    ("GBP/JPY", "olymp_trade", "elite", "call", -5, "5 min", "pending"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    print(f"backend: {db.backend()}")
    if db.backend() != "sqlite":
        print("seed.py is for the local SQLite fallback only. Refusing to seed Supabase.")
        raise SystemExit(1)
    db.init_db()
    if args.reset:
        db.get_sqlite().execute("DELETE FROM signals")
        db.get_sqlite().commit()
        print("cleared existing signals")
    now = datetime.now(timezone.utc)
    for pair, broker, strategy, direction, offset, expiry, result in SAMPLE_SIGNALS:
        entry = now + timedelta(minutes=offset)
        row = db.insert_signal(
            pair=pair, broker=broker, strategy=strategy, direction=direction,
            entry=entry.isoformat(), expiry=expiry, market="real",
            sent_at=entry.isoformat(), is_beta=True,
        )
        if result != "pending":
            db.update_signal_result(row["id"], result)
        print(f"  #{row['id']} {strategy} {pair} {direction} {expiry} -> {result} (real, beta)")
    print(f"seeded {len(SAMPLE_SIGNALS)} beta real-market signals (OTC: none — real only, Nov 24 scope)")


if __name__ == "__main__":
    main()
