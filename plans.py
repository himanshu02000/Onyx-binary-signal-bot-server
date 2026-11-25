"""Onyx — free / premium rules (single source of truth).

From the free build plan (p. 8) and the content pack (pp. 6, 8, 11):

    Free    — max 5 signals/day, 1 strategy sample, locked entry/expiry
              beyond the sample, ads ON, results = today only.
    Premium — unlimited signals, all 3 strategies (Elite, Turbo, Prime),
              full entry/expiry, no ads, full results history.
              1.97 USDT / month, 30 days from approval, auto-expires
              (premium is an expiry DATE, see db.py).

SCOPE (Nov 24, owner decision): REAL PAIRS ONLY for now. OTC is
"coming soon" — OTC signals are never served in this phase, for free
or premium. OTC_ENABLED stays False until the owner flips it.

Beta mode: every signal served is beta/test data until a strategy
proves it wins. No accuracy number is claimed anywhere.

Reconciling the two source docs (noted, not hidden): the plan says
free = "up to 5 signals a day"; the content pack (p. 6) says free =
"1-2 sample signals" with full details locked. This module implements
both: free users SEE up to 5 signals a day, but only the FIRST one
(the sample, 1 strategy) shows full entry/expiry — signals 2-5 show
pair / direction / strategy / broker only, with entry/expiry locked.
Signals beyond 5 are not served at all to free users.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

PREMIUM_PRICE_USDT = 1.97
PREMIUM_DAYS = 30
FREE_DAILY_LIMIT = 10  # owner's call Nov 24: free = 10 signals/day (for now)
FREE_SAMPLE_UNLOCKED = 1  # only the first (newest) signal is fully visible to free users
STRATEGIES = ("elite", "turbo", "prime")
BROKERS = ("quotex", "olymp_trade", "pocket_option", "finnhub")  # finnhub = test-phase live feed only (Nov 25)
NETWORKS = ("BSC", "Tron")

# Scope flag — Nov 24: real pairs only, OTC coming soon.
OTC_ENABLED = False
OTC_COMING_SOON_MESSAGE = "OTC signals are coming soon. Real-market signals only for now."

BETA = True  # all served signals are beta/test data until a strategy proves it wins


def is_premium_active(premium_until) -> bool:
    """Premium is an expiry DATE: active only while premium_until >= today."""
    if not premium_until:
        return False
    if isinstance(premium_until, datetime):
        premium_until = premium_until.date()
    elif isinstance(premium_until, str):
        premium_until = date.fromisoformat(premium_until[:10])
    return premium_until >= datetime.now(timezone.utc).date()


def apply_plan_to_signals(signals: list[dict], plan: str) -> dict:
    """Shape a newest-first signal list for the caller's plan.

    Premium: everything, full details, no ads.
    Free:    at most FREE_DAILY_LIMIT signals; only the first is fully
             visible; the rest have entry/expiry locked (None + locked=True).
    Both:    real-market only (OTC never reaches here), beta flag on every row.
    Returns {"signals": [...], "meta": {...}}.
    """
    plan = "premium" if plan == "premium" else "free"
    if plan == "premium":
        shaped = [dict(s, locked=False, is_beta=BETA) for s in signals]
        return {
            "signals": shaped,
            "meta": {
                "plan": "premium",
                "daily_limit": None,
                "shown": len(shaped),
                "locked_count": 0,
                "ads": False,
                "beta": BETA,
                "market": "real",
                "otc_enabled": OTC_ENABLED,
                "otc_note": OTC_COMING_SOON_MESSAGE,
            },
        }

    visible = signals[:FREE_DAILY_LIMIT]
    shaped = []
    for i, s in enumerate(visible):
        row = dict(s, is_beta=BETA)
        if i < FREE_SAMPLE_UNLOCKED:
            row["locked"] = False
        else:
            # Locked: pair/direction/strategy/broker visible, entry/expiry hidden.
            row["locked"] = True
            row["entry"] = None
            row["expiry"] = None
        shaped.append(row)
    return {
        "signals": shaped,
        "meta": {
            "plan": "free",
            "daily_limit": FREE_DAILY_LIMIT,
            "shown": len(shaped),
            "locked_count": sum(1 for r in shaped if r["locked"]),
            "ads": True,
            "beta": BETA,
            "market": "real",
            "otc_enabled": OTC_ENABLED,
            "otc_note": OTC_COMING_SOON_MESSAGE,
        },
    }


def results_scope(plan: str) -> dict:
    """Free sees today only; premium sees full history."""
    if plan == "premium":
        return {"since": None, "scope": "full_history"}
    today = datetime.now(timezone.utc).date().isoformat()
    return {"since": today, "scope": "today_only"}
