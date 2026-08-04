import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dashboard.phone_share import (  # noqa: E402
    DRIVE_APPDATA_SCOPE,
    GoogleDriveConfig,
    HttpResponse,
    InMemoryOAuthSession,
    OAuthCredentials,
    SecureOAuthCredentialStore,
    WindowsDPAPIProtector,
)
from dashboard.phone_share.credential_store import _FILE_MAGIC  # noqa: E402


CLIENT_ID = "1234567890-securestoretest.apps.googleusercontent.com"
REDIRECT_URI = "http://127.0.0.1:8765/oauth2/callback"


class FakeProtector:
    def __init__(self, *, available=True):
        self.available = available

    def protect(self, value):
        return b"protected:" + bytes(byte ^ 0xA5 for byte in value)

    def unprotect(self, value):
        if not value.startswith(b"protected:"):
            raise OSError("tampered")
        return bytes(byte ^ 0xA5 for byte in value[len(b"protected:"):])


def credentials(*, access="access-token-value", refresh="refresh-token-value",
                expires=4600.0):
    return OAuthCredentials(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        scopes=(DRIVE_APPDATA_SCOPE,),
    )


class SecureOAuthCredentialStoreTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is Windows-only")
    def test_real_windows_dpapi_round_trip_uses_current_user_encryption(self):
        protector = WindowsDPAPIProtector()
        self.assertTrue(protector.available)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.dpapi"
            store = SecureOAuthCredentialStore(
                CLIENT_ID,
                path=path,
                protector=protector,
                clock=lambda: 1000.0,
            )
            self.assertTrue(store.save(credentials()))
            disk = path.read_bytes()
            self.assertNotIn(b"access-token-value", disk)
            self.assertNotIn(b"refresh-token-value", disk)
            self.assertEqual(store.load(), credentials())

    def test_round_trip_writes_only_protected_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "credential.dpapi"
            store = SecureOAuthCredentialStore(
                CLIENT_ID,
                path=path,
                protector=FakeProtector(),
                clock=lambda: 1000.0,
            )
            self.assertEqual(store.mode, "secure")
            self.assertTrue(store.save(credentials()))
            disk = path.read_bytes()
            self.assertTrue(disk.startswith(_FILE_MAGIC))
            self.assertNotIn(b"access-token-value", disk)
            self.assertNotIn(b"refresh-token-value", disk)
            restored = store.load()
            self.assertEqual(restored.access_token, "access-token-value")
            self.assertEqual(restored.refresh_token, "refresh-token-value")
            self.assertNotIn("access-token-value", repr(store) + repr(restored))

    def test_unavailable_protector_never_creates_plaintext_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-created" / "credential.dpapi"
            store = SecureOAuthCredentialStore(
                CLIENT_ID,
                path=path,
                protector=FakeProtector(available=False),
                clock=lambda: 1000.0,
            )
            self.assertEqual(store.mode, "memory-only")
            self.assertFalse(store.save(credentials()))
            self.assertIsNone(store.load())
            self.assertFalse(path.parent.exists())

    def test_tampered_and_wrong_client_files_are_ignored_and_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.dpapi"
            protector = FakeProtector()
            store = SecureOAuthCredentialStore(
                CLIENT_ID, path=path, protector=protector, clock=lambda: 1000.0
            )
            self.assertTrue(store.save(credentials()))
            path.write_bytes(path.read_bytes()[:-1] + b"!")
            self.assertIsNone(store.load())
            self.assertFalse(path.exists())

            self.assertTrue(store.save(credentials()))
            other = SecureOAuthCredentialStore(
                "9999999999-otherclient.apps.googleusercontent.com",
                path=path,
                protector=protector,
                clock=lambda: 1000.0,
            )
            self.assertIsNone(other.load())
            self.assertFalse(path.exists())

    def test_invalid_schema_expiry_and_duplicate_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.dpapi"
            protector = FakeProtector()
            store = SecureOAuthCredentialStore(
                CLIENT_ID, path=path, protector=protector, clock=lambda: 1000.0
            )
            self.assertFalse(store.save(credentials(expires=999999.0)))
            self.assertEqual(store.mode, "memory-only")
            self.assertFalse(path.exists())

            duplicate = (
                b'{"schema_version":1,"schema_version":1,"provider":"google-drive"}'
            )
            path.write_bytes(_FILE_MAGIC + protector.protect(duplicate))
            self.assertIsNone(store.load())
            self.assertFalse(path.exists())


class SessionPersistenceHookTests(unittest.TestCase):
    class Transport:
        def __init__(self):
            self.documents = []

        def request(self, method, url, **kwargs):
            document = self.documents.pop(0)
            return HttpResponse(200, {}, json.dumps(document).encode(), url)

    def test_refresh_notifies_store_and_snapshot_repr_redacts(self):
        transport = self.Transport()
        transport.documents.append({
            "access_token": "new-access-token",
            "expires_in": 3600,
            "scope": DRIVE_APPDATA_SCOPE,
            "token_type": "Bearer",
        })
        session = InMemoryOAuthSession(
            GoogleDriveConfig(CLIENT_ID, REDIRECT_URI),
            transport,
            clock=lambda: 1000.0,
        )
        session.set_credentials(credentials(access="old-access-token", expires=1010.0))
        notifications = []
        session.set_credentials_changed_callback(notifications.append)
        self.assertEqual(session.authorization_header(), "Bearer new-access-token")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].refresh_token, "refresh-token-value")
        snapshot = session.credentials_snapshot()
        self.assertEqual(snapshot.access_token, "new-access-token")
        self.assertNotIn("new-access-token", repr(snapshot))

    def test_terminal_clear_notifies_encrypted_store_once(self):
        session = InMemoryOAuthSession(
            GoogleDriveConfig(CLIENT_ID, REDIRECT_URI),
            self.Transport(),
            clock=lambda: 1000.0,
        )
        session.set_credentials(credentials())
        clears = []
        session.set_credentials_cleared_callback(lambda: clears.append(True))

        session.clear_credentials()
        session.clear_credentials()

        self.assertFalse(session.is_authorized)
        self.assertEqual(clears, [True])


if __name__ == "__main__":
    unittest.main()
