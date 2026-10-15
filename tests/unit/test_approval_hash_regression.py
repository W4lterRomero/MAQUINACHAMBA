"""Offline regression tests for immutable approval package framing."""
import hashlib
import unittest

from backend.app.core.security import hash_content, verify_content_hash


class ApprovalHashRegressionTests(unittest.TestCase):
    def test_field_boundaries(self):
        self.assertNotEqual(hash_content("ab", "c"), hash_content("a", "bc"))

    def test_empty_fields(self):
        self.assertNotEqual(hash_content(), hash_content(""))
        self.assertNotEqual(hash_content("a", ""), hash_content("", "a"))

    def test_order(self):
        self.assertNotEqual(hash_content("cv", "message"), hash_content("message", "cv"))

    def test_unicode_round_trip_and_change(self):
        digest = hash_content("Currículum", "Mensaje")
        self.assertTrue(verify_content_hash(digest, "Currículum", "Mensaje"))
        self.assertFalse(verify_content_hash(digest, "Currículum", "Otro mensaje"))

    def test_legacy_approval_requires_new_review(self):
        legacy = hashlib.sha256(b"cvmessage").hexdigest()
        self.assertFalse(verify_content_hash(legacy, "cv", "message"))
