"""Onyx — TXID verification.

DEFAULT: MANUAL CHECK. A human looks the TXID up in the public explorer
(BscScan for BSC, TronScan for Tron), confirms the amount is 1.97 USDT,
the destination is our wallet, and the TXID was never used before, then
calls POST /api/payments/verify. That is the launch flow.

OPTIONAL AUTO-CHECK (opt-in only): `check_txid()` below does the same
checks against the FREE public explorer endpoints — no paid key needed
for a basic transaction lookup:
  * BSC:  BscScan public API, token-transfer lookup
          (https://api.bscscan.com/api). A free BscScan API key can be set
          via BSCSCAN_API_KEY; without one the endpoint still answers a
          small number of anonymous calls, then rate-limits.
  * Tron: TronScan public API, no key at all
          (https://apilist.tronscanapi.com/api/transaction-info?hash=...)

It NEVER invents an explorer response: every verdict comes from a real
HTTP call, and any network error, unexpected shape, or missing field
returns status "unverifiable" with a reason — never "verified".
Auto-check is OFF unless AUTO_TXID_CHECK=true in the environment, and
even when on, it only ADVISES: it never approves a payment by itself.
Approval always goes through POST /api/payments/verify (admin key).

USDT contracts (public, well-known — verified against BscScan/TronScan listings):
  BSC  (BEP20): 0x55d398326f99059fF775485246999027B3197955
                 (Binance-Peg BSC-USD — 18 DECIMALS, not 6)
  Tron (TRC20): TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t       (6 decimals)
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

import requests

USDT_BSC_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
USDT_TRON_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_BSC_DECIMALS = 18  # Binance-Peg BSC-USD uses 18 decimals
USDT_TRON_DECIMALS = 6  # TRC20 USDT uses 6 decimals
EXPECTED_AMOUNT = Decimal("1.97")
TIMEOUT_S = 15

AUTO_CHECK_ENABLED = os.environ.get("AUTO_TXID_CHECK", "false").lower() == "true"


def _verdict(status: str, reason: str, details: Optional[dict] = None) -> dict:
    return {"status": status, "reason": reason, "details": details or {}}


# ------------------------------------------------------------------- BSC
def check_txid_bsc(txid: str, expected_to: str, amount: Decimal = EXPECTED_AMOUNT) -> dict:
    """Look a BSC TXID up on BscScan's free public API and check it is a
    1.97 USDT (BEP20) transfer to our wallet. Returns a verdict dict."""
    api_key = os.environ.get("BSCSCAN_API_KEY", "").strip()
    try:
        resp = requests.get(
            "https://api.bscscan.com/api",
            params={
                "module": "account",
                "action": "tokentx",
                "contractaddress": USDT_BSC_CONTRACT,
                "address": expected_to,
                "sort": "desc",
                "apikey": api_key,
            },
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # network / HTTP / bad JSON -> unverifiable, never verified
        return _verdict("unverifiable", f"BscScan lookup failed: {exc}")

    if str(data.get("status")) != "1":
        # "0" with "No transactions found" means: no USDT transfer TO our
        # address exists at all — that is a real answer, not an error.
        msg = str(data.get("message", "")).lower()
        if "no transactions" in msg:
            return _verdict("not_found", "No USDT transfer to our BSC wallet found for this TXID.")
        return _verdict("unverifiable", f"BscScan returned an unexpected response: {data.get('message')}")

    txid_lower = txid.strip().lower()
    for tx in data.get("result") or []:
        if str(tx.get("hash", "")).lower() != txid_lower:
            continue
        try:
            value = Decimal(str(tx["value"])) / (10 ** USDT_BSC_DECIMALS)
        except Exception:
            return _verdict("unverifiable", "BscScan returned a transfer with an unreadable amount.")
        to_addr = str(tx.get("to", ""))
        if to_addr.lower() != expected_to.lower():
            return _verdict("mismatch", "TXID exists but the destination is not our BSC wallet.",
                            {"to": to_addr, "amount_usdt": str(value)})
        if value != amount:
            return _verdict("mismatch", f"Amount is {value} USDT, expected {amount} USDT.",
                            {"to": to_addr, "amount_usdt": str(value)})
        return _verdict("verified", "1.97 USDT received on BSC to our wallet.",
                        {"to": to_addr, "amount_usdt": str(value),
                         "from": tx.get("from"), "block": tx.get("blockNumber")})
    return _verdict("not_found", "TXID not found among USDT transfers to our BSC wallet.")


# ------------------------------------------------------------------ Tron
def check_txid_tron(txid: str, expected_to: str, amount: Decimal = EXPECTED_AMOUNT) -> dict:
    """Look a Tron TXID up on TronScan's free public API (no key) and check
    it is a 1.97 USDT (TRC20) transfer to our wallet."""
    try:
        resp = requests.get(
            "https://apilist.tronscanapi.com/api/transaction-info",
            params={"hash": txid.strip()},
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return _verdict("unverifiable", f"TronScan lookup failed: {exc}")

    if not data or not data.get("hash"):
        return _verdict("not_found", "TXID not found on TronScan.")
    transfers = (data.get("tokenTransferInfo") or {}).get("transfersAll") or []
    for tr in transfers:
        if str(tr.get("contract_address", "")) != USDT_TRON_CONTRACT:
            continue
        to_addr = str(tr.get("to_address", ""))
        try:
            value = Decimal(str(tr["amount_str"])) / (10 ** USDT_TRON_DECIMALS)
        except Exception:
            return _verdict("unverifiable", "TronScan returned a transfer with an unreadable amount.")
        if to_addr != expected_to:
            return _verdict("mismatch", "TXID exists but the destination is not our Tron wallet.",
                            {"to": to_addr, "amount_usdt": str(value)})
        if value != amount:
            return _verdict("mismatch", f"Amount is {value} USDT, expected {amount} USDT.",
                            {"to": to_addr, "amount_usdt": str(value)})
        return _verdict("verified", "1.97 USDT received on Tron to our wallet.",
                        {"to": to_addr, "amount_usdt": str(value),
                         "from": tr.get("from_address")})
    return _verdict("not_found", "TXID found on Tron but it is not a USDT transfer to our wallet.")


# ------------------------------------------------------------- dispatcher
def check_txid(network: str, txid: str, expected_to: str,
               amount: Decimal = EXPECTED_AMOUNT) -> dict:
    """network: 'BSC' or 'Tron'. Returns one of:
    verified / mismatch / not_found / unverifiable (see _verdict).
    Callers must treat anything other than 'verified' as NOT paid.
    Raises ValueError on an unknown network."""
    network = network.strip()
    if network == "BSC":
        return check_txid_bsc(txid, expected_to, amount)
    if network == "Tron":
        return check_txid_tron(txid, expected_to, amount)
    raise ValueError(f"Unknown network: {network!r} (expected 'BSC' or 'Tron')")


def manual_check_instructions(network: str, txid: str, expected_to: str) -> str:
    """What the human verifier checks, in the exact order."""
    explorer = (
        f"https://bscscan.com/tx/{txid}" if network == "BSC"
        else f"https://tronscan.org/#/transaction/{txid}"
    )
    return (
        f"Manual TXID check ({network}):\n"
        f"1. Open {explorer}\n"
        f"2. Confirm the transfer is USDT, amount exactly 1.97.\n"
        f"3. Confirm the destination (To) is our wallet: {expected_to}\n"
        f"4. Confirm this TXID was never used before (payments table / GET by TXID).\n"
        f"5. If all four pass, approve via POST /api/payments/verify with the admin key."
    )
