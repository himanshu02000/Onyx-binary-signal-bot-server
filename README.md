# Onyx Binary Signal Bot — Backend Skeleton

FastAPI backend for Onyx (Quotex, Olymp Trade, Pocket Option signals).
Free stack only: Render free tier + Supabase free tier. No paid service,
no paid API key anywhere. Local files only — nothing here is deployed.

**Status: beta skeleton.** The API serves beta/test signals only, and
premium checkout is DISABLED until the owner supplies real wallet
addresses and approves activation.

## Scope (Nov 24, owner decision)

**REAL PAIRS ONLY for now — OTC is "coming soon".** Every signal
endpoint serves `market=real` only; an `?market=otc` request returns an
empty list with an `otc_note`. The `market` column stays in the schema
(real/otc) for later, and the engine writes real-market signals only
in this phase. Nothing else changes.

## What lives here

| File | What it does |
|---|---|
| `app.py` | FastAPI app — all endpoints, plan shaping, TXID submit/verify |
| `db.py` | Database layer — Supabase client + local SQLite fallback. **All queries go through here; the engine agent reuses `insert_signal()` / `update_signal_result()`** |
| `plans.py` | Free/premium rules (single source of truth) |
| `txid_check.py` | TXID verification — manual first; optional auto-check via free BscScan / TronScan public APIs |
| `seed.py` | Local-dev sample data (SQLite only, beta real-market signals) |
| `supabase/schema.sql` | Supabase tables: users, signals, payments, settings |
| `requirements.txt` | Free packages only (pyquotex is the engine agent's, not here) |
| `render.yaml` / `Procfile` | Render free-tier deploy config |
| `.env.example` | Every env var, placeholders only — never a real secret |

**No strategy logic lives in this skeleton.** It only stores and serves
signals. The engine (`server/engine/`) is a separate agent's area — do
not put strategy code here.

## Free / premium rules

- **Free:** up to 10 signals/day (owner's call Nov 24 — was 5), only the first (sample, 1 strategy)
  shows full entry/expiry — signals 2–10 show pair/direction/strategy
  only, entry/expiry locked. Ads on. Results: today only.
- **Premium:** unlimited, all 3 strategies (Elite, Turbo, Prime), full
  details, no ads, full results history. 1.97 USDT/month, 30 days from
  approval. Premium is an expiry DATE (`premium_until`) — it auto-expires.
- **Beta:** every served signal carries `is_beta: true`. No accuracy
  number is claimed anywhere (backtests so far are ~49–52%, below the
  ~53% breakeven — see the content pack p. 11).

Note on the source docs: the build plan says free = "up to 5 a day";
the content pack (p. 6) says free = "1–2 sample signals" with details
locked. This build implements both — up to 5 visible, only the first
fully unlocked. If the owner wants a different split, change
`FREE_SAMPLE_UNLOCKED` in `plans.py` (one line).

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | `{"status":"ok", ...}` + db backend, scope, checkout flag |
| GET | `/api/signals?broker=&strategy=&plan=&user_id=` | Real only, newest first; free/premium shaping per `plans.py` |
| GET | `/api/results?broker=&strategy=&plan=&user_id=` | Win/loss/pending, newest first; free = today only |
| GET | `/api/premium/status?user_id=` | Plan + `premium_until` + `days_left` |
| GET | `/api/premium/info` | Price, networks, wallets, checkout on/off, risk warning |
| POST | `/api/users` | `{telegram_id, username}` — register / refresh |
| POST | `/api/payments/txid` | `{user_id, network, txid, amount}` → stored `pending` |
| POST | `/api/payments/verify` | Admin (`X-Admin-Key`): approve/reject → premium 30 days on approval |
| POST | `/api/payments/check` | Admin: optional auto TXID check — **advisory only, never approves** |

`GET /api/signals` and `/api/results` also accept `?market=otc` — it
always returns an empty list in this phase (OTC coming soon).

## Payments / TXID flow

1. User sends exactly **1.97 USDT** (BSC or Tron) to our wallet and
   pastes the TXID. `POST /api/payments/txid` rejects: duplicate TXID
   (409 — the `txid` column is UNIQUE, so no double-spend ever),
   wrong amount (400 — must be exactly 1.97), unknown network (400),
   unknown user (404). Valid submissions are stored as `pending`.
2. **Manual check first (launch flow):** a human opens the TXID in
   BscScan / TronScan and confirms amount 1.97, destination = our
   wallet, TXID never used before. Then `POST /api/payments/verify`
   with the admin key approves or rejects. Approval sets premium for
   30 days from approval.
3. **Optional auto-check** (`txid_check.py`, `AUTO_TXID_CHECK=true`):
   free public BscScan / TronScan endpoints check tx exists, amount
   is 1.97 USDT, destination matches our wallet. Any failure returns
   `unverifiable` ("manual check needed") — it never invents a result,
   and it never approves a payment by itself.

**Checkout is disabled by default.** `POST /api/payments/txid`
returns 503 until the owner sets real wallet addresses and flips
`checkout_enabled` to `true` in the Supabase `settings` table.
The verify endpoint exists, but no premium activation goes live
without the owner's approval.

**Admin auth warning:** the verify/check endpoints use a single
`ADMIN_KEY` env var (`X-Admin-Key` header) for now. Real admin auth
(proper accounts/roles) must replace this before launch.

## Database

- **Supabase (real store):** apply `supabase/schema.sql` in the
  Supabase SQL Editor. Tables: `users`, `signals`, `payments`
  (`txid` UNIQUE), `settings` (wallet addresses, price 1.97, free
  daily limit 10). Seed settings use clearly-marked PLACEHOLDER
  wallet addresses.
- **SQLite fallback (local dev/tests only):** when `SUPABASE_URL` /
  `SUPABASE_KEY` are absent, `db.py` uses `onyx_dev.db` with the same
  tables. Never point production at SQLite.

## Local run

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill ADMIN_KEY (local test value only)
.venv/bin/python seed.py    # sample beta signals (SQLite fallback)
.venv/bin/uvicorn app:app --reload --port 8000
curl localhost:8000/health
curl "localhost:8000/api/signals?plan=free"
curl "localhost:8000/api/signals?plan=premium"
curl "localhost:8000/api/signals?market=otc"   # -> [] (coming soon)
```

To test the TXID flow locally, flip checkout on in the local SQLite DB
first (it is off by default, as it must be until real wallets exist):

```bash
sqlite3 onyx_dev.db "UPDATE settings SET value='true' WHERE key='checkout_enabled';"
```

## Render deploy (owner does this — nothing is deployed by the agent)

1. Create a Supabase project (free) → run `supabase/schema.sql` in
   its SQL Editor → copy the project URL + anon key.
2. Create a Render Web Service (free, Python) from the
   `onyx-signal-bot-server` repo via Blueprint (`render.yaml` at the
   repo root — this directory IS the repo root).
3. Set env vars in Render (never in code): `SUPABASE_URL`,
   `SUPABASE_KEY`, `ADMIN_KEY`, `WALLET_BSC`, `WALLET_TRON`.
   `FREE_DAILY_LIMIT=10`, `CHECKOUT_ENABLED=false`, `BETA=true` come
   from `render.yaml`. Optional: `BSCSCAN_API_KEY` (free key),
   `AUTO_TXID_CHECK=false`. NOTE: the code reads `SUPABASE_URL` +
   `SUPABASE_KEY` (Supabase REST client), NOT a Postgres
   `DATABASE_URL` connection string — do not wire DATABASE_URL
   expecting the API to use it.
4. Deploy → `GET /health` must return `{"status":"ok"}`.
5. Keep the free tier awake during trading hours with a free ping
   service hitting `/health` every few minutes (Render sleeps when
   idle — 30–60s cold start, same as Captcha Earn).

## Still needed from the owner before launch

- Real BSC (BEP20) and Tron (TRC20) USDT wallet addresses → replace
  the placeholders in `settings` (Supabase) and Render env vars.
- Explicit approval to enable checkout (`checkout_enabled=true`).
- A Quotex demo account for the Way 1 feed (engine agent's area) —
  login goes only in Render env vars, never in chat or code.
- A Supabase project + Render service (see deploy steps above).
- Real admin auth to replace the single `ADMIN_KEY` before launch.
- A strategy that proves it wins (backtest + live test above the
  ~53% breakeven) before any accuracy claim or public launch.
