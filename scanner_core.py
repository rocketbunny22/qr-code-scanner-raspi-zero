"""Hardware-independent helpers for the QR scanner."""

from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import parse_qs, urlparse

RETRYABLE_STATUSES = frozenset({"offline", "bad_response", "error"})


def parse_qr_url(qr_data):
    """Extract the first company and attendee values from a badge URL."""
    parsed = urlparse(qr_data)
    params = parse_qs(parsed.query)

    return {
        "company_id": params.get("company_id", [""])[0],
        "attendee": params.get("attendee", [""])[0],
    }


def payload_fingerprint(payload):
    """Return a short non-reversible identifier suitable for operational logs."""
    return hashlib.sha256(payload).hexdigest()[:12]


def is_retryable_result(result):
    """Return whether a failed request may be submitted again safely."""
    return result.get("status") in RETRYABLE_STATUSES


class SeenPayloadCache:
    """Bounded, expiring history of QR payloads."""

    def __init__(self, max_entries, ttl_seconds):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries = OrderedDict()

    def __contains__(self, payload):
        return payload in self._entries

    def __len__(self):
        return len(self._entries)

    def add(self, payload, now):
        self._entries.pop(payload, None)
        self._entries[payload] = now
        self.prune(now)

    def discard(self, payload):
        self._entries.pop(payload, None)

    def prune(self, now):
        stale_before = now - self.ttl_seconds

        while self._entries:
            _, first_seen_at = next(iter(self._entries.items()))
            if first_seen_at >= stale_before:
                break
            self._entries.popitem(last=False)

        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


class ScanOutbox:
    """Durable queue of accepted scans awaiting a definitive API result."""

    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self._lock = threading.Lock()

        file_descriptor = os.open(
            self.database_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        os.close(file_descriptor)
        os.chmod(self.database_path, 0o600)

        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_scans (
                id INTEGER PRIMARY KEY,
                payload BLOB NOT NULL UNIQUE,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL
            )
            """
        )
        self._connection.commit()

    def close(self):
        with self._lock:
            self._connection.close()

    def enqueue(self, payload, now, initial_delay):
        """Save a payload and return its ID, or the existing ID for a duplicate."""
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO pending_scans (payload, next_attempt_at)
                VALUES (?, ?)
                ON CONFLICT(payload) DO NOTHING
                """,
                (payload, now + initial_delay),
            )
            row = self._connection.execute(
                "SELECT id FROM pending_scans WHERE payload = ?",
                (payload,),
            ).fetchone()
            self._connection.commit()
        return row[0]

    def acknowledge(self, entry_id):
        """Remove a scan after the API returns a definitive outcome."""
        with self._lock:
            self._connection.execute("DELETE FROM pending_scans WHERE id = ?", (entry_id,))
            self._connection.commit()

    def release_all(self, now):
        """Make persisted entries eligible for retry after a process restart."""
        with self._lock:
            self._connection.execute(
                "UPDATE pending_scans SET next_attempt_at = ?",
                (now,),
            )
            self._connection.commit()

    def schedule_retry(self, entry_id, now, base_delay, max_delay):
        """Apply capped exponential backoff and return the delay selected."""
        with self._lock:
            row = self._connection.execute(
                "SELECT attempts FROM pending_scans WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                return None

            attempts = row[0] + 1
            delay = min(base_delay * (2 ** (attempts - 1)), max_delay)
            self._connection.execute(
                """
                UPDATE pending_scans
                SET attempts = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (attempts, now + delay, entry_id),
            )
            self._connection.commit()
        return delay

    def claim_due(self, now, lease_seconds):
        """Lease one due scan so a background worker can submit it safely."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, payload FROM pending_scans
                WHERE next_attempt_at <= ?
                ORDER BY id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None

            self._connection.execute(
                "UPDATE pending_scans SET next_attempt_at = ? WHERE id = ?",
                (now + lease_seconds, row[0]),
            )
            self._connection.commit()
        return row

    def count(self):
        with self._lock:
            return self._connection.execute(
                "SELECT COUNT(*) FROM pending_scans"
            ).fetchone()[0]
