"""ONYX signal engine (server-side only).

BETA MODE: signals are written with status="test"/"demo" only.
No signal is delivered to any user. Every strategy tested so far
loses vs the 53.2% breakeven — this engine is a test harness until
a strategy proves it wins.
"""

__version__ = "0.1.0-beta"

from .types import REAL_PAIRS, Candle, Market, Signal  # noqa: F401
