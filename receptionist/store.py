"""SQLite storage for bookings, messages and call reports. One file, no server to run."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    code TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL,
    slot_start TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    call_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    reason TEXT NOT NULL,
    for_agent TEXT,
    call_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    caller TEXT,
    ended_reason TEXT,
    summary TEXT,
    structured TEXT,
    transcript TEXT,
    recording_url TEXT,
    cost REAL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- bookings ----
    def private_slot_taken(self, listing_id: str, slot_start: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM bookings WHERE listing_id=? AND slot_start=? AND kind='private'",
                (listing_id, slot_start),
            ).fetchone()
        return row is not None

    def existing_booking(self, listing_id: str, slot_start: str, phone: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT code FROM bookings WHERE listing_id=? AND slot_start=? AND phone=?",
                (listing_id, slot_start, phone),
            ).fetchone()
        return row["code"] if row else None

    def add_booking(self, listing_id, slot_start, kind, name, phone, email=None, call_id=None) -> str:
        code = "HR" + "".join(secrets.choice("ACDEFHJKLMNPQRTUVWXY3479") for _ in range(4))
        with self._conn() as c:
            c.execute(
                "INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?,?)",
                (code, listing_id, slot_start, kind, name, phone, email, call_id, _now()),
            )
        return code

    def bookings_for_phone(self, phone: str, listing_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM bookings WHERE phone=?" + (" AND listing_id=?" if listing_id else "") + " ORDER BY slot_start"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, (phone, listing_id) if listing_id else (phone,))]

    def cancel_booking(self, code: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM bookings WHERE code=?", (code,))

    def bookings(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM bookings ORDER BY slot_start")]

    # ---- messages ----
    def add_message(self, name, phone, reason, for_agent=None, call_id=None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages (name, phone, reason, for_agent, call_id, created_at) VALUES (?,?,?,?,?,?)",
                (name, phone, reason, for_agent, call_id, _now()),
            )
            return cur.lastrowid

    def caller_history(self, phone: str, exclude_call_id: str | None = None) -> dict | None:
        """Most recent name and contact for this phone number from an earlier call, if any."""
        with self._conn() as c:
            b = c.execute(
                "SELECT name, listing_id, slot_start, kind, created_at FROM bookings WHERE phone=? AND IFNULL(call_id,'')<>IFNULL(?,'') ORDER BY created_at DESC LIMIT 1",
                (phone, exclude_call_id),
            ).fetchone()
            m = c.execute(
                "SELECT name, reason, created_at FROM messages WHERE phone=? AND IFNULL(call_id,'')<>IFNULL(?,'') ORDER BY created_at DESC LIMIT 1",
                (phone, exclude_call_id),
            ).fetchone()
        if not b and not m:
            return None
        latest = max([r for r in (b, m) if r], key=lambda r: r["created_at"])
        return {"name": latest["name"], "booking": dict(b) if b else None, "message": dict(m) if m else None}

    def messages_for_call(self, call_id: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM messages WHERE call_id=?", (call_id,))]

    def messages(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM messages ORDER BY id DESC")]

    # ---- calls ----
    def save_call(self, call_id, caller, ended_reason, summary, structured, transcript, recording_url, cost) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO calls VALUES (?,?,?,?,?,?,?,?,?)",
                (call_id, caller, ended_reason, summary, json.dumps(structured or {}), transcript, recording_url, cost, _now()),
            )

    def calls(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM calls ORDER BY created_at DESC")]
