"""Pairing: let unknown senders request access with a one-time code that an admin approves.

Flow: unknown sender messages the bot -> gets a code -> admin runs ``kiro-crew pairing approve <code>``
(or ``/pair approve <code>`` in chat) -> the sender is authorised immediately, with no restart or YAML edit.

Safeguards: codes are random, unambiguous, expire after 1 hour, are reissued rather than multiplied,
replies are rate-limited per sender, and the number of pending requests is capped so a stranger
cannot flood the table.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from ..memory.db import Database

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
CODE_LEN = 8
TTL_S = 3600
NOTIFY_EVERY_S = 600
MAX_PENDING = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pairing_approved (
    channel TEXT NOT NULL, sender_id TEXT NOT NULL, approved_at REAL NOT NULL, approved_by TEXT,
    PRIMARY KEY (channel, sender_id));
CREATE TABLE IF NOT EXISTS pairing_requests (
    channel TEXT NOT NULL, sender_id TEXT NOT NULL, code TEXT NOT NULL UNIQUE, display_name TEXT,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, last_notified REAL NOT NULL,
    PRIMARY KEY (channel, sender_id));
"""


@dataclass
class PairingRequest:
    channel: str
    sender_id: str
    code: str
    display_name: str | None
    expires_at: float


class PairingStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(_SCHEMA)

    def is_approved(self, channel: str, sender_id: str) -> bool:
        return self.db.one("SELECT 1 FROM pairing_approved WHERE channel=? AND sender_id=?",
                           (channel, sender_id)) is not None

    def request(self, channel: str, sender_id: str, display_name: str | None = None) -> tuple[str, bool] | None:
        """Issue (or re-issue) a code. Returns ``(code, should_notify)``, or ``None`` if the queue is full."""
        now = time.time()
        with self.db.transaction():
            self.db.execute("DELETE FROM pairing_requests WHERE expires_at < ?", (now,))
            row = self.db.one("SELECT code, last_notified FROM pairing_requests WHERE channel=? AND sender_id=?",
                              (channel, sender_id))
            if row:
                notify = now - row["last_notified"] >= NOTIFY_EVERY_S
                if notify:
                    self.db.execute("UPDATE pairing_requests SET last_notified=? WHERE channel=? AND sender_id=?",
                                    (now, channel, sender_id))
                return row["code"], notify
            if self.db.scalar("SELECT COUNT(*) FROM pairing_requests") >= MAX_PENDING:
                return None
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
            self.db.execute("INSERT INTO pairing_requests VALUES (?,?,?,?,?,?,?)",
                            (channel, sender_id, code, display_name, now, now + TTL_S, now))
            return code, True

    def approve(self, code: str, approved_by: str = "admin") -> PairingRequest | None:
        code = code.strip().upper()
        now = time.time()
        with self.db.transaction():
            row = self.db.one("SELECT * FROM pairing_requests WHERE code=? AND expires_at>=?", (code, now))
            if row is None:
                return None
            self.db.execute("INSERT OR REPLACE INTO pairing_approved VALUES (?,?,?,?)",
                            (row["channel"], row["sender_id"], now, approved_by))
            self.db.execute("DELETE FROM pairing_requests WHERE code=?", (code,))
        return PairingRequest(row["channel"], row["sender_id"], code, row["display_name"], row["expires_at"])

    def revoke(self, channel: str, sender_id: str) -> bool:
        return self.db.execute("DELETE FROM pairing_approved WHERE channel=? AND sender_id=?",
                               (channel, sender_id)).rowcount > 0

    def pending(self) -> list[PairingRequest]:
        rows = self.db.query("SELECT * FROM pairing_requests WHERE expires_at>=? ORDER BY created_at", (time.time(),))
        return [PairingRequest(r["channel"], r["sender_id"], r["code"], r["display_name"], r["expires_at"])
                for r in rows]

    def approved(self) -> list[tuple[str, str]]:
        return [(r["channel"], r["sender_id"]) for r in
                self.db.query("SELECT channel, sender_id FROM pairing_approved ORDER BY approved_at")]
