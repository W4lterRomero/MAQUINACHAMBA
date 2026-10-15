"""Offline validation tests; these do not certify network transport safety."""
import unittest

from backend.app.core.security import validate_url


class URLRegressionTests(unittest.TestCase):
    def test_local_and_nonpublic_addresses(self):
        for host in ("localhost", "LOCALHOST.", "a.localhost", "server.local",
                     "metadata.google.internal.", "127.0.0.1", "10.0.0.1",
                     "0.0.0.0", "100.64.0.1", "224.0.0.1", "[::1]",
                     "[::ffff:127.0.0.1]", "[fe80::1%25eth0]"):
            with self.subTest(host=host):
                self.assertFalse(validate_url("http://" + host)[0])

    def test_ambiguous_numeric_hosts(self):
        for host in ("127.1", "2130706433", "0177.0.0.1", "0x7f000001"):
            with self.subTest(host=host):
                self.assertFalse(validate_url("http://" + host)[0])

    def test_invalid_and_credential_urls(self):
        for url in ("http://[broken", "https://example.com:bad", "http://example.com:0",
                    "https://user:password@example.com", "http://example.com\\@localhost",
                    "https://exam\nple.com", "file:///tmp/example"):
            with self.subTest(url=url):
                self.assertFalse(validate_url(url)[0])

    def test_public_format_requires_further_checks(self):
        for url in ("https://example.com/job", "https://8.8.8.8/"):
            allowed, reason = validate_url(url)
            self.assertTrue(allowed)
            self.assertIn("DNS", reason)
