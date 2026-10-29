import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from leadgen.audit import _validate_public_url, classify_website


class AuditSecurityTests(unittest.TestCase):
    def test_private_and_loopback_targets_are_rejected(self):
        for url in ("http://127.0.0.1", "http://localhost", "http://10.0.0.1"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    _validate_public_url(url)

    def test_transient_failures_are_inconclusive(self):
        status, evidence = classify_website(
            "https://example.test", {"error": "TIMEOUT", "audit_notes": ["Timeout"]}
        )
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("Timeout", evidence)


if __name__ == "__main__":
    unittest.main()
