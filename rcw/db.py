"""SQLite connection, transactions, canonical hashing and the application audit log."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
GENESIS_HASH = "0" * 64

# Tests inject a fixed clock; everything else uses UTC wall time.
_clock: Callable[[], str] | None = None


def set_clock(fn: Callable[[], str] | None) -> None:
    global _clock
    _clock = fn


def now_iso() -> str:
    if _clock is not None:
        return _clock()
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for every hash: sorted keys, no whitespace, UTF-8 safe.

    Composite keys are always hashed as JSON *arrays* (e.g. ["2026-04","REP-A","SGD"]),
    never by joining strings, so no delimiter can make two different keys collide.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if str(db_path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction. BEGIN IMMEDIATE serialises writers (double-click close etc.)."""
    if conn.in_transaction:
        # Nested use: participate in the caller's transaction.
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ------------------------------------------------------------------ audit log

def _audit_hash(prev_hash: str, record: dict[str, Any]) -> str:
    return sha256_text(prev_hash + canonical_json(record))


def audit(conn: sqlite3.Connection, actor: str, action: str, entity_type: str, entity_id: str,
          details: dict[str, Any] | None = None) -> None:
    """Append one audit entry, chained to the previous entry's hash.

    Must be called inside the same transaction as the change it records.
    The chain makes accidental or partial edits detectable by `verify_audit_chain`;
    it is NOT tamper-proof against someone who controls the database file and
    recomputes the whole chain.
    """
    row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else GENESIS_HASH
    ts = now_iso()
    record = {
        "ts": ts, "actor": actor, "action": action, "entity_type": entity_type,
        "entity_id": str(entity_id), "details": details or {},
    }
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, entity_type, entity_id, details_json, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, actor, action, entity_type, str(entity_id), canonical_json(details or {}), prev_hash,
         _audit_hash(prev_hash, record)),
    )


def verify_audit_chain(conn: sqlite3.Connection) -> dict[str, Any]:
    prev = GENESIS_HASH
    count = 0
    for row in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
        record = {
            "ts": row["ts"], "actor": row["actor"], "action": row["action"],
            "entity_type": row["entity_type"], "entity_id": row["entity_id"],
            "details": json.loads(row["details_json"]),
        }
        if row["prev_hash"] != prev or row["hash"] != _audit_hash(prev, record):
            return {"ok": False, "entries": count, "broken_at_seq": row["seq"]}
        prev = row["hash"]
        count += 1
    return {"ok": True, "entries": count, "broken_at_seq": None, "head": prev}


def db_fingerprint(conn: sqlite3.Connection) -> str:
    """Hash of every table's full content — used by tests to prove GET pages are read-only."""
    h = hashlib.sha256()
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        h.update(t.encode())
        for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
            h.update(canonical_json(list(row)).encode())
    return h.hexdigest()
