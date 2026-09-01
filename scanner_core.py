"""Hardware-independent helpers for the QR scanner."""

from collections import OrderedDict
import hashlib
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
