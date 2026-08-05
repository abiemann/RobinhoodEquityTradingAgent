import os
import sys
import unittest
import urllib.parse


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dashboard.phone_share.public_config import (  # noqa: E402
    GOOGLE_OAUTH_BROKER_URL,
)


class PhoneSharePublicConfigTests(unittest.TestCase):
    def test_release_oauth_broker_url_is_live_and_pinned(self):
        parsed = urllib.parse.urlsplit(GOOGLE_OAUTH_BROKER_URL)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "rhmra-google-oauth-broker.abiemann.workers.dev")
        self.assertEqual(parsed.path, "/oauth/token")
        self.assertEqual(parsed.query, "")
        self.assertEqual(parsed.fragment, "")
        self.assertNotIn("example", parsed.hostname)
        self.assertFalse(parsed.hostname.endswith(".invalid"))


if __name__ == "__main__":
    unittest.main()
