import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from scripts.check_secrets import scan


class SecretCheckTests(unittest.TestCase):
    def run_scan(self, source):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "source.py").write_bytes(source)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = scan([directory])
            return result, output.getvalue()

    def test_clean(self):
        self.assertEqual(self.run_scan(b"value = 1\n")[0], 0)

    def test_key_fails_without_exposing_value(self):
        secret = "sk-" + "A" * 24
        code, output = self.run_scan(('token = "' + secret + '"').encode())
        self.assertEqual(code, 1)
        self.assertNotIn(secret, output)

    def test_password_fails(self):
        code, output = self.run_scan(b'password = "synthetic-fixture"')
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic-fixture", output)

    def test_invalid_encoding_fails_closed(self):
        self.assertEqual(self.run_scan(b"\xff")[0], 2)

    def test_missing_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(scan([Path(directory) / "missing"]), 2)
