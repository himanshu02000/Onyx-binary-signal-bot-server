"""Onyx — Telegram bot handler (webhook only, no polling).

Why webhook only: Render free tier cannot run a background worker, so
the bot answers via a FastAPI endpoint (POST /telegram/webhook in
app.py). Telegram POSTs each update; we reply with a raw Bot API call.

Why raw Bot API calls (no python-telegram-bot): one less dependency,
and `requests` is already in requirements.txt (txid_check.py uses it).

Secrets: the bot token comes ONLY from the TELEGRAM_BOT_TOKEN env var
— never hardcoded, never written to any file, never logged. If it is
absent, the webhook endpoint returns 503 and the rest of the app is
unaffected. An optional TELEGRAM_WEBHOOK_SECRET env var, when set,
makes the endpoint reject webhook POSTs whose
X-Telegram-Bot-Api-Secret-Token header does not match (401).

What the bot does (owner-approved copy, no accuracy claims):
  /start -> register the user (best-effort) + welcome text + one
            "Open Onyx" web_app button to the Mini App URL.
  /help  -> short help text, no keyboard.
  anything else -> ignored silently (200, no reply).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

import db

logger = logging.getLogger("onyx.telegram")

MINI_APP_URL = "https://himanshu02000.github.io/Onyx-binary-signal-bot/"

# Owner-approved welcome text, verbatim. No accuracy claims, no extra promises.
WELCOME_TEXT = (
    "Welcome to Onyx Binary Signal Bot.\n"
    "Signals for Quotex, Olymp Trade and Pocket Option.\n"
    "Free — 10 signals a day.\n"
    "Premium — unlimited signals, no ads, 1.97 USDT/month.\n"
    "Tap Open Onyx to start."
)

HELP_TEXT = (
    "Onyx Binary Signal Bot — Help\n\n"
    "Commands:\n"
    "/start — open the app\n"
    "/help — show this help\n\n"
    "How it works:\n"
    "1. Tap Open Onyx to open the app.\n"
    "2. Free plan: 10 signals a day.\n"
    "3. Premium: unlimited signals, no ads — 1.97 USDT/month."
)

_OPEN_BUTTON = {"text": "🚀 Open Onyx", "web_app": {"url": MINI_APP_URL}}
WELCOME_KEYBOARD = {"inline_keyboard": [[_OPEN_BUTTON]]}

_TIMEOUT_S = 15


def _api_base() -> str:
    """Telegram Bot API base. TELEGRAM_API_BASE exists only so local
    verification can point the calls at a stub server; production
    always uses the default (https://api.telegram.org)."""
    return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def _token() -> Optional[str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    return token or None


def token_configured() -> bool:
    return _token() is not None


def webhook_secret() -> Optional[str]:
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    return secret or None


def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
    """POST sendMessage to the Bot API. Returns True on Telegram ok.
    The token is used only inside the URL — never logged, never stored."""
    token = _token()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set; cannot send Telegram message.")
        return False
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(
            f"{_api_base()}/bot{token}/sendMessage",
            json=payload,
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return bool(resp.json().get("ok"))
    except Exception as exc:  # network / Telegram error -> log, never raise
        logger.warning("Telegram sendMessage failed: %s", exc)
        return False


def _command(text: str) -> str:
    """First word of the message, lowercased ('/start ref_123' -> '/start')."""
    return (text or "").strip().split()[0].lower() if text and text.strip() else ""


def handle_update(update: dict) -> str:
    """Handle one Telegram update dict. Returns 'start' | 'help' | 'ignored'.
    Never raises for a malformed update — Telegram retries on non-200."""
    try:
        message = update.get("message") or {}
        text = message.get("text") or ""
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id or not isinstance(text, str):
            return "ignored"
        cmd = _command(text)
        if cmd == "/start":
            user = message.get("from") or {}
            telegram_id = user.get("id")
            username = user.get("username") or user.get("first_name")
            if telegram_id:
                try:
                    # Same fields as POST /api/users. A DB failure must
                    # never stop the welcome reply, so this is best-effort.
                    db.upsert_user(int(telegram_id), username)
                except Exception as exc:  # noqa: BLE001 - reply must go out anyway
                    logger.warning("Telegram user registration failed: %s", exc)
            send_message(int(chat_id), WELCOME_TEXT, reply_markup=WELCOME_KEYBOARD)
            return "start"
        if cmd == "/help":
            send_message(int(chat_id), HELP_TEXT)
            return "help"
        return "ignored"
    except Exception as exc:  # noqa: BLE001 - never fail the webhook on a bad update
        logger.warning("Telegram update handling failed: %s", exc)
        return "ignored"
