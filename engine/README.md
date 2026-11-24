# ONYX Signal Engine — server-side only

The brain of the Onyx binary signal bot (Way 1: pyquotex + free
real-market feed). It reads live candles, runs the strategies, emits
signals, and scores them after expiry. Nothing in this package is
ever shown to a user except through a signal's public fields.

## Scope (owner decision, Nov 24 2026)

- **REAL pairs only.** OTC is "coming soon" — no OTC symbols are
  subscribed, no OTC candles are built, no OTC signal is emitted.
  Every candle/signal carries a `market` field (`"real"` this phase)
  so OTC can be added later without changing the shape.
- **Six forex pairs:** EUR/USD, GBP/USD, EUR/JPY, USD/JPY, AUD/USD,
  USD/CAD. **No gold** — binary is forex-only for the owner.
- **BETA MODE:** every signal is written with `status="test"` /
  `is_beta=true`. No signal is delivered to any user. The engine is a
  test harness until a strategy proves it wins.
- **Free stack only.** No paid service anywhere.

## Honest state of the strategies

Every strategy tested so far LOSES vs the 53.2% breakeven (88%
payout on Quotex real pairs). Best measured is HA-fade at ~51%.
**Turbo was never backtested** — its implementation here is a
documented, conservative interpretation of the owner's description,
disabled by default, and must be backtested before it is trusted.
Do not claim any strategy wins.

## Layout

```
engine/
  types.py        Candle / Signal / Market / pairs / session window
  candles.py      CandleStore — 1m feed, builds 2/3/5/15/30/60m,
                  complete candles only (backtest rule A1)
  feed.py         HistdataFileFeed (test replay) + TwelveDataFeed
                  (free live-feed option: seed/backfill only)
  strategies.py   Elite, Prime, HA-fade, Turbo — SERVER SIDE ONLY
  results.py      ResultChecker (tie = loss, missing bar = void) +
                  JsonFileSignalStore + DbSignalStore (db.py adapter)
  quotex_feed.py  pyquotex wrapper: demo, read-only, SSID env auth
  run.py          runner: --mode test (replay) / live (stream)
  requirements.txt  pyquotex (GitHub — not on PyPI)
scripts/
  prove_stream.py prove the live Quotex stream (60s, real pairs)
```

The backend skeleton lives next door (`server/db.py`, `app.py`) and
is owned by the backend agent. The engine writes signals through
`db.insert_signal` / `db.update_signal_result` via `DbSignalStore`
when a database is wanted; otherwise it uses the local JSONL outbox
(`data/signals.jsonl`, `data/results.jsonl`).

### Known schema gaps (flagged, not fixed here — backend's area)

1. `signals.strategy` CHECK is `('elite','turbo','prime')` — **HA-fade
   is missing**. HA-fade signals cannot be written to the db until
   `'ha_fade'` is added to that CHECK (schema.sql + db.py SQLite
   schema). `DbSignalStore` keeps them in the JSONL fallback.
2. `signals.result` CHECK is `('win','loss','pending')` — no `'void'`.
   VOID results stay `'pending'` in the db; the JSONL outbox records
   them properly.

## Strategy rules (ports of the backtests)

- **Elite** (`backtest_elite_binary.py`, A1–A11): higher-TF sweep
  candle → last FVG in the sweep leg → entry at the open of the next
  lower-TF candle after price retraces into the zone; invalidate if a
  candle closes beyond the zone's far edge; 10-candle window; expiry
  = lower-TF duration. TF pairs 5m→1m, 15m→2m, 30m→3m, 1h→5m.
  Default trigger `overlap` (genuine zone entry); `literal` is the
  backtest's primary variant (which never actually waits — see the
  backtest report).
- **Prime** (`backtest_pullback.py`, A1–A7): 5m chart, trend from
  market structure (HH+HL / LL+LH, fractal window 2), largest
  pullback body-to-body projected forward, enter with the trend at
  the zone, pullback ≥ 1.0× ATR(14). Real market only. Default
  expiry 5 min.
- **HA-fade** (`backtest_heikinashi_fade.py`, A1–A7): two consecutive
  same-colour Heikin Ashi candles → trade opposite on the next
  candle (two greens → PUT, two reds → CALL); entry at the next
  open, expiry one candle. Default TFs 15/30/60m (1m fires ~700/day
  and wins least).
- **Turbo** — UNPROVEN, see `strategies.py` header for the exact
  mechanical reading. Disabled by default; enable only with
  `--strategies ...,turbo` and only for testing.

Session window for all strategies: entries 00:00–21:00 UTC.
Expiry scoring: expiry price = close of the 1m bar at entry +
(expiry − 1) min; tie = loss; missing bar = void.

## Quickstart

```bash
cd ~/workspace/signal-bot/server
python3 -m venv .venv
.venv/bin/pip install -r engine/requirements.txt

# TEST MODE — no credentials, no network. Replays stored Histdata
# candles and compares each strategy against its published backtest.
.venv/bin/python -m engine.run --mode test
.venv/bin/python -m engine.run --mode test --dataset short \
    --pairs EURUSD --strategies prime   # Prime's published dataset

# LIVE MODE — needs a Quotex demo session (see below).
QUOTEX_EMAIL=... QUOTEX_SSID=... .venv/bin/python -m engine.run --mode live
```

## Live feed (Way 1) — env vars, never files, never a password

| Var | Meaning |
|-----|---------|
| `QUOTEX_EMAIL` | Demo-account email |
| `QUOTEX_SSID` | Session token (SSID) for that account |
| `QUOTEX_USER_AGENT` | Optional user-agent string |
| `TWELVEDATA_API_KEY` | Optional: free Twelve Data key (seed/backfill only) |

There is **no password** in this engine. pyquotex's own
`credentials()` helper (prompts + writes `settings/config.ini`) and
`set_session()` (persists the token to `session.json`) are never
called. The SSID is injected into the client's in-memory
`session_data` only, and pyquotex's session directory is pointed at
a fresh temp dir. The account is forced to PRACTICE (demo) and the
code never calls any trade/order method — read-only by construction.

If the stream drops, `QuotexFeed` reconnects with exponential
backoff (1s → 60s cap) and resubscribes. While it is down, no
candles are emitted and no signals are made from stale data — the
engine keeps running on the real-market feed instead.

### Free live-feed option (Twelve Data, Basic plan — free)

Verified Nov 24 2026: **8 credits/min, 800 credits/day, reset
midnight UTC**, 1 credit per symbol per standard `time_series` call.
That budget **cannot** sustain live 1m polling for 6 pairs
(6 × 1440 = 8,640/day), so `TwelveDataFeed` is used only for
seeding history at startup and backfilling gaps after a reconnect
(6 pairs every 15 min ≈ 576 credits/day — inside budget). Live
1-minute candles in this phase come from Quotex. Note: some trackers
list the free tier as non-commercial use — check the licence on
twelvedata.com/pricing before user-facing use.

## Proving the live stream

```bash
QUOTEX_EMAIL=... QUOTEX_SSID=... .venv/bin/python scripts/prove_stream.py
# prints live 1m candles for EURUSD,GBPUSD for 60s, then a summary.
# Without the env vars it exits 2 with a clear message — no crash,
# no password prompt.
```

**How to get the session values:** the Quotex demo login lives in
the Secure Vault (email Moneyfesting1000dollars@gmail.com). The
parent agent routes a browser sign-in through the vault path; from
the logged-in browser session, copy the SSID and export it as
`QUOTEX_SSID` with `QUOTEX_EMAIL`. SSIDs expire — a rejected stream
means "obtain a fresh SSID the same way", never "enter a password".

## Test-mode verification (run Nov 24 2026)

All runs replay the stored Histdata candles (ext files: 6 forex
pairs, Aug 2025–Nov 2026, ~2.87M 1m bars) unless noted. No strategy
beats the 53.2% breakeven — the engine is a test harness, as briefed.

| Strategy | Engine (test mode) | Published backtest |
|----------|--------------------|--------------------|
| Elite, overlap, 6 forex pairs | **47.08%** (49,805 trades) — `test-results-elite-ext.json` | O combined 47.50–50.18% (4 pairs incl. gold); forex-only best 49.50% (1h→5m) |
| Elite, same 3 pairs as the backtest (EUR/GBP/EURJPY) | **47.45%** (24,949 trades); per-cell win rates and trade counts match the published overlap table essentially exactly (e.g. 5m→1m EUR 47.20% / 3,909 trades — identical; 1h→5m forex-only 49.50% — identical) | same table (`RESULTS_ELITE_BINARY.md`) |
| HA-fade, 15/30/60m, 6 forex pairs | **50.49%** (210,441 trades) — 15m ~50.1%, 30m ~50.9%, 1h ~51.2% | forex-only 15m 50.23%, 30m 50.95%, 1h 51.17% |
| Prime (EUR/USD, 5m/5m, short file) | **44.73%** (883 trades) | 49.07% published — **but see note** |
| Turbo (first measurement, UNPROVEN) | **37.50%** (4,456 trades) — the gap pattern mean-reverts; clearly losing | NEVER BACKTESTED — first measurement only |

**Prime lookahead note (found + verified Nov 24 2026):** the
published 49.07% embeds a 2-bar fractal lookahead — the backtest
confirms a swing at the touch candle using candles i+1/i+2, which a
live engine cannot know. Re-running the backtest's own code with
swings restricted to those confirmed by data up to the touch candle
gives 44.78% on 882 trades; the engine gives 44.73% on 883 trades.
The logic matches; 44.7% is the honest live-faithful Prime number,
not 49.07%. (Prime also has no session filter in its backtest, unlike
Elite/HA-fade — the engine matches each strategy to its own backtest.)

Two real bugs were found and fixed by this verification (both in
`strategies.py`, both caught only because the numbers were compared):
1. Elite's sweep-leg lookup included the 1m bar that triggered the
   HTF close, so the 5m→1m leg never matched and that TF pair emitted
   zero signals (first run: 20,055 Elite trades; fixed run: 49,805).
2. A blanket 00:00–21:00 session filter was applied to Prime, whose
   backtest has none (884 vs 808 signals before the fix).
The full combined run (`test-results-ext.json`) is regenerated after
every fix so the artifact always matches the current code.

(Numbers filled in from the actual runs; see the JSON files.)

## What is BLOCKED

- **Live stream proof.** `prove_stream.py` has not run against a
  real Quotex session yet — it needs `QUOTEX_EMAIL` + `QUOTEX_SSID`
  from the vault/browser path (above). Until that runs, the live
  feed is code-reviewed and import-tested, not proven.
- **HA-fade in the db** (schema CHECK gap above) and **VOID in the
  db** (no `'void'` result value) — backend agent's call.
