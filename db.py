"""Onyx — database layer (the ONLY place SQL / Supabase calls live).

Two backends, one interface. Every other module (app.py, and later the
signal engine in server/engine/) goes through the functions in this file
and never touches a client directly.

1. Supabase (the real store) — used when SUPABASE_URL and SUPABASE_KEY
   are set in the environment. Uses the `supabase` Python client.
2. SQLite fallback (LOCAL DEV / TESTS ONLY) — used when those vars are
   absent. A file database (ONYX_DB_PATH, default server/onyx_dev.db).
   It mirrors the Supabase tables so the API can be run and tested with
   zero setup. NEVER point production at SQLite; Supabase is the real
   store. The SQLite file is git-ignored and must never be deployed.

SCOPE (Nov 24, owner decision): REAL PAIRS ONLY for now; OTC is
"coming soon". The `market` column exists in both backends, but
get_signals() / get_results() only ever return market='real' unless a
caller explicitly passes market='otc' AND otc is enabled — and the API
layer never does that in this phase (see plans.py OTC_ENABLED=False).

No strategy logic lives here. This layer only stores / serves signals.
No secrets are read from anywhere except environment variables.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------- config
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()  # anon key is enough for the API; service key only if RLS demands it
ONYX_DB_PATH = os.environ.get(
    "ONYX_DB_PATH", str(Path(__file__).resolve().parent / "onyx_dev.db")
)

PREMIUM_PRICE = float(os.environ.get("PREMIUM_PRICE_USDT", "1.97"))
PREMIUM_DAYS = int(os.environ.get("PREMIUM_DAYS", "30"))
FREE_DAILY_LIMIT = int(os.environ.get("FREE_DAILY_LIMIT", "10"))

# ---------------------------------------------------------------- backend
_supabase_client = None
_thread_local = threading.local()  # one SQLite conn per thread (FastAPI runs endpoints in a threadpool)


def backend() -> str:
    """'supabase' when configured, otherwise 'sqlite' (local dev/tests)."""
    return "supabase" if (SUPABASE_URL and SUPABASE_KEY) else "sqlite"


def get_supabase():
    """Lazily build the Supabase client. Raises if not configured."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client  # imported lazily: not needed for SQLite dev

        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


# ------------------------------------------------------- SQLite fallback
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free','premium')),
    premium_until DATE,
    signals_used_today INTEGER NOT NULL DEFAULT 0,
    last_signal_date DATE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','blocked')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    broker TEXT NOT NULL CHECK (broker IN ('quotex','olymp_trade','pocket_option','finnhub')),
    strategy TEXT NOT NULL CHECK (strategy IN ('elite','turbo','prime','ha_fade','momentum')),
    direction TEXT NOT NULL CHECK (direction IN ('call','put')),
    entry TEXT NOT NULL,
    expiry TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    result TEXT NOT NULL DEFAULT 'pending' CHECK (result IN ('win','loss','pending','void')),
    market TEXT NOT NULL DEFAULT 'real' CHECK (market IN ('real','otc')),
    is_beta INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    network TEXT NOT NULL CHECK (network IN ('BSC','Tron')),
    amount REAL NOT NULL CHECK (amount = 1.97),
    txid TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT,
    decided_by TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('wallet_bsc',  '0xPLACEHOLDER_BSC — replace with real address before launch'),
    ('wallet_tron', 'PLACEHOLDER_TRON — replace with real address before launch'),
    ('premium_price_usdt', '1.97'),
    ('premium_days', '30'),
    ('free_daily_limit', '10'),
    ('checkout_enabled', 'false'),
    ('otc_enabled', 'false');
CREATE INDEX IF NOT EXISTS idx_signals_sent_at ON signals (sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_txid ON payments (txid);
"""


def get_sqlite() -> sqlite3.Connection:
    # One SQLite connection per thread: FastAPI runs sync endpoints in a
    # threadpool, and a sqlite3 connection may only be used in its own thread.
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(ONYX_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SQLITE_SCHEMA)
        conn.commit()
        _thread_local.conn = conn
    return conn


def init_db() -> str:
    """Ensure tables exist on the active backend. Returns backend name."""
    if backend() == "supabase":
        get_supabase()  # just validates the client builds; tables come from schema.sql
    else:
        get_sqlite()
    return backend()


# ------------------------------------------------------------ helpers
def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _is_premium_active(user: Optional[dict]) -> bool:
    """Premium is an expiry DATE: active only while premium_until >= today."""
    if not user or user.get("plan") != "premium":
        return False
    until = _parse_date(user.get("premium_until"))
    return bool(until and until >= _today())


def effective_plan(user: Optional[dict]) -> str:
    """'premium' only if the expiry date has not passed, else 'free'."""
    return "premium" if _is_premium_active(user) else "free"


# ------------------------------------------------------------ settings
def get_settings() -> dict:
    """Wallet addresses, price, limits. Placeholders until the owner sets real ones."""
    if backend() == "supabase":
        rows = get_supabase().table("settings").select("key,value").execute().data or []
        out = {r["key"]: r["value"] for r in rows}
    else:
        cur = get_sqlite().execute("SELECT key, value FROM settings")
        out = {r["key"]: r["value"] for r in cur.fetchall()}
    out.setdefault("premium_price_usdt", str(PREMIUM_PRICE))
    out.setdefault("premium_days", str(PREMIUM_DAYS))
    out.setdefault("free_daily_limit", str(FREE_DAILY_LIMIT))
    out.setdefault("checkout_enabled", "false")
    out.setdefault("otc_enabled", "false")
    return out


def checkout_enabled() -> bool:
    """Premium checkout is OFF until the owner sets real wallet addresses
    and flips this on. Default false (beta)."""
    return get_settings().get("checkout_enabled", "false").lower() == "true"


def otc_enabled() -> bool:
    """False in this phase (Nov 24: real pairs only, OTC coming soon)."""
    return get_settings().get("otc_enabled", "false").lower() == "true"


# ---------------------------------------------------------------- users
def get_user_by_telegram_id(telegram_id: int) -> Optional[dict]:
    if backend() == "supabase":
        res = (
            get_supabase().table("users")
            .select("*").eq("telegram_id", telegram_id).limit(1).execute()
        )
        return (res.data or [None])[0]
    row = get_sqlite().execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    return dict(row) if row else None


def get_user(user_id: int) -> Optional[dict]:
    if backend() == "supabase":
        res = get_supabase().table("users").select("*").eq("id", user_id).limit(1).execute()
        return (res.data or [None])[0]
    row = get_sqlite().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def upsert_user(telegram_id: int, username: Optional[str] = None) -> dict:
    """Create the user on first sight, or refresh their username. Returns the row."""
    existing = get_user_by_telegram_id(telegram_id)
    if existing:
        if username and username != existing.get("username"):
            if backend() == "supabase":
                get_supabase().table("users").update({"username": username}).eq("id", existing["id"]).execute()
            else:
                get_sqlite().execute(
                    "UPDATE users SET username = ? WHERE id = ?", (username, existing["id"])
                )
                get_sqlite().commit()
            existing["username"] = username
        return existing
    if backend() == "supabase":
        res = (
            get_supabase().table("users")
            .insert({"telegram_id": telegram_id, "username": username})
            .execute()
        )
        return res.data[0]
    cur = get_sqlite().execute(
        "INSERT INTO users (telegram_id, username) VALUES (?, ?)", (telegram_id, username)
    )
    get_sqlite().commit()
    return get_user(cur.lastrowid)  # type: ignore[arg-type]


def set_premium(user_id: int, days: int = PREMIUM_DAYS) -> Optional[dict]:
    """Premium = 30 days from approval. Sets plan + expiry date."""
    until = _today() + timedelta(days=days)
    if backend() == "supabase":
        get_supabase().table("users").update(
            {"plan": "premium", "premium_until": until.isoformat()}
        ).eq("id", user_id).execute()
    else:
        get_sqlite().execute(
            "UPDATE users SET plan='premium', premium_until=? WHERE id=?",
            (until.isoformat(), user_id),
        )
        get_sqlite().commit()
    return get_user(user_id)


def expire_premium_if_needed(user: dict) -> dict:
    """If premium_until has passed, flip the stored plan back to free.
    Returns the (possibly updated) user dict."""
    if user.get("plan") == "premium" and not _is_premium_active(user):
        if backend() == "supabase":
            get_supabase().table("users").update({"plan": "free"}).eq("id", user["id"]).execute()
        else:
            get_sqlite().execute("UPDATE users SET plan='free' WHERE id=?", (user["id"],))
            get_sqlite().commit()
        user["plan"] = "free"
    return user


def count_free_signals_today(user: dict) -> int:
    """Signals served to this free user today (resets on a new day)."""
    if user.get("last_signal_date") != _today().isoformat():
        return 0
    return int(user.get("signals_used_today") or 0)


def bump_free_signal_count(user: dict) -> None:
    today = _today().isoformat()
    if backend() == "supabase":
        get_supabase().table("users").update(
            {"signals_used_today": count_free_signals_today(user) + 1, "last_signal_date": today}
        ).eq("id", user["id"]).execute()
    else:
        get_sqlite().execute(
            "UPDATE users SET signals_used_today=?, last_signal_date=? WHERE id=?",
            (count_free_signals_today(user) + 1, today, user["id"]),
        )
        get_sqlite().commit()


# -------------------------------------------------------------- signals
# NOTE for the engine agent: insert_signal() is the write path you reuse.
# The API never serves OTC in this phase — see get_signals() below.

def insert_signal(
    pair: str,
    broker: str,
    strategy: str,
    direction: str,
    entry: str,
    expiry: str,
    market: str = "real",
    sent_at: Optional[str] = None,
    is_beta: bool = True,
) -> dict:
    """Write one signal. `market` is kept for later; the engine writes
    'real' only in this phase (Nov 24 scope: real pairs only)."""
    row = {
        "pair": pair,
        "broker": broker,
        "strategy": strategy,
        "direction": direction.lower(),
        "entry": entry,
        "expiry": expiry,
        "market": market,
        "is_beta": is_beta,
    }
    if sent_at:
        row["sent_at"] = sent_at
    if backend() == "supabase":
        res = get_supabase().table("signals").insert(row).execute()
        return res.data[0]
    cols = ", ".join(row.keys())
    cur = get_sqlite().execute(
        f"INSERT INTO signals ({cols}) VALUES ({','.join('?' * len(row))})",
        tuple(1 if (k == "is_beta" and v is True) else v for k, v in row.items()),
    )
    get_sqlite().commit()
    return get_signal(cur.lastrowid)  # type: ignore[arg-type]


def count_signals_today() -> int:
    """Signals written today (UTC), manual + automatic — the free
    daily counter for on-demand Generate. Counts rows in the same
    table GET /api/signals reads."""
    today = _today().isoformat()
    if backend() == "supabase":
        res = (
            get_supabase().table("signals").select("id")
            .gte("sent_at", today + "T00:00:00+00:00").execute()
        )
        return len(res.data or [])
    row = get_sqlite().execute(
        "SELECT COUNT(*) AS n FROM signals WHERE date(sent_at) = ?", (today,)
    ).fetchone()
    return int(row["n"])


def get_signal(signal_id: int) -> Optional[dict]:
    if backend() == "supabase":
        res = get_supabase().table("signals").select("*").eq("id", signal_id).limit(1).execute()
        return (res.data or [None])[0]
    row = get_sqlite().execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    return dict(row) if row else None


def get_signals(
    broker: Optional[str] = None,
    strategy: Optional[str] = None,
    market: str = "real",
    limit: int = 50,
) -> list[dict]:
    """Newest first. REAL MARKET ONLY by default — OTC is never served
    in this phase: passing market='otc' returns [] unless otc is enabled
    in settings (it is not, Nov 24 scope)."""
    if market == "otc" and not otc_enabled():
        return []
    market = "real"  # hard guard for this phase, regardless of caller
    if backend() == "supabase":
        q = get_supabase().table("signals").select("*").eq("market", "real")
        if broker:
            q = q.eq("broker", broker)
        if strategy:
            q = q.eq("strategy", strategy)
        res = q.order("sent_at", desc=True).limit(limit).execute()
        return res.data or []
    sql = "SELECT * FROM signals WHERE market = 'real'"
    params: list[Any] = []
    if broker:
        sql += " AND broker = ?"
        params.append(broker)
    if strategy:
        sql += " AND strategy = ?"
        params.append(strategy)
    sql += " ORDER BY sent_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in get_sqlite().execute(sql, params).fetchall()]


def get_results(
    broker: Optional[str] = None,
    strategy: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Past signals with win/loss/pending, newest first. Real only (see above)."""
    if backend() == "supabase":
        q = get_supabase().table("signals").select("*").eq("market", "real")
        if broker:
            q = q.eq("broker", broker)
        if strategy:
            q = q.eq("strategy", strategy)
        if since:
            q = q.gte("sent_at", since)
        res = q.order("sent_at", desc=True).limit(limit).execute()
        return res.data or []
    sql = "SELECT * FROM signals WHERE market = 'real'"
    params: list[Any] = []
    if broker:
        sql += " AND broker = ?"
        params.append(broker)
    if strategy:
        sql += " AND strategy = ?"
        params.append(strategy)
    if since:
        sql += " AND sent_at >= ?"
        params.append(since)
    sql += " ORDER BY sent_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in get_sqlite().execute(sql, params).fetchall()]


def update_signal_result(signal_id: int, result: str) -> Optional[dict]:
    """Result checker hook (engine agent): mark win/loss after expiry.
    Results are never edited after posting — this is the one write."""
    assert result in ("win", "loss", "pending", "void")
    if backend() == "supabase":
        get_supabase().table("signals").update({"result": result}).eq("id", signal_id).execute()
    else:
        get_sqlite().execute(
            "UPDATE signals SET result=? WHERE id=?", (result, signal_id)
        )
        get_sqlite().commit()
    return get_signal(signal_id)


# ------------------------------------------------------------- payments
def get_payment_by_txid(txid: str) -> Optional[dict]:
    txid = txid.strip()
    if backend() == "supabase":
        res = get_supabase().table("payments").select("*").eq("txid", txid).limit(1).execute()
        return (res.data or [None])[0]
    row = get_sqlite().execute("SELECT * FROM payments WHERE txid = ?", (txid,)).fetchone()
    return dict(row) if row else None


def create_payment(user_id: int, network: str, amount: float, txid: str) -> dict:
    """Store a TXID submission as pending. Caller must have checked for
    duplicates first — the UNIQUE constraint is the backstop."""
    row = {"user_id": user_id, "network": network, "amount": amount,
           "txid": txid.strip(), "status": "pending"}
    if backend() == "supabase":
        res = get_supabase().table("payments").insert(row).execute()
        return res.data[0]
    cur = get_sqlite().execute(
        "INSERT INTO payments (user_id, network, amount, txid, status) VALUES (?,?,?,?,?)",
        (user_id, network, amount, txid.strip(), "pending"),
    )
    get_sqlite().commit()
    return get_payment(cur.lastrowid)  # type: ignore[arg-type]


def get_payment(payment_id: int) -> Optional[dict]:
    if backend() == "supabase":
        res = get_supabase().table("payments").select("*").eq("id", payment_id).limit(1).execute()
        return (res.data or [None])[0]
    row = get_sqlite().execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    return dict(row) if row else None


def decide_payment(payment_id: int, approve: bool, decided_by: str = "manual",
                   note: Optional[str] = None) -> Optional[dict]:
    """Mark a pending payment approved/rejected. On approval, premium is
    set for 30 days from approval. Returns the updated payment, or None."""
    payment = get_payment(payment_id)
    if not payment or payment.get("status") != "pending":
        return payment
    status = "approved" if approve else "rejected"
    now = datetime.now(timezone.utc).isoformat()
    if backend() == "supabase":
        get_supabase().table("payments").update(
            {"status": status, "decided_at": now, "decided_by": decided_by, "note": note}
        ).eq("id", payment_id).execute()
    else:
        get_sqlite().execute(
            "UPDATE payments SET status=?, decided_at=?, decided_by=?, note=? WHERE id=?",
            (status, now, decided_by, note, payment_id),
        )
        get_sqlite().commit()
    if approve:
        set_premium(int(payment["user_id"]))
    return get_payment(payment_id)
