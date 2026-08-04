import base64
import hashlib
import json
import os
import sys
import unittest
import urllib.parse
from datetime import datetime, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dashboard.phone_share import (  # noqa: E402
    DRIVE_APPDATA_SCOPE,
    GoogleDriveConfig,
    GoogleDriveProvider,
    HttpResponse,
    InMemoryOAuthSession,
    OAuthCredentials,
    PhoneShareProviderError,
    phone_share_filename,
    validate_envelope,
)
from dashboard.phone_share.google_drive import (  # noqa: E402
    GOOGLE_DRIVE_FILES_ENDPOINT,
    GOOGLE_DRIVE_UPLOAD_ENDPOINT,
    GOOGLE_TOKEN_ENDPOINT,
)


CLIENT_ID = "1234567890-rhmraphonetest.apps.googleusercontent.com"
CLIENT_SECRET = "test-desktop-client-credential-value"
REDIRECT_URI = "http://127.0.0.1:8765/oauth2/callback"
SHARE_ID = "S" * 22
FILE_NAME = "rhmra-phone-v2-" + SHARE_ID + ".json"
FILE_ID = "DriveFile_123456789"


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def envelope(sequence=1):
    return {
        "schema_version": 1,
        "share_id": SHARE_ID,
        "sequence": sequence,
        "captured_at": "2026-08-03T12:00:00.000Z",
        "expires_at": "2026-08-03T14:00:00.000Z",
        "iv": b64url(b"I" * 12),
        "ciphertext": b64url(b"encrypted-payload"),
    }


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.responses = []

    def queue(self, status, document=None, body=None, url=None, headers=None):
        if body is None:
            body = b"" if document is None else json.dumps(document).encode("utf-8")
        self.responses.append((status, body, url, headers or {}))

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        status, body, final_url, headers = self.responses.pop(0)
        return HttpResponse(status, headers, body, final_url or url)


class GoogleDriveConfigTests(unittest.TestCase):
    def test_accepts_strict_loopback_desktop_config(self):
        config = GoogleDriveConfig(
            CLIENT_ID, REDIRECT_URI, client_secret=CLIENT_SECRET
        )
        self.assertEqual(config.client_id, CLIENT_ID)
        self.assertEqual(config.redirect_uri, REDIRECT_URI)
        self.assertEqual(config.client_secret, CLIENT_SECRET)
        self.assertNotIn(CLIENT_SECRET, repr(config))

    def test_preserves_legacy_positional_timeout_argument(self):
        config = GoogleDriveConfig(CLIENT_ID, REDIRECT_URI, 7)

        self.assertEqual(config.request_timeout_seconds, 7)
        self.assertIsNone(config.client_secret)

    def test_rejects_unsafe_or_ambiguous_config(self):
        invalid = [
            ("not-a-client", REDIRECT_URI),
            (CLIENT_ID + "\r\nX: bad", REDIRECT_URI),
            (CLIENT_ID, "https://127.0.0.1:8765/oauth2/callback"),
            (CLIENT_ID, "http://localhost:8765/oauth2/callback"),
            (CLIENT_ID, "http://127.0.0.1/oauth2/callback"),
            (CLIENT_ID, "http://127.0.0.1:8765/oauth2/callback?x=1"),
            (CLIENT_ID, "http://user@127.0.0.1:8765/oauth2/callback"),
        ]
        for client_id, redirect_uri in invalid:
            with self.subTest(client_id=client_id, redirect_uri=redirect_uri):
                with self.assertRaises(PhoneShareProviderError):
                    GoogleDriveConfig(client_id, redirect_uri)

        for kwargs in (
            {"client_secret": ""},
            {"client_secret": "bad secret"},
            {"client_secret": "bad\r\nsecret"},
            {"request_timeout_seconds": 0},
            {"request_timeout_seconds": True},
            {"max_response_bytes": 1024},
            {"refresh_leeway_seconds": 301},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PhoneShareProviderError):
                    GoogleDriveConfig(CLIENT_ID, REDIRECT_URI, **kwargs)


class OAuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.config = GoogleDriveConfig(
            CLIENT_ID, REDIRECT_URI, client_secret=CLIENT_SECRET
        )
        self.transport = FakeTransport()
        values = [b"A" * 32, b"B" * 64]
        self.session = InMemoryOAuthSession(
            self.config,
            self.transport,
            clock=lambda: 1000.0,
            random_bytes=lambda length: values.pop(0),
        )

    def token_document(self, **overrides):
        value = {
            "access_token": "access-token-value",
            "refresh_token": "refresh-token-value",
            "expires_in": 3600,
            "scope": DRIVE_APPDATA_SCOPE,
            "token_type": "Bearer",
        }
        value.update(overrides)
        return value

    def test_pkce_authorization_request_is_complete_and_redacted(self):
        pending = self.session.begin_authorization("person@example.com")
        parsed = urllib.parse.urlsplit(pending.authorization_url)
        query = urllib.parse.parse_qs(parsed.query)
        expected_challenge = b64url(
            hashlib.sha256(pending.code_verifier.encode("ascii")).digest()
        )
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["scope"], [DRIVE_APPDATA_SCOPE])
        self.assertEqual(query["state"], [pending.state])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [expected_challenge])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertNotIn(CLIENT_SECRET, pending.authorization_url)
        self.assertNotIn(pending.state, repr(pending))
        self.assertNotIn(pending.code_verifier, repr(pending))

    def test_complete_authorization_validates_state_and_exchanges_code(self):
        pending = self.session.begin_authorization()
        self.transport.queue(200, self.token_document())
        callback = REDIRECT_URI + "?" + urllib.parse.urlencode(
            {"code": "authorization-code", "state": pending.state}
        )
        credentials = self.session.complete_authorization(callback, pending)
        self.assertEqual(credentials.expires_at, 4600.0)
        self.assertEqual(credentials.scopes, (DRIVE_APPDATA_SCOPE,))
        self.assertNotIn("access-token-value", repr(credentials))
        self.assertNotIn("refresh-token-value", repr(credentials))

        method, url, request = self.transport.requests[0]
        form = urllib.parse.parse_qs(request["body"].decode("ascii"))
        self.assertEqual((method, url), ("POST", GOOGLE_TOKEN_ENDPOINT))
        self.assertEqual(form["code"], ["authorization-code"])
        self.assertEqual(form["code_verifier"], [pending.code_verifier])
        self.assertEqual(form["client_id"], [CLIENT_ID])
        self.assertEqual(form["client_secret"], [CLIENT_SECRET])

    def test_state_mismatch_duplicate_state_and_replay_fail_before_network(self):
        pending = self.session.begin_authorization()
        callbacks = [
            REDIRECT_URI + "?code=authorization-code&state=wrong",
            REDIRECT_URI
            + "?code=authorization-code&state="
            + pending.state
            + "&state=again",
        ]
        for callback in callbacks:
            with self.subTest(callback=callback):
                # Each failed completion consumes its one-time pending state.
                with self.assertRaises(PhoneShareProviderError) as caught:
                    self.session.complete_authorization(callback, pending)
                self.assertIn(caught.exception.code, {"invalid_oauth_state"})
                self.assertEqual(self.transport.requests, [])
                if callback is not callbacks[-1]:
                    values = [b"C" * 32, b"D" * 64]
                    self.session._random_bytes = lambda length: values.pop(0)
                    pending = self.session.begin_authorization()

        with self.assertRaises(PhoneShareProviderError) as caught:
            self.session.complete_authorization(callbacks[-1], pending)
        self.assertEqual(caught.exception.code, "invalid_oauth_state")

    def test_malformed_callback_query_fails_safely_before_network(self):
        pending = self.session.begin_authorization()
        callback = REDIRECT_URI + '?not-a-valid-query-field'

        with self.assertRaises(PhoneShareProviderError) as caught:
            self.session.complete_authorization(callback, pending)

        self.assertEqual(caught.exception.code, 'invalid_oauth_callback')
        self.assertEqual(self.transport.requests, [])

    def test_authorize_uses_injected_browser_and_receiver(self):
        self.transport.queue(200, self.token_document())
        opened = []

        def receive(redirect_uri):
            pending_state = urllib.parse.parse_qs(
                urllib.parse.urlsplit(opened[0]).query
            )["state"][0]
            return redirect_uri + "?code=authorization-code&state=" + pending_state

        credentials = self.session.authorize(opened.append, receive)
        self.assertTrue(self.session.is_authorized)
        self.assertEqual(credentials.token_type, "Bearer")

    def test_refreshes_expiring_access_token_without_losing_refresh_token(self):
        self.session.set_credentials(
            OAuthCredentials(
                access_token="old-access-token",
                refresh_token="kept-refresh-token",
                expires_at=1010.0,
                scopes=(DRIVE_APPDATA_SCOPE,),
            )
        )
        self.transport.queue(
            200,
            {
                "access_token": "new-access-token",
                "expires_in": 3600,
                "scope": DRIVE_APPDATA_SCOPE,
                "token_type": "Bearer",
            },
        )
        self.assertEqual(self.session.authorization_header(), "Bearer new-access-token")
        form = urllib.parse.parse_qs(
            self.transport.requests[0][2]["body"].decode("ascii")
        )
        self.assertEqual(form["refresh_token"], ["kept-refresh-token"])
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["client_secret"], [CLIENT_SECRET])

    def test_revoked_refresh_token_clears_credentials_and_notifies_store(self):
        self.session.set_credentials(
            OAuthCredentials(
                access_token="old-access-token",
                refresh_token="revoked-refresh-token",
                expires_at=1010.0,
                scopes=(DRIVE_APPDATA_SCOPE,),
            )
        )
        cleared = []
        self.session.set_credentials_cleared_callback(
            lambda: cleared.append(True)
        )
        self.transport.queue(
            400,
            {
                "error": "invalid_grant",
                "error_description": "revoked-refresh-token private detail",
            },
        )

        with self.assertRaises(PhoneShareProviderError) as caught:
            self.session.authorization_header()

        self.assertEqual(caught.exception.code, "google_authorization_required")
        self.assertFalse(self.session.is_authorized)
        self.assertEqual(cleared, [True])
        self.assertNotIn("revoked-refresh-token", repr(caught.exception))

    def test_transient_refresh_failure_preserves_saved_credentials(self):
        self.session.set_credentials(
            OAuthCredentials(
                access_token="old-access-token",
                refresh_token="kept-refresh-token",
                expires_at=1010.0,
                scopes=(DRIVE_APPDATA_SCOPE,),
            )
        )
        cleared = []
        self.session.set_credentials_cleared_callback(
            lambda: cleared.append(True)
        )
        self.transport.queue(503, body=b'{"error":"temporarily_unavailable"}')

        with self.assertRaises(PhoneShareProviderError) as caught:
            self.session.authorization_header()

        self.assertEqual(caught.exception.code, "oauth_refresh_failed")
        self.assertTrue(self.session.is_authorized)
        self.assertEqual(cleared, [])

    def test_upstream_error_is_safe_and_does_not_echo_secrets(self):
        pending = self.session.begin_authorization()
        secret_body = b'{"error":"authorization-code access-token-value"}'
        self.transport.queue(400, body=secret_body)
        callback = REDIRECT_URI + "?code=authorization-code&state=" + pending.state
        with self.assertRaises(PhoneShareProviderError) as caught:
            self.session.complete_authorization(callback, pending)
        rendered = str(caught.exception) + repr(caught.exception)
        self.assertNotIn("authorization-code", rendered)
        self.assertNotIn("access-token-value", rendered)

    def test_malformed_upstream_error_fields_fail_safely(self):
        documents = (
            {
                "error": [],
                "error_description": "client_secret is missing.",
            },
            {
                "error": "invalid_request",
                "error_description": [],
            },
        )
        for index, document in enumerate(documents):
            with self.subTest(document=document):
                transport = FakeTransport()
                values = [
                    bytes([67 + index]) * 32,
                    bytes([69 + index]) * 64,
                ]
                session = InMemoryOAuthSession(
                    self.config,
                    transport,
                    clock=lambda: 1000.0,
                    random_bytes=lambda length: values.pop(0),
                )
                pending = session.begin_authorization()
                transport.queue(400, document)
                callback = (
                    REDIRECT_URI
                    + "?code=authorization-code&state="
                    + pending.state
                )

                with self.assertRaises(PhoneShareProviderError) as caught:
                    session.complete_authorization(callback, pending)

                self.assertEqual(caught.exception.code, "oauth_exchange_failed")
                self.assertNotIn(
                    "client_secret",
                    str(caught.exception) + repr(caught.exception),
                )

    def test_missing_desktop_credential_is_classified_without_echoing_body(self):
        config = GoogleDriveConfig(CLIENT_ID, REDIRECT_URI)
        values = [b"C" * 32, b"D" * 64]
        session = InMemoryOAuthSession(
            config,
            self.transport,
            clock=lambda: 1000.0,
            random_bytes=lambda length: values.pop(0),
        )
        pending = session.begin_authorization()
        self.transport.queue(
            400,
            {
                "error": "invalid_request",
                "error_description": "client_secret is missing.",
                "private_detail": "authorization-code access-token-value",
            },
        )
        callback = REDIRECT_URI + "?code=authorization-code&state=" + pending.state

        with self.assertRaises(PhoneShareProviderError) as caught:
            session.complete_authorization(callback, pending)

        self.assertEqual(
            caught.exception.code, "oauth_client_credentials_rejected"
        )
        rendered = str(caught.exception) + repr(caught.exception)
        self.assertNotIn("authorization-code", rendered)
        self.assertNotIn("access-token-value", rendered)
        form = urllib.parse.parse_qs(
            self.transport.requests[-1][2]["body"].decode("ascii")
        )
        self.assertNotIn("client_secret", form)

    def test_rejected_client_credential_on_refresh_clears_stale_session(self):
        self.session.set_credentials(
            OAuthCredentials(
                access_token="old-access-token",
                refresh_token="kept-refresh-token",
                expires_at=1010.0,
                scopes=(DRIVE_APPDATA_SCOPE,),
            )
        )
        cleared = []
        self.session.set_credentials_cleared_callback(
            lambda: cleared.append(True)
        )
        self.transport.queue(
            401,
            {
                "error": "invalid_client",
                "error_description": "private upstream credential detail",
            },
        )

        with self.assertRaises(PhoneShareProviderError) as caught:
            self.session.authorization_header()

        self.assertEqual(
            caught.exception.code, "oauth_client_credentials_rejected"
        )
        self.assertFalse(self.session.is_authorized)
        self.assertEqual(cleared, [True])
        self.assertNotIn(
            "private upstream credential detail",
            str(caught.exception) + repr(caught.exception),
        )


class EnvelopeTests(unittest.TestCase):
    def test_valid_envelope_is_copied_without_transformation(self):
        value = envelope()
        result = validate_envelope(value)
        self.assertEqual(result, value)
        self.assertIsNot(result, value)
        self.assertEqual(phone_share_filename(SHARE_ID), FILE_NAME)

    def test_rejects_schema_types_encoding_and_lifetime_errors(self):
        mutations = [
            {"extra": True},
            {"schema_version": 2},
            {"share_id": "short"},
            {"sequence": True},
            {"sequence": 0},
            {"captured_at": "2026-08-03T12:00:00Z"},
            {"expires_at": "2026-08-03T21:00:00.000Z"},
            {"iv": "A" * 15},
            {"ciphertext": "*" * 22},
            {"ciphertext": b64url(b"too-short")},
        ]
        for mutation in mutations:
            value = envelope()
            value.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(PhoneShareProviderError):
                    validate_envelope(value)

    def test_rejects_expired_and_far_future_envelopes(self):
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        expired = envelope()
        expired["captured_at"] = "2026-08-03T09:00:00.000Z"
        expired["expires_at"] = "2026-08-03T11:00:00.000Z"
        future = envelope()
        future["captured_at"] = "2026-08-03T12:03:00.000Z"
        future["expires_at"] = "2026-08-03T14:03:00.000Z"

        for value in (expired, future):
            with self.subTest(value=value):
                with self.assertRaises(PhoneShareProviderError):
                    validate_envelope(value, now=now)


class GoogleDriveProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = GoogleDriveConfig(CLIENT_ID, REDIRECT_URI)
        self.transport = FakeTransport()
        self.session = InMemoryOAuthSession(
            self.config, self.transport, clock=lambda: 1000.0
        )
        self.session.set_credentials(
            OAuthCredentials(
                access_token="access-token-value",
                refresh_token="refresh-token-value",
                expires_at=9999.0,
                scopes=(DRIVE_APPDATA_SCOPE,),
            )
        )
        self.provider = GoogleDriveProvider(
            self.config,
            self.session,
            self.transport,
            random_bytes=lambda length: b"R" * length,
            clock=lambda: datetime(
                2026, 8, 3, 12, 0, tzinfo=timezone.utc
            ),
        )

    def metadata(self, **overrides):
        value = {
            "id": FILE_ID,
            "name": FILE_NAME,
            "modifiedTime": "2026-08-03T12:00:01.000Z",
            "size": "200",
        }
        value.update(overrides)
        return value

    def queue_listing(self, files=None, **extra):
        document = {"files": files or []}
        document.update(extra)
        self.transport.queue(200, document)

    def assert_auth_header(self, index=0):
        headers = self.transport.requests[index][2]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer access-token-value")

    def test_exact_name_listing_is_limited_to_appdatafolder(self):
        self.queue_listing([self.metadata()])
        found = self.provider.list_file(SHARE_ID)
        self.assertEqual(found.file_id, FILE_ID)
        method, url, request = self.transport.requests[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(method, "GET")
        self.assertEqual(query["spaces"], ["appDataFolder"])
        self.assertEqual(query["pageSize"], ["2"])
        self.assertEqual(
            query["q"],
            ["'appDataFolder' in parents and name = '" + FILE_NAME + "' and trashed = false"],
        )
        self.assert_auth_header()

    def test_duplicate_exact_name_fails_closed(self):
        self.queue_listing([self.metadata(), self.metadata(id="OtherFile_123456")])
        with self.assertRaises(PhoneShareProviderError) as caught:
            self.provider.list_file(SHARE_ID)
        self.assertEqual(caught.exception.code, "duplicate_drive_file")

    def test_put_creates_multipart_file_with_exact_name_and_parent(self):
        self.queue_listing()
        self.transport.queue(200, self.metadata())
        self.queue_listing([self.metadata()])
        created = self.provider.put_envelope(envelope())
        self.assertEqual(created.name, FILE_NAME)
        method, url, request = self.transport.requests[1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.startswith(GOOGLE_DRIVE_UPLOAD_ENDPOINT + "?"))
        self.assertIn("uploadType=multipart", url)
        self.assertIn(b'"name":"' + FILE_NAME.encode("ascii") + b'"', request["body"])
        self.assertIn(b'"parents":["appDataFolder"]', request["body"])
        self.assertIn(
            json.dumps(envelope(), separators=(",", ":")).encode("ascii"),
            request["body"],
        )
        self.assertTrue(request["headers"]["Content-Type"].startswith("multipart/related"))

    def test_create_race_preserves_newer_monotonic_envelope(self):
        created_metadata = self.metadata(id="DriveFile_LowerSequence")
        newer_metadata = self.metadata(id="DriveFile_HigherSequence")
        self.queue_listing()
        self.transport.queue(200, created_metadata)
        self.queue_listing([created_metadata, newer_metadata])
        self.transport.queue(200, envelope(sequence=1))
        self.transport.queue(200, envelope(sequence=2))
        self.transport.queue(204, body=b"")
        self.queue_listing([newer_metadata])
        self.transport.queue(200, envelope(sequence=2))

        with self.assertRaises(PhoneShareProviderError) as caught:
            self.provider.put_envelope(envelope(sequence=1))

        self.assertEqual(caught.exception.code, "stale_envelope")
        delete_requests = [
            request for request in self.transport.requests if request[0] == "DELETE"
        ]
        self.assertEqual(
            delete_requests[0][1],
            GOOGLE_DRIVE_FILES_ENDPOINT + "/DriveFile_LowerSequence",
        )

    def test_create_race_uses_stable_id_tiebreaker_for_identical_envelopes(self):
        created_metadata = self.metadata(id="DriveFile_Z_Created")
        canonical_metadata = self.metadata(id="DriveFile_A_Existing")
        self.queue_listing()
        self.transport.queue(200, created_metadata)
        self.queue_listing([created_metadata, canonical_metadata])
        self.transport.queue(200, envelope(sequence=1))
        self.transport.queue(200, envelope(sequence=1))
        self.transport.queue(204, body=b"")
        self.queue_listing([canonical_metadata])
        self.transport.queue(200, envelope(sequence=1))

        result = self.provider.put_envelope(envelope(sequence=1))

        self.assertEqual(result.file_id, "DriveFile_A_Existing")
        delete_requests = [
            request for request in self.transport.requests if request[0] == "DELETE"
        ]
        self.assertEqual(
            delete_requests[0][1],
            GOOGLE_DRIVE_FILES_ENDPOINT + "/DriveFile_Z_Created",
        )

    def test_put_updates_only_the_exact_file_id(self):
        self.queue_listing([self.metadata()])
        self.transport.queue(
            200, envelope(sequence=1), headers={"etag": '"version-1"'}
        )
        self.transport.queue(200, self.metadata(size="201"))
        updated = self.provider.put_envelope(envelope(sequence=2))
        self.assertEqual(updated.size, 201)
        method, url, request = self.transport.requests[2]
        self.assertEqual(method, "PATCH")
        self.assertEqual(urllib.parse.urlsplit(url).path, "/upload/drive/v3/files/" + FILE_ID)
        self.assertEqual(
            json.loads(request["body"]), envelope(sequence=2)
        )
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["headers"]["If-Match"], '"version-1"')

    def test_put_is_idempotent_for_an_identical_sequence(self):
        self.queue_listing([self.metadata()])
        self.transport.queue(200, envelope(sequence=2))

        result = self.provider.put_envelope(envelope(sequence=2))

        self.assertEqual(result.file_id, FILE_ID)
        self.assertEqual(
            [request[0] for request in self.transport.requests], ["GET", "GET"]
        )

    def test_put_rejects_rollback_and_same_sequence_conflict(self):
        self.queue_listing([self.metadata()])
        self.transport.queue(200, envelope(sequence=3))
        with self.assertRaises(PhoneShareProviderError) as caught:
            self.provider.put_envelope(envelope(sequence=2))
        self.assertEqual(caught.exception.code, "stale_envelope")

        self.transport.requests.clear()
        current = envelope(sequence=3)
        conflicting = envelope(sequence=3)
        conflicting["ciphertext"] = b64url(b"different-encrypted-payload")
        self.queue_listing([self.metadata()])
        self.transport.queue(200, current)
        with self.assertRaises(PhoneShareProviderError) as caught:
            self.provider.put_envelope(conflicting)
        self.assertEqual(caught.exception.code, "envelope_conflict")
        self.assertEqual(
            [request[0] for request in self.transport.requests], ["GET", "GET"]
        )

    def test_put_retries_etag_conflict_then_rechecks_monotonicity(self):
        incoming = envelope(sequence=4)
        self.queue_listing([self.metadata()])
        self.transport.queue(
            200, envelope(sequence=1), headers={"etag": '"version-1"'}
        )
        self.transport.queue(412, body=b"")
        self.queue_listing([self.metadata()])
        self.transport.queue(
            200, envelope(sequence=3), headers={"etag": '"version-3"'}
        )
        self.transport.queue(200, self.metadata(size="204"))

        result = self.provider.put_envelope(incoming)

        self.assertEqual(result.size, 204)
        patch_requests = [
            request for request in self.transport.requests if request[0] == "PATCH"
        ]
        self.assertEqual(len(patch_requests), 2)
        self.assertEqual(
            [request[2]["headers"]["If-Match"] for request in patch_requests],
            ['"version-1"', '"version-3"'],
        )

    def test_get_validates_opaque_envelope_and_share_binding(self):
        self.queue_listing([self.metadata()])
        self.transport.queue(200, envelope())
        result = self.provider.get_envelope(SHARE_ID)
        self.assertEqual(result, envelope())
        method, url, unused = self.transport.requests[1]
        self.assertEqual(method, "GET")
        self.assertEqual(url, GOOGLE_DRIVE_FILES_ENDPOINT + "/" + FILE_ID + "?alt=media")

        self.transport.requests.clear()
        self.queue_listing([self.metadata()])
        wrong = envelope()
        wrong["share_id"] = "Z" * 22
        self.transport.queue(200, wrong)
        with self.assertRaises(PhoneShareProviderError) as caught:
            self.provider.get_envelope(SHARE_ID)
        self.assertEqual(caught.exception.code, "drive_share_mismatch")

    def test_delete_is_exact_idempotent_and_rejects_redirect(self):
        self.queue_listing()
        self.assertFalse(self.provider.delete_envelope(SHARE_ID))

        self.queue_listing([self.metadata()])
        self.transport.queue(204, body=b"")
        self.assertTrue(self.provider.delete_envelope(SHARE_ID))
        self.assertEqual(
            self.transport.requests[-1][:2],
            ("DELETE", GOOGLE_DRIVE_FILES_ENDPOINT + "/" + FILE_ID),
        )

        self.transport.requests.clear()
        redirect_url = GOOGLE_DRIVE_FILES_ENDPOINT + "?elsewhere=true"
        self.transport.queue(200, {"files": []}, url=redirect_url)
        with self.assertRaises(PhoneShareProviderError) as caught:
            self.provider.list_file(SHARE_ID)
        self.assertEqual(caught.exception.code, "redirect_rejected")

    def test_drive_error_body_and_token_are_never_exposed(self):
        self.transport.queue(
            503,
            body=b'{"error":"access-token-value private upstream detail"}',
        )
        with self.assertRaises(PhoneShareProviderError) as caught:
            self.provider.list_file(SHARE_ID)
        rendered = str(caught.exception) + repr(caught.exception)
        self.assertEqual(caught.exception.code, "drive_unavailable")
        self.assertNotIn("access-token-value", rendered)
        self.assertNotIn("private upstream detail", rendered)
        self.assertTrue(self.session.is_authorized)

    def test_drive_authorization_rejection_clears_credentials(self):
        cleared = []
        self.session.set_credentials_cleared_callback(
            lambda: cleared.append(True)
        )
        self.transport.queue(
            401,
            body=b'{"error":"access-token-value private upstream detail"}',
        )

        with self.assertRaises(PhoneShareProviderError) as caught:
            self.provider.list_file(SHARE_ID)

        self.assertEqual(caught.exception.code, "google_authorization_required")
        self.assertFalse(self.session.is_authorized)
        self.assertEqual(cleared, [True])
        rendered = str(caught.exception) + repr(caught.exception)
        self.assertNotIn("access-token-value", rendered)
        self.assertNotIn("private upstream detail", rendered)


if __name__ == "__main__":
    unittest.main()
