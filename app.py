"""Onyx Binary Signal Bot — backend skeleton (FastAPI).

Why FastAPI: async-ready for the live candle stream the engine will add,
automatic request validation, and the same Python the engine uses.
(Flask would work too; FastAPI is the better fit here, so no justification
for Flask is needed.)

Endpoints (all JSON):
    GET  /health                         {"status": "ok", ...}
    GET  /api/signals                    real-market signals, newest first
    GET  /api/results                    past signals win/loss/pending
    GET  /api/premium/status?user_id=    plan + premium expiry
    GET  /api/premium/info               price, networks, wallets, checkout on/off
    POST /api/users                      register / refresh a Telegram user
    POST /api/payments/txid              submit a TXID (stored as pending)
    POST /api/payments/verify            admin: approve/reject -> premium 30 days
    POST /api/payments/check             admin: run the optional auto TXID check (advisory only)
    POST /telegram/webhook                Telegram bot webhook (/start + /help; 503 if no bot token)

SCOPE (Nov 24, owner decision): REAL PAIRS ONLY for now; OTC is
"coming soon". Every signal endpoint serves market='real' only —
an `market=otc` request returns an empty list with an otc_note.
The engine writes real-market signals only in this phase.

Beta mode: every served signal carries is_beta=true. No accuracy
claim is made anywhere. Premium checkout is DISABLED by default
(checkout_enabled=false) until the owner supplies real wallet
addresses and flips it on — the verify endpoint exists, but no
premium activation goes live without the owner's approval.

Admin auth (for now): a simple ADMIN_KEY env var, sent as
`X-Admin-Key` header. Documented limitation: real admin auth
(proper accounts / roles) must replace this before launch.

Config: entirely via environment variables — see .env.example.
No secret is ever in code. CORS is open to GitHub Pages and the
Telegram Mini App origins (see ALLOWED_ORIGINS below).

Run locally:  uvicorn app:app --reload --port 8000   (from server/)
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import db
import plans
import telegram_bot
import txid_check
from db import backend as db_backend

ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()

ALLOWED_ORIGINS = [
    "https://himanshu02000.github.io",  # GitHub Pages frontend (same pattern as Captcha Earn)
    "https://web.telegram.org",       # Telegram Web / Mini App
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
_extra = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
ALLOWED_ORIGINS = list(dict.fromkeys(ALLOWED_ORIGINS + _extra))

app = FastAPI(title="Onyx Binary Signal Bot — API", version="0.1.0-beta")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    if not telegram_bot.token_configured():
        print("WARNING: TELEGRAM_BOT_TOKEN not set — /telegram/webhook returns 503.")
    elif not telegram_bot.webhook_secret():
        print("WARNING: TELEGRAM_WEBHOOK_SECRET not set — webhook accepts any POST "
              "(set it in Render and in setWebhook for production).")


@app.on_event("startup")
async def _start_signal_engine() -> None:
    """Start the Finnhub live-feed engine as a background task (test feed).

    Only when FINNHUB_API_KEY is set. A failed import or a feed error is
    logged and never stops the API — signals simply stay empty, as before.
    On Render free the web service sleeps when idle, so the engine pauses
    with it; traffic (API calls / Telegram webhook) wakes it again.
    """
    if not os.environ.get("FINNHUB_API_KEY", "").strip():
        print("Signal engine idle: FINNHUB_API_KEY not set — no live signals.")
        return
    try:
        from types import SimpleNamespace

        from engine.run import run_finnhub
        from engine.types import REAL_PAIRS
    except Exception as exc:  # engine import must never kill the API
        print(f"Signal engine NOT started (import failed: {exc}).")
        return
    args = SimpleNamespace(
        pairs=",".join(REAL_PAIRS),
        strategies="elite,prime,ha_fade",
        elite_trigger="overlap",
        prime_expiry=5,
        ha_tfs="15,30,60",
        data_dir="data",
    )
    task = asyncio.create_task(run_finnhub(args))
    task.add_done_callback(
        lambda t: print(f"Signal engine stopped: {t.exception()!r}")
        if t.exception()
        else None
    )
    print("Signal engine started (Finnhub test feed, background task).")


# ------------------------------------------------------------------ models
class UserIn(BaseModel):
    telegram_id: int
    username: Optional[str] = None


class TxidIn(BaseModel):
    user_id: int
    network: str
    txid: str
    amount: float


class VerifyIn(BaseModel):
    payment_id: int
    approve: bool
    note: Optional[str] = None


class CheckIn(BaseModel):
    payment_id: int


# ------------------------------------------------------------------ helpers
def _require_admin(x_admin_key: Optional[str]) -> None:
    if not ADMIN_KEY:
        raise HTTPException(503, "Admin key is not configured on the server (ADMIN_KEY).")
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(401, "Invalid admin key.")


def _resolve_plan(user_id: Optional[int], plan_param: Optional[str]) -> str:
    """Plan resolution: a real user's stored plan wins; otherwise the
    ?plan= query param (free/premium) is used (handy for the frontend
    preview); default free. Expired premium auto-expires to free."""
    if user_id:
        user = db.get_user(user_id)
        if user:
            db.expire_premium_if_needed(user)
            return db.effective_plan(user)
    return "premium" if (plan_param or "").lower() == "premium" else "free"


def _public_signal(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "pair": row.get("pair"),
        "broker": row.get("broker"),
        "strategy": row.get("strategy"),
        "direction": row.get("direction"),
        "entry": row.get("entry"),
        "expiry": row.get("expiry"),
        "sent_at": row.get("sent_at"),
        "result": row.get("result"),
        "market": row.get("market", "real"),
        "is_beta": bool(row.get("is_beta", True)),
        "locked": row.get("locked", False),
    }


# ------------------------------------------------------------------- health
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "onyx-signal-bot",
        "version": "0.1.0-beta",
        "db": db_backend(),
        "market_scope": "real_only",
        "otc": "coming_soon",
        "beta": True,
        "checkout_enabled": db.checkout_enabled(),
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------ signals
@app.get("/api/signals")
def list_signals(
    broker: Optional[str] = Query(None, description="quotex | olymp_trade | pocket_option"),
    strategy: Optional[str] = Query(None, description="elite | turbo | prime"),
    market: str = Query("real", description="real only in this phase; otc returns empty (coming soon)"),
    plan: Optional[str] = Query(None, description="free | premium (used when user_id is absent)"),
    user_id: Optional[int] = Query(None, description="DB user id; stored plan wins over ?plan="),
    limit: int = Query(50, ge=1, le=100),
):
    if broker and broker not in plans.BROKERS:
        raise HTTPException(400, f"Unknown broker: {broker}")
    if strategy and strategy not in plans.STRATEGIES:
        raise HTTPException(400, f"Unknown strategy: {strategy}")
    if market not in ("real", "otc"):
        raise HTTPException(400, "market must be 'real' or 'otc'")
    if market == "otc":
        # Real pairs only in this phase — OTC is coming soon, never served.
        return {"signals": [], "meta": {
            "plan": _resolve_plan(user_id, plan), "market": "otc",
            "otc_enabled": False, "otc_note": plans.OTC_COMING_SOON_MESSAGE,
            "beta": True, "shown": 0, "locked_count": 0}}
    rows = db.get_signals(broker=broker, strategy=strategy, market="real", limit=limit)
    resolved_plan = _resolve_plan(user_id, plan)
    shaped = plans.apply_plan_to_signals([_public_signal(r) for r in rows], resolved_plan)
    # Free daily counter: count the unlocked sample served (best-effort, only for known users).
    if user_id and resolved_plan == "free" and shaped["signals"]:
        user = db.get_user(user_id)
        if user:
            db.bump_free_signal_count(user)
    return shaped


@app.get("/api/results")
def list_results(
    broker: Optional[str] = Query(None),
    strategy: Optional[str] = Query(None),
    plan: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=200),
):
    if broker and broker not in plans.BROKERS:
        raise HTTPException(400, f"Unknown broker: {broker}")
    if strategy and strategy not in plans.STRATEGIES:
        raise HTTPException(400, f"Unknown strategy: {strategy}")
    resolved_plan = _resolve_plan(user_id, plan)
    scope = plans.results_scope(resolved_plan)
    rows = db.get_results(broker=broker, strategy=strategy, since=scope["since"], limit=limit)
    return {
        "results": [_public_signal(r) for r in rows],
        "meta": {"plan": resolved_plan, "scope": scope["scope"], "shown": len(rows),
                 "beta": True, "market": "real",
                 "note": "Results are never edited after posting. Wins and losses both shown."},
    }


# ------------------------------------------------------------------ premium
@app.get("/api/premium/status")
def premium_status(user_id: int = Query(...)):
    user = db.get_user(user_id)
    if not user:
        return {"user_id": user_id, "plan": "free", "premium_until": None,
                "days_left": 0, "expired": False, "beta": True}
    db.expire_premium_if_needed(user)
    plan = db.effective_plan(user)
    until = user.get("premium_until")
    days_left = 0
    if plan == "premium" and until:
        days_left = max(0, (plans.date.fromisoformat(str(until)[:10])
                            - datetime.now(timezone.utc).date()).days)
    return {"user_id": user_id, "plan": plan, "premium_until": until,
            "days_left": days_left,
            "expired": bool(user.get("premium_until")) and plan == "free",
            "beta": True}


@app.get("/api/premium/info")
def premium_info():
    settings = db.get_settings()
    return {
        "price_usdt": float(settings.get("premium_price_usdt", "1.97")),
        "days": int(settings.get("premium_days", "30")),
        "networks": ["BSC", "Tron"],
        "wallet_bsc": settings.get("wallet_bsc"),
        "wallet_tron": settings.get("wallet_tron"),
        "wallets_are_placeholders": "PLACEHOLDER" in str(settings.get("wallet_bsc", "")),
        "checkout_enabled": db.checkout_enabled(),
        "free_daily_limit": int(settings.get("free_daily_limit", "10")),
        "beta": True,
        "risk_warning": ("Trading binary options is high risk. You can lose part or all of your "
                         "money. Signals are information, not a guarantee of profit."),
    }


# --------------------------------------------------------------------- users
@app.post("/api/users")
def create_user(body: UserIn):
    user = db.upsert_user(body.telegram_id, body.username)
    return {"user_id": user["id"], "telegram_id": user["telegram_id"],
            "username": user.get("username"), "plan": db.effective_plan(user),
            "premium_until": user.get("premium_until")}


# ------------------------------------------------------------------ payments
@app.post("/api/payments/txid")
def submit_txid(body: TxidIn):
    """Submit a TXID for a Premium payment. Stored as pending; nothing is
    approved here. Checkout must be enabled (owner flips it on once real
    wallet addresses are in place)."""
    if not db.checkout_enabled():
        raise HTTPException(
            503,
            "Premium checkout is disabled (beta). Wallet addresses are still "
            "placeholders — the owner enables checkout once the real BSC and "
            "Tron addresses are set.",
        )
    network = body.network.strip()
    if network.lower() == "bsc":
        network = "BSC"
    elif network.lower() == "tron":
        network = "Tron"
    if network not in plans.NETWORKS:
        raise HTTPException(400, "network must be 'BSC' or 'Tron'.")
    try:
        amount = Decimal(str(body.amount))
    except InvalidOperation:
        raise HTTPException(400, "amount must be a number.")
    if amount != Decimal("1.97"):
        raise HTTPException(400, f"amount must be exactly 1.97 USDT (got {amount}).")
    txid = body.txid.strip()
    if not txid:
        raise HTTPException(400, "txid is required.")
    user = db.get_user(body.user_id)
    if not user:
        raise HTTPException(404, f"No user with user_id {body.user_id}. Register via POST /api/users first.")
    if db.get_payment_by_txid(txid):
        raise HTTPException(409, "This TXID has already been submitted. One TXID can only ever be used once.")
    payment = db.create_payment(body.user_id, network, float(amount), txid)
    settings = db.get_settings()
    wallet = settings.get("wallet_bsc") if network == "BSC" else settings.get("wallet_tron")
    return {
        "status": "pending",
        "payment_id": payment["id"],
        "network": network,
        "amount": float(amount),
        "message": ("Payment received for manual verification. Premium starts "
                    "after your payment is confirmed (30 days from approval)."),
        "manual_check": txid_check.manual_check_instructions(network, txid, wallet or ""),
    }


@app.post("/api/payments/verify")
def verify_payment(body: VerifyIn, x_admin_key: Optional[str] = Header(None)):
    """Admin hook (manual check first). Approves/rejects a pending payment;
    on approval, premium is set for 30 days from approval.
    Auth: X-Admin-Key header = ADMIN_KEY env var. Real admin auth must
    replace this before launch."""
    _require_admin(x_admin_key)
    payment = db.get_payment(body.payment_id)
    if not payment:
        raise HTTPException(404, f"No payment with payment_id {body.payment_id}.")
    if payment.get("status") != "pending":
        raise HTTPException(409, f"Payment is already {payment['status']}; only pending payments can be decided.")
    updated = db.decide_payment(body.payment_id, approve=body.approve,
                                decided_by="manual", note=body.note)
    user = db.get_user(int(payment["user_id"]))
    return {
        "status": updated["status"],
        "payment_id": updated["id"],
        "user_id": payment["user_id"],
        "plan": db.effective_plan(user) if user else None,
        "premium_until": user.get("premium_until") if user else None,
        "message": ("Premium active for 30 days from today." if body.approve
                    else "Payment rejected. Do not send USDT again until support replies."),
    }


@app.post("/api/payments/check")
def auto_check_payment(body: CheckIn, x_admin_key: Optional[str] = Header(None)):
    """OPTIONAL auto-check (opt-in, AUTO_TXID_CHECK=true). Advisory only:
    it reports what the public explorer says — it never approves a payment.
    Approval still needs POST /api/payments/verify."""
    _require_admin(x_admin_key)
    payment = db.get_payment(body.payment_id)
    if not payment:
        raise HTTPException(404, f"No payment with payment_id {body.payment_id}.")
    if not txid_check.AUTO_CHECK_ENABLED:
        return {"status": "disabled",
                "message": "Auto TXID check is off (default). Use the manual check: "
                           "look the TXID up in BscScan / TronScan, then call /api/payments/verify.",
                "manual_check": "See POST /api/payments/txid response or txid_check.manual_check_instructions()."}
    settings = db.get_settings()
    wallet = settings.get("wallet_bsc") if payment["network"] == "BSC" else settings.get("wallet_tron")
    if "PLACEHOLDER" in str(wallet or ""):
        return {"status": "unverifiable",
                "reason": "Wallet address is still a placeholder — set the real address before auto-check can run."}
    verdict = txid_check.check_txid(payment["network"], payment["txid"], str(wallet))
    verdict["payment_id"] = payment["id"]
    verdict["advisory_only"] = True
    verdict["note"] = "This does NOT approve the payment. Approve via POST /api/payments/verify."
    return verdict


# ----------------------------------------------------------------- telegram
@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    """Telegram bot webhook (webhook only — no polling; Render free tier
    cannot run a background worker). Answers /start and /help; ignores
    everything else silently. Returns 503 while TELEGRAM_BOT_TOKEN is
    unset, 401 when a configured webhook secret does not match."""
    secret = telegram_bot.webhook_secret()
    if secret and x_telegram_bot_api_secret_token != secret:
        raise HTTPException(401, "Invalid webhook secret.")
    if not telegram_bot.token_configured():
        raise HTTPException(
            503,
            "Telegram bot is not configured on this server "
            "(TELEGRAM_BOT_TOKEN is not set).",
        )
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body.")
    if not isinstance(update, dict):
        raise HTTPException(400, "Update must be a JSON object.")
    handled = telegram_bot.handle_update(update)
    return {"ok": True, "handled": handled}
