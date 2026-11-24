"""Validate supabase/schema.sql by loading it into in-memory SQLite after
stripping Postgres-only bits (plpgsql function, triggers, COMMENT ON,
now() -> datetime('now')). This checks table/column/constraint shape,
NOT Postgres itself — no Postgres server is available locally, so the
schema must still be applied once in the Supabase SQL Editor before
launch (see README). Also proves the TXID UNIQUE and amount=1.97
constraints actually fire.

Usage: python validate_schema_sqlite.py   (from server/ or anywhere)
"""
import re
import sqlite3
import sys
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def translated_sql() -> str:
    sql = SCHEMA.read_text()
    sql2 = re.sub(r"create or replace function.*?^\$\$;", "", sql, flags=re.S | re.M)
    assert "plpgsql" not in sql2
    lines = []
    for ln in sql2.splitlines():
        if ln.strip().lower().startswith("comment on"):
            continue
        idx = ln.find("--")
        if idx != -1:
            ln = ln[:idx]
        lines.append(ln)
    sql2 = "\n".join(lines)
    sql2 = re.sub(r"drop trigger.*?;", "", sql2, flags=re.S)
    sql2 = re.sub(r"create trigger.*?;", "", sql2, flags=re.S)
    sql2 = sql2.replace("public.", "")
    sql2 = sql2.replace("bigint generated always as identity", "INTEGER")
    sql2 = sql2.replace("timestamptz", "TEXT").replace("numeric(10, 2)", "REAL")
    sql2 = sql2.replace("default now()", "default (datetime('now'))")
    return sql2


def main() -> int:
    con = sqlite3.connect(":memory:")
    con.executescript(translated_sql())
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    assert tables == ["payments", "settings", "signals", "users"], tables
    con.execute("INSERT INTO users (telegram_id) VALUES (1)")
    con.execute("INSERT INTO payments (user_id, network, amount, txid) VALUES (1,'BSC',1.97,'0xDUP')")
    try:
        con.execute("INSERT INTO payments (user_id, network, amount, txid) VALUES (1,'BSC',1.97,'0xDUP')")
        print("FAIL: duplicate TXID allowed")
        return 1
    except sqlite3.IntegrityError:
        pass
    try:
        con.execute("INSERT INTO payments (user_id, network, amount, txid) VALUES (1,'BSC',2.00,'0xOTHER')")
        print("FAIL: wrong amount allowed")
        return 1
    except sqlite3.IntegrityError:
        pass
    print("schema.sql OK: tables", tables, "| TXID UNIQUE + amount=1.97 constraints enforced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
