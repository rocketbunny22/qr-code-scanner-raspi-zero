import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from scanner_core import (
    ScanOutbox,
    SeenPayloadCache,
    is_retryable_result,
    parse_qr_url,
    payload_fingerprint,
)


class ParseQrUrlTests(unittest.TestCase):
    def test_extracts_first_company_and_attendee(self):
        result = parse_qr_url(
            "https://example.invalid/check-in?company_id=123&attendee=456"
            "&attendee=ignored"
        )

        self.assertEqual(result, {"company_id": "123", "attendee": "456"})

    def test_missing_values_are_empty(self):
        self.assertEqual(
            parse_qr_url("https://example.invalid/check-in"),
            {"company_id": "", "attendee": ""},
        )


class PayloadFingerprintTests(unittest.TestCase):
    def test_is_stable_and_does_not_contain_payload(self):
        payload = b"private attendee data"

        self.assertEqual(payload_fingerprint(payload), payload_fingerprint(payload))
        self.assertEqual(len(payload_fingerprint(payload)), 12)
        self.assertNotIn("private", payload_fingerprint(payload))


class RetryableResultTests(unittest.TestCase):
    def test_transport_and_protocol_errors_are_retryable(self):
        for status in ("offline", "bad_response", "error"):
            with self.subTest(status=status):
                self.assertTrue(is_retryable_result({"status": status}))

    def test_definitive_results_are_not_retryable(self):
        for status in ("checked_in", "not_found", "invalid", None):
            with self.subTest(status=status):
                self.assertFalse(is_retryable_result({"status": status}))


class SeenPayloadCacheTests(unittest.TestCase):
    def test_discards_oldest_entry_at_capacity(self):
        cache = SeenPayloadCache(max_entries=2, ttl_seconds=60)
        cache.add(b"one", 1)
        cache.add(b"two", 2)
        cache.add(b"three", 3)

        self.assertNotIn(b"one", cache)
        self.assertIn(b"two", cache)
        self.assertIn(b"three", cache)

    def test_expires_entries_after_ttl(self):
        cache = SeenPayloadCache(max_entries=2, ttl_seconds=10)
        cache.add(b"old", 1)
        cache.add(b"current", 8)

        cache.prune(12)

        self.assertNotIn(b"old", cache)
        self.assertIn(b"current", cache)

    def test_discard_allows_retry(self):
        cache = SeenPayloadCache(max_entries=2, ttl_seconds=10)
        cache.add(b"retry", 1)

        cache.discard(b"retry")

        self.assertNotIn(b"retry", cache)

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            SeenPayloadCache(max_entries=0, ttl_seconds=10)
        with self.assertRaises(ValueError):
            SeenPayloadCache(max_entries=1, ttl_seconds=0)


class ScanOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "outbox.sqlite3"
        self.outbox = ScanOutbox(self.database_path)

    def tearDown(self):
        self.outbox.close()
        self.temporary_directory.cleanup()

    def test_persists_entries_and_deduplicates_payloads(self):
        first_id = self.outbox.enqueue(b"badge", now=10, initial_delay=5)
        second_id = self.outbox.enqueue(b"badge", now=20, initial_delay=5)

        self.assertEqual(first_id, second_id)
        self.assertEqual(self.outbox.count(), 1)

        self.outbox.close()
        self.outbox = ScanOutbox(self.database_path)
        self.assertEqual(self.outbox.count(), 1)

    def test_claims_due_entry_and_respects_lease(self):
        entry_id = self.outbox.enqueue(b"badge", now=10, initial_delay=5)

        self.assertIsNone(self.outbox.claim_due(now=14, lease_seconds=30))
        self.assertEqual(
            self.outbox.claim_due(now=15, lease_seconds=30),
            (entry_id, b"badge"),
        )
        self.assertIsNone(self.outbox.claim_due(now=20, lease_seconds=30))

    def test_release_all_makes_persisted_entries_due(self):
        entry_id = self.outbox.enqueue(b"badge", now=10, initial_delay=120)

        self.outbox.release_all(now=20)

        self.assertEqual(
            self.outbox.claim_due(now=20, lease_seconds=30),
            (entry_id, b"badge"),
        )

    def test_retries_with_capped_exponential_backoff(self):
        entry_id = self.outbox.enqueue(b"badge", now=0, initial_delay=0)

        self.assertEqual(
            self.outbox.schedule_retry(entry_id, now=10, base_delay=5, max_delay=20),
            5,
        )
        self.assertIsNone(self.outbox.claim_due(now=14, lease_seconds=30))
        self.assertIsNotNone(self.outbox.claim_due(now=15, lease_seconds=30))
        self.assertEqual(
            self.outbox.schedule_retry(entry_id, now=20, base_delay=5, max_delay=20),
            10,
        )
        self.assertEqual(
            self.outbox.schedule_retry(entry_id, now=30, base_delay=5, max_delay=20),
            20,
        )

    def test_acknowledge_removes_entry(self):
        entry_id = self.outbox.enqueue(b"badge", now=0, initial_delay=0)

        self.outbox.acknowledge(entry_id)

        self.assertEqual(self.outbox.count(), 0)


if __name__ == "__main__":
    unittest.main()
