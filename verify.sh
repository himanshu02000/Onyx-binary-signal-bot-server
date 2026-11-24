#!/bin/bash
# Onyx backend — local verification script.
# Starts the server on :8000 (SQLite fallback), seeds sample data, and
# curls every endpoint, including the TXID error cases.
# Usage: bash verify.sh   (run from server/)
set -u
cd "$(dirname "$0")"
VENV=.venv/bin
if [ ! -x "$VENV/uvicorn" ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
export ADMIN_KEY=test-admin-key
export ONYX_DB_PATH=/tmp/onyx_verify.db
rm -f "$ONYX_DB_PATH"
$VENV/python seed.py
# Checkout is off by default (correct for beta) — flip it on for this local test only.
sqlite3 "$ONYX_DB_PATH" "UPDATE settings SET value='true' WHERE key='checkout_enabled';"
$VENV/uvicorn app:app --port 8000 >/tmp/onyx_verify_server.log 2>&1 &
SRV=$!
sleep 3
B=http://127.0.0.1:8000
echo "== /health";                 curl -s $B/health; echo
echo "== signals free";           curl -s "$B/api/signals?plan=free"; echo
echo "== signals premium";        curl -s "$B/api/signals?plan=premium" | head -c 600; echo
echo "== signals otc (expect [])"; curl -s "$B/api/signals?market=otc"; echo
echo "== results";                curl -s "$B/api/results?plan=premium" | head -c 400; echo
echo "== create user";            curl -s -X POST $B/api/users -H 'Content-Type: application/json' -d '{"telegram_id": 111, "username": "testuser"}'; echo
echo "== premium status (free)";  curl -s "$B/api/premium/status?user_id=1"; echo
echo "== txid valid";             curl -s -X POST $B/api/payments/txid -H 'Content-Type: application/json' -d '{"user_id":1,"network":"BSC","txid":"0xabc123","amount":1.97}'; echo
echo "== txid duplicate (409)";   curl -s -w " HTTP:%{http_code}" -X POST $B/api/payments/txid -H 'Content-Type: application/json' -d '{"user_id":1,"network":"BSC","txid":"0xabc123","amount":1.97}'; echo
echo "== txid wrong amount (400)";curl -s -w " HTTP:%{http_code}" -X POST $B/api/payments/txid -H 'Content-Type: application/json' -d '{"user_id":1,"network":"Tron","txid":"0xdef456","amount":2.00}'; echo
echo "== txid bad network (400)"; curl -s -w " HTTP:%{http_code}" -X POST $B/api/payments/txid -H 'Content-Type: application/json' -d '{"user_id":1,"network":"ETH","txid":"0x999","amount":1.97}'; echo
echo "== verify approve";         curl -s -X POST $B/api/payments/verify -H 'Content-Type: application/json' -H 'X-Admin-Key: test-admin-key' -d '{"payment_id":1,"approve":true}'; echo
echo "== premium status (premium)"; curl -s "$B/api/premium/status?user_id=1"; echo
echo "== verify no key (401)";    curl -s -w " HTTP:%{http_code}" -X POST $B/api/payments/verify -H 'Content-Type: application/json' -d '{"payment_id":1,"approve":true}'; echo
kill $SRV 2>/dev/null
echo "done. server log: /tmp/onyx_verify_server.log"
