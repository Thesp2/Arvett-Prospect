import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gui.app import app


class AppRouteTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_health_endpoint_and_security_headers(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_run_rejects_malformed_requests(self):
        self.assertEqual(self.client.post("/run", data="not json").status_code, 415)
        response = self.client.post("/run", json={"vertical": "web_design"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("market", response.get_json()["error"])

    def test_run_rejects_unsupported_sources(self):
        response = self.client.post("/run", json={
            "vertical": "web_design",
            "market": "Mehsana, Gujarat, India",
            "sources": ["internal"],
        })
        self.assertEqual(response.status_code, 400)

    def test_download_kind_is_allowlisted(self):
        response = self.client.get("/download/not-a-file/unknown")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
