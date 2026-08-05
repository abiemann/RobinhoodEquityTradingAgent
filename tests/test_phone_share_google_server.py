import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE_PATH = os.path.join(ROOT, 'dashboard', 'serve.py')
SPEC = importlib.util.spec_from_file_location(
    'phone_share_google_dashboard_server', SERVE_PATH
)
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)

VALID_CLIENT_ID = (
    '1234567890-abcdefghijklmnopqrstuvwxyz.apps.googleusercontent.com'
)
VALID_CLIENT_SECRET = 'test-desktop-client-credential-value'
VALID_BROKER_URL = 'https://oauth-broker.example/oauth/token'


class FakeOAuthSession:
    instances = []
    fail_begin = False
    fail_complete = False
    failure_code = 'oauth_exchange_failed'
    fail_revoke = False
    revoke_result = True

    def __init__(self, config):
        self.config = config
        self.authorized = False
        self.completed_urls = []
        self.restored_credentials = None
        self.credentials_changed_callback = None
        self.credentials_cleared_callback = None
        self.revoke_calls = 0
        self.clear_calls = 0
        self.__class__.instances.append(self)

    @property
    def is_authorized(self):
        return self.authorized

    def begin_authorization(self):
        if self.__class__.fail_begin:
            raise SERVER.PhoneShareProviderError(
                'oauth_begin_failed', 'sensitive begin detail'
            )
        return SERVER.OAuthAuthorizationRequest(
            authorization_url='https://accounts.google.test/authorize?state=state-token',
            redirect_uri=self.config.redirect_uri,
            state='state-token',
            code_verifier='verifier-token',
        )

    def complete_authorization(self, callback_url, pending):
        self.completed_urls.append(callback_url)
        if self.__class__.fail_complete:
            raise SERVER.PhoneShareProviderError(
                self.__class__.failure_code,
                'sensitive callback detail ' + str(self.config.client_secret),
            )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(callback_url).query)
        if query.get('state') != [pending.state] or query.get('code') != ['code-token']:
            raise SERVER.PhoneShareProviderError(
                'invalid_oauth_state', 'sensitive state detail'
            )
        self.authorized = True
        credentials = object()
        if self.credentials_changed_callback is not None:
            self.credentials_changed_callback(credentials)
        return credentials

    def set_credentials(self, credentials):
        self.restored_credentials = credentials
        self.authorized = True

    def set_credentials_changed_callback(self, callback):
        self.credentials_changed_callback = callback

    def set_credentials_cleared_callback(self, callback):
        self.credentials_cleared_callback = callback

    def clear_credentials(self):
        self.clear_calls += 1
        was_authorized = self.authorized
        self.authorized = False
        if was_authorized and self.credentials_cleared_callback is not None:
            self.credentials_cleared_callback()

    def revoke_credentials(self):
        self.revoke_calls += 1
        if self.__class__.fail_revoke:
            raise SERVER.PhoneShareProviderError(
                'oauth_revoke_failed', 'private-revocation-token-detail'
            )
        return self.__class__.revoke_result


class FakeCredentialStore:
    instances = []
    loaded = None

    def __init__(self, client_id):
        self.client_id = client_id
        self.mode = 'secure'
        self.saved = []
        self.clear_calls = 0
        self.load_calls = 0
        self.__class__.instances.append(self)

    def load(self):
        self.load_calls += 1
        return self.__class__.loaded

    def save(self, credentials):
        self.saved.append(credentials)
        return True

    def clear(self):
        self.clear_calls += 1
        return True


class FakeGoogleDriveProvider:
    instances = []
    fail_operation = False
    failure_code = 'drive_unavailable'

    def __init__(self, config, session):
        self.config = config
        self.session = session
        self.put_calls = []
        self.delete_calls = []
        self.__class__.instances.append(self)

    def put_envelope(self, envelope):
        if self.__class__.fail_operation:
            if self.__class__.failure_code in {
                'google_authorization_required',
                'drive_authorization_failed',
            }:
                self.session.clear_credentials()
            raise SERVER.PhoneShareProviderError(
                self.__class__.failure_code,
                'oauth-super-secret must stay hidden',
            )
        self.put_calls.append(dict(envelope))

    def delete_envelope(self, share_id):
        if self.__class__.fail_operation:
            if self.__class__.failure_code in {
                'google_authorization_required',
                'drive_authorization_failed',
            }:
                self.session.clear_credentials()
            raise SERVER.PhoneShareProviderError(
                self.__class__.failure_code,
                'oauth-super-secret must stay hidden',
            )
        self.delete_calls.append(share_id)
        return True


class GooglePhoneShareServerTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        FakeOAuthSession.instances = []
        FakeOAuthSession.fail_begin = False
        FakeOAuthSession.fail_complete = False
        FakeOAuthSession.failure_code = 'oauth_exchange_failed'
        FakeOAuthSession.fail_revoke = False
        FakeOAuthSession.revoke_result = True
        FakeGoogleDriveProvider.instances = []
        FakeGoogleDriveProvider.fail_operation = False
        FakeGoogleDriveProvider.failure_code = 'drive_unavailable'
        FakeCredentialStore.instances = []
        FakeCredentialStore.loaded = None
        self.credentials_directory = tempfile.TemporaryDirectory()
        self.credentials_path = self.write_google_credentials(
            self.credentials_directory.name
        )
        self.session_patch = mock.patch.object(
            SERVER, 'InMemoryOAuthSession', FakeOAuthSession
        )
        self.provider_patch = mock.patch.object(
            SERVER, 'GoogleDriveProvider', FakeGoogleDriveProvider
        )
        self.store_patch = mock.patch.object(
            SERVER, 'SecureOAuthCredentialStore', FakeCredentialStore
        )
        self.public_config_patch = mock.patch.multiple(
            SERVER,
            GOOGLE_DESKTOP_CLIENT_ID=VALID_CLIENT_ID,
            GOOGLE_OAUTH_BROKER_URL=VALID_BROKER_URL,
        )
        self.session_patch.start()
        self.provider_patch.start()
        self.store_patch.start()
        self.public_config_patch.start()
        self.logs = []
        logs = self.logs

        class QuietHandler(SERVER.Handler):
            def log_message(self, format, *args):
                logs.append(format % args)

        self.server = SERVER.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.provider_patch.stop()
        self.session_patch.stop()
        self.store_patch.stop()
        self.public_config_patch.stop()
        self.credentials_directory.cleanup()
        self.environment.stop()

    @property
    def local_origin(self):
        return 'http://127.0.0.1:' + str(self.server.server_port)

    def configure_google(self, *, explicit=True):
        os.environ[SERVER.PHONE_SHARE_GOOGLE_CLIENT_ID_ENV] = VALID_CLIENT_ID
        os.environ.setdefault(
            SERVER.PHONE_SHARE_GOOGLE_CREDENTIALS_FILE_ENV,
            self.credentials_path,
        )
        if explicit:
            os.environ[SERVER.PHONE_SHARE_PROVIDER_ENV] = (
                SERVER.PHONE_SHARE_PROVIDER_GOOGLE
            )

    def configure_builtin_broker(self, *, explicit=True):
        os.environ.pop(SERVER.PHONE_SHARE_GOOGLE_CLIENT_ID_ENV, None)
        os.environ.pop(SERVER.PHONE_SHARE_GOOGLE_CREDENTIALS_FILE_ENV, None)
        if explicit:
            os.environ[SERVER.PHONE_SHARE_PROVIDER_ENV] = (
                SERVER.PHONE_SHARE_PROVIDER_GOOGLE
            )

    def write_google_credentials(
        self,
        directory,
        *,
        client_id=VALID_CLIENT_ID,
        client_secret=VALID_CLIENT_SECRET,
    ):
        path = os.path.join(directory, 'google-desktop-client.json')
        with open(path, 'w', encoding='utf-8') as credentials_file:
            json.dump(
                {
                    'installed': {
                        'client_id': client_id,
                        'client_secret': client_secret,
                        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                        'token_uri': 'https://oauth2.googleapis.com/token',
                    }
                },
                credentials_file,
            )
        return path

    def configure_cloudflare(self):
        os.environ.update({
            SERVER.PHONE_SHARE_URL_ENV: 'https://share.example',
            SERVER.PHONE_SHARE_TOKEN_ENV: 'upload-' + ('t' * 40),
            SERVER.PHONE_SHARE_ACCESS_ID_ENV: 'access-client-id',
            SERVER.PHONE_SHARE_ACCESS_SECRET_ENV: 'access-' + ('s' * 40),
        })

    def envelope(self):
        captured = datetime.now(timezone.utc).replace(microsecond=0)
        expires = captured + timedelta(hours=1)
        return {
            'schema_version': 1,
            'share_id': 'G' * 22,
            'sequence': 1,
            'captured_at': captured.isoformat(timespec='milliseconds').replace(
                '+00:00', 'Z'
            ),
            'expires_at': expires.isoformat(timespec='milliseconds').replace(
                '+00:00', 'Z'
            ),
            'iv': 'I' * 16,
            'ciphertext': 'C' * 22,
        }

    def request(self, method, path, body=None, headers=None, host=None):
        request_headers = {
            'Host': host or ('127.0.0.1:' + str(self.server.server_port)),
        }
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.server_port, timeout=2
        )
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def mutation_headers(self, *, json_body=False):
        headers = {
            'Origin': self.local_origin,
            'X-RHMRA-CSRF': SERVER.PHONE_SHARE_CSRF_TOKEN,
        }
        if json_body:
            headers['Content-Type'] = 'application/json; charset=utf-8'
        return headers

    def connect(self):
        status, _, body = self.request(
            'POST', '/api/phone-share/connect', body=b'{}',
            headers=self.mutation_headers(json_body=True),
        )
        self.assertEqual(status, 200)
        return json.loads(body)

    def complete_callback(self, *, host=None):
        return self.request(
            'GET', '/oauth2/callback?code=code-token&state=state-token', host=host
        )

    def test_google_is_default_and_cloudflare_requires_explicit_selection(self):
        self.configure_builtin_broker(explicit=False)
        status, headers, body = self.request('GET', '/api/phone-share/config')
        document = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(document['provider'], 'google-drive')
        self.assertFalse(document['connected'])
        self.assertTrue(document['desktop_credentials_configured'])
        self.assertTrue(document['oauth_configured'])
        self.assertEqual(
            document['viewer_url'],
            'https://abiemann.github.io/RobinhoodEquityTradingDashboardViewer/'
        )
        self.assertEqual(document['ttl_seconds'], 28800)
        self.assertEqual(document['csrf_token'], SERVER.PHONE_SHARE_CSRF_TOKEN)
        self.assertEqual(headers.get('Cache-Control'), 'no-store')
        self.assertNotIn(VALID_CLIENT_ID, body.decode('utf-8'))
        runtime = SERVER._google_runtime_for_server(
            self.server,
            SERVER._selected_phone_share_settings(self.server.server_port),
        )
        self.assertTrue(hasattr(runtime.session, 'set_credentials'))
        self.assertIsNone(runtime.config.client_secret)
        self.assertEqual(
            runtime.config.oauth_token_endpoint, VALID_BROKER_URL
        )

        os.environ.pop(SERVER.PHONE_SHARE_GOOGLE_CLIENT_ID_ENV, None)
        self.configure_cloudflare()
        with mock.patch.object(
            SERVER, 'GOOGLE_DESKTOP_CLIENT_ID', VALID_CLIENT_ID
        ):
            default_google = json.loads(
                self.request('GET', '/api/phone-share/config')[2]
            )
        self.assertTrue(default_google['configured'])
        self.assertEqual(
            default_google['provider'], SERVER.PHONE_SHARE_PROVIDER_GOOGLE
        )

        os.environ[SERVER.PHONE_SHARE_PROVIDER_ENV] = 'cloudflare'
        cloudflare = json.loads(
            self.request('GET', '/api/phone-share/config')[2]
        )
        self.assertTrue(cloudflare['configured'])
        self.assertEqual(cloudflare['provider'], 'cloudflare')

    def test_missing_default_google_config_is_explicit_when_bundled_fallback_absent(
        self,
    ):
        with mock.patch.object(SERVER, 'GOOGLE_DESKTOP_CLIENT_ID', ''):
            response = json.loads(
                self.request('GET', '/api/phone-share/config')[2]
            )
        self.assertEqual(
            response,
            {'configured': False, 'provider': 'google-drive'},
        )

    def test_selector_and_google_viewer_override_fail_closed(self):
        self.configure_google()
        os.environ[SERVER.PHONE_SHARE_GOOGLE_VIEWER_URL_ENV] = (
            'https://viewer.example/app/'
        )
        document = json.loads(
            self.request('GET', '/api/phone-share/config')[2]
        )
        self.assertEqual(document['viewer_url'], 'https://viewer.example/app/')

        os.environ[SERVER.PHONE_SHARE_GOOGLE_VIEWER_URL_ENV] = (
            'https://viewer.example/app/?secret=bad'
        )
        self.assertEqual(
            json.loads(self.request('GET', '/api/phone-share/config')[2]),
            {'configured': False, 'provider': 'google-drive'},
        )

        self.configure_cloudflare()
        os.environ[SERVER.PHONE_SHARE_PROVIDER_ENV] = 'unknown-provider'
        self.assertEqual(
            json.loads(self.request('GET', '/api/phone-share/config')[2]),
            {'configured': False, 'provider': 'google-drive'},
        )

    def test_connect_requires_same_origin_csrf_and_empty_body(self):
        self.configure_google()
        self.assertEqual(
            self.request('POST', '/api/phone-share/connect')[0], 403
        )
        bad_csrf = self.mutation_headers()
        bad_csrf['X-RHMRA-CSRF'] = 'wrong'
        self.assertEqual(
            self.request(
                'POST', '/api/phone-share/connect', headers=bad_csrf
            )[0],
            403,
        )
        unexpected = json.dumps({'unexpected': True}).encode('utf-8')
        status = self.request(
            'POST', '/api/phone-share/connect', body=unexpected,
            headers=self.mutation_headers(json_body=True),
        )[0]
        self.assertEqual(status, 400)
        self.assertEqual(FakeOAuthSession.instances, [])

        document = self.connect()
        self.assertTrue(document['authorization_url'].startswith('https://'))
        self.assertEqual(len(FakeOAuthSession.instances), 1)

    def test_disconnect_requires_same_origin_csrf_and_exact_empty_json(self):
        self.configure_builtin_broker()
        path = '/api/phone-share/disconnect-google'
        self.assertEqual(self.request('POST', path, body=b'{}')[0], 403)

        bad_csrf = self.mutation_headers(json_body=True)
        bad_csrf['X-RHMRA-CSRF'] = 'wrong'
        self.assertEqual(
            self.request('POST', path, body=b'{}', headers=bad_csrf)[0],
            403,
        )
        self.assertEqual(
            self.request(
                'POST', path, body=b'{}', headers=self.mutation_headers()
            )[0],
            415,
        )
        unexpected = json.dumps({'forget_pairing': True}).encode('utf-8')
        self.assertEqual(
            self.request(
                'POST', path, body=unexpected,
                headers=self.mutation_headers(json_body=True),
            )[0],
            400,
        )
        self.assertEqual(FakeOAuthSession.instances, [])

    def test_disconnect_revokes_then_clears_local_credentials_without_deleting_share(self):
        self.configure_builtin_broker()
        self.connect()
        self.assertEqual(self.complete_callback()[0], 200)
        runtime = SERVER._google_runtime_for_server(
            self.server,
            SERVER._selected_phone_share_settings(self.server.server_port),
        )
        session = runtime.session
        store = runtime.credential_store
        provider = runtime.provider

        status, _, body = self.request(
            'POST', '/api/phone-share/disconnect-google', body=b'{}',
            headers=self.mutation_headers(json_body=True),
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                'ok': True,
                'connected': False,
                'remote_revocation_confirmed': True,
            },
        )
        self.assertEqual(session.revoke_calls, 1)
        self.assertEqual(session.clear_calls, 1)
        self.assertFalse(session.is_authorized)
        self.assertGreaterEqual(store.clear_calls, 2)
        self.assertEqual(provider.delete_calls, [])
        config = json.loads(self.request('GET', '/api/phone-share/config')[2])
        self.assertFalse(config['connected'])

    def test_disconnect_remote_failure_is_honest_but_still_clears_local_credentials(self):
        self.configure_builtin_broker()
        self.connect()
        self.assertEqual(self.complete_callback()[0], 200)
        runtime = SERVER._google_runtime_for_server(
            self.server,
            SERVER._selected_phone_share_settings(self.server.server_port),
        )
        session = runtime.session
        store = runtime.credential_store
        FakeOAuthSession.fail_revoke = True

        status, _, body = self.request(
            'POST', '/api/phone-share/disconnect-google', body=b'{}',
            headers=self.mutation_headers(json_body=True),
        )
        document = json.loads(body)

        self.assertEqual(status, 200)
        self.assertTrue(document['ok'])
        self.assertFalse(document['connected'])
        self.assertFalse(document['remote_revocation_confirmed'])
        self.assertEqual(
            document['warning'], SERVER.PHONE_SHARE_GOOGLE_REVOKE_WARNING
        )
        self.assertFalse(session.is_authorized)
        self.assertEqual(session.clear_calls, 1)
        self.assertGreaterEqual(store.clear_calls, 2)
        exposed = body.decode('utf-8') + ' '.join(self.logs)
        self.assertNotIn('private-revocation-token-detail', exposed)
        self.assertIn('oauth_revoke_failed', exposed)

    def test_disconnect_is_idempotent_when_already_disconnected(self):
        self.configure_builtin_broker()
        headers = self.mutation_headers(json_body=True)
        responses = [
            self.request(
                'POST', '/api/phone-share/disconnect-google', body=b'{}',
                headers=headers,
            )
            for _ in range(2)
        ]

        for status, _, body in responses:
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body),
                {
                    'ok': True,
                    'connected': False,
                    'remote_revocation_confirmed': True,
                },
            )
        session = FakeOAuthSession.instances[0]
        store = FakeCredentialStore.instances[0]
        self.assertEqual(session.revoke_calls, 0)
        self.assertEqual(session.clear_calls, 2)
        self.assertEqual(store.clear_calls, 2)

    def test_connect_uses_builtin_broker_without_local_credentials(self):
        self.configure_builtin_broker()

        status, _, body = self.request(
            'POST', '/api/phone-share/connect', body=b'{}',
            headers=self.mutation_headers(json_body=True),
        )

        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['authorization_url'].startswith('https://'))
        self.assertEqual(len(FakeOAuthSession.instances), 1)
        config = FakeOAuthSession.instances[0].config
        self.assertIsNone(config.client_secret)
        self.assertEqual(config.oauth_token_endpoint, VALID_BROKER_URL)

    def test_server_rejects_generic_unpinned_oauth_token_endpoint(self):
        self.configure_builtin_broker()
        unpinned_config = mock.Mock(
            client_secret=None,
            oauth_token_endpoint='https://unexpected.example/oauth/token',
        )
        with mock.patch.object(
            SERVER, 'GoogleDriveConfig', return_value=unpinned_config
        ):
            settings = SERVER._google_phone_share_settings(
                self.server.server_port
            )

        self.assertIsNone(settings)

    def test_external_desktop_credentials_are_loaded_but_never_published(self):
        self.configure_google()
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_google_credentials(directory)
            os.environ[
                SERVER.PHONE_SHARE_GOOGLE_CREDENTIALS_FILE_ENV
            ] = path
            status, _, body = self.request('GET', '/api/phone-share/config')

            self.assertEqual(status, 200)
            document = json.loads(body)
            self.assertTrue(document['configured'])
            self.assertNotIn(VALID_CLIENT_SECRET, body.decode('utf-8'))
            self.assertNotIn(path, body.decode('utf-8'))
            self.assertEqual(
                FakeOAuthSession.instances[-1].config.client_secret,
                VALID_CLIENT_SECRET,
            )
            self.assertEqual(
                FakeOAuthSession.instances[-1].config.oauth_token_endpoint,
                'https://oauth2.googleapis.com/token',
            )
            self.assertNotIn(
                VALID_CLIENT_SECRET,
                repr(FakeOAuthSession.instances[-1].config),
            )

            os.environ[SERVER.PHONE_SHARE_GOOGLE_CLIENT_ID_ENV] = (
                '9999999999-differentclient.apps.googleusercontent.com'
            )
            mismatch = json.loads(
                self.request('GET', '/api/phone-share/config')[2]
            )
            self.assertEqual(
                mismatch,
                {
                    'configured': False,
                    'provider': 'google-drive',
                    'configuration_error':
                        SERVER.PHONE_SHARE_GOOGLE_CREDENTIAL_FILE_ERROR,
                },
            )
            self.assertNotIn(path, json.dumps(mismatch))
            self.assertNotIn(VALID_CLIENT_SECRET, json.dumps(mismatch))

    def test_desktop_credentials_must_match_the_bundled_client(self):
        self.configure_google()
        del os.environ[SERVER.PHONE_SHARE_GOOGLE_CLIENT_ID_ENV]
        with mock.patch.object(
            SERVER,
            'GOOGLE_DESKTOP_CLIENT_ID',
            '9999999999-differentclient.apps.googleusercontent.com',
        ):
            document = json.loads(
                self.request('GET', '/api/phone-share/config')[2]
            )

        self.assertEqual(
            document,
            {
                'configured': False,
                'provider': 'google-drive',
                'configuration_error':
                    SERVER.PHONE_SHARE_GOOGLE_CREDENTIAL_FILE_ERROR,
            },
        )
        self.assertNotIn(self.credentials_path, json.dumps(document))
        self.assertNotIn(VALID_CLIENT_SECRET, json.dumps(document))

    def test_repository_contained_desktop_credentials_are_rejected(self):
        self.configure_google()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = self.write_google_credentials(directory)
            os.environ[
                SERVER.PHONE_SHARE_GOOGLE_CREDENTIALS_FILE_ENV
            ] = path

            document = json.loads(
                self.request('GET', '/api/phone-share/config')[2]
            )
            self.assertEqual(document['configured'], False)
            self.assertEqual(document['provider'], 'google-drive')
            self.assertEqual(
                document['configuration_error'],
                SERVER.PHONE_SHARE_GOOGLE_CREDENTIAL_FILE_ERROR,
            )
            self.assertNotIn(path, json.dumps(document))
            self.assertNotIn(VALID_CLIENT_SECRET, json.dumps(document))

    @unittest.skipUnless(os.name == 'nt', 'Windows path syntax only')
    def test_windows_device_path_cannot_bypass_repository_rejection(self):
        self.configure_google()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = self.write_google_credentials(directory)
            device_path = (
                chr(92) * 2 + '?' + chr(92) + os.path.abspath(path)
            )
            os.environ[
                SERVER.PHONE_SHARE_GOOGLE_CREDENTIALS_FILE_ENV
            ] = device_path

            document = json.loads(
                self.request('GET', '/api/phone-share/config')[2]
            )

            self.assertFalse(document['configured'])
            self.assertEqual(
                document['configuration_error'],
                SERVER.PHONE_SHARE_GOOGLE_CREDENTIAL_FILE_ERROR,
            )
            self.assertNotIn(device_path, json.dumps(document))

    def test_invalid_desktop_credential_fields_report_file_error(self):
        self.configure_google()
        del os.environ[SERVER.PHONE_SHARE_GOOGLE_CLIENT_ID_ENV]
        cases = (
            ('not-a-client-id', VALID_CLIENT_SECRET),
            (VALID_CLIENT_ID, 'bad secret'),
        )
        for client_id, client_secret in cases:
            with self.subTest(
                client_id=client_id,
                client_secret=client_secret,
            ), tempfile.TemporaryDirectory() as directory:
                path = self.write_google_credentials(
                    directory,
                    client_id=client_id,
                    client_secret=client_secret,
                )
                os.environ[
                    SERVER.PHONE_SHARE_GOOGLE_CREDENTIALS_FILE_ENV
                ] = path

                document = json.loads(
                    self.request('GET', '/api/phone-share/config')[2]
                )

                self.assertFalse(document['configured'])
                self.assertEqual(
                    document['configuration_error'],
                    SERVER.PHONE_SHARE_GOOGLE_CREDENTIAL_FILE_ERROR,
                )
                self.assertNotIn(path, json.dumps(document))
                self.assertNotIn(client_secret, json.dumps(document))

    def test_saved_tokens_are_restored_in_builtin_broker_mode(self):
        self.configure_google()
        FakeCredentialStore.loaded = object()
        configured = json.loads(
            self.request('GET', '/api/phone-share/config')[2]
        )
        self.assertTrue(configured['connected'])
        self.assertEqual(FakeCredentialStore.instances[-1].load_calls, 1)

        self.configure_builtin_broker()
        broker = json.loads(
            self.request('GET', '/api/phone-share/config')[2]
        )

        self.assertTrue(broker['connected'])
        self.assertTrue(broker['desktop_credentials_configured'])
        self.assertTrue(broker['oauth_configured'])
        self.assertEqual(FakeCredentialStore.instances[-1].load_calls, 1)
        runtime = SERVER._google_runtime_for_server(
            self.server,
            SERVER._selected_phone_share_settings(self.server.server_port),
        )
        self.assertIsNone(runtime.config.client_secret)
        self.assertEqual(runtime.config.oauth_token_endpoint, VALID_BROKER_URL)

    def test_callback_is_exact_loopback_one_shot_and_safe(self):
        self.configure_google()
        self.connect()
        wrong_host = 'localhost:' + str(self.server.server_port)
        status, headers, body = self.complete_callback(host=wrong_host)
        self.assertEqual(status, 400)
        self.assertIn(b'not connected', body)
        self.assertEqual(headers.get('Cache-Control'), 'no-store')

        status, headers, body = self.complete_callback()
        self.assertEqual(status, 200)
        self.assertIn(b'Google Drive connected', body)
        self.assertEqual(headers.get('X-Frame-Options'), 'DENY')
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        session = FakeOAuthSession.instances[0]
        self.assertEqual(
            session.completed_urls,
            [self.local_origin + '/oauth2/callback?code=code-token&state=state-token'],
        )
        config = json.loads(self.request('GET', '/api/phone-share/config')[2])
        self.assertTrue(config['connected'])

        self.assertEqual(self.complete_callback()[0], 400)

    def test_missing_desktop_credential_fails_fast_with_safe_guidance(self):
        self.configure_google()
        FakeOAuthSession.fail_complete = True
        FakeOAuthSession.failure_code = 'oauth_client_credentials_rejected'
        self.connect()

        status, _, body = self.complete_callback()

        self.assertEqual(status, 400)
        body_text = body.decode('utf-8')
        self.assertIn(SERVER.PHONE_SHARE_GOOGLE_CREDENTIAL_ERROR, body_text)
        self.assertNotIn('sensitive callback detail', body_text)
        config = json.loads(self.request('GET', '/api/phone-share/config')[2])
        self.assertFalse(config['connected'])
        self.assertEqual(
            config['connection_error'],
            SERVER.PHONE_SHARE_GOOGLE_CREDENTIAL_ERROR,
        )
        self.assertNotIn(
            'sensitive callback detail',
            body_text + json.dumps(config) + ' '.join(self.logs),
        )
        self.assertNotIn(
            VALID_CLIENT_SECRET,
            body_text + json.dumps(config) + ' '.join(self.logs),
        )

        self.connect()
        refreshed = json.loads(
            self.request('GET', '/api/phone-share/config')[2]
        )
        self.assertNotIn('connection_error', refreshed)

    def test_broker_client_failure_uses_safe_update_guidance(self):
        self.configure_builtin_broker()
        FakeOAuthSession.fail_complete = True
        FakeOAuthSession.failure_code = 'oauth_client_credentials_rejected'
        self.connect()

        status, _, body = self.complete_callback()

        self.assertEqual(status, 400)
        body_text = body.decode('utf-8')
        self.assertIn(SERVER.PHONE_SHARE_GOOGLE_BROKER_ERROR, body_text)
        self.assertNotIn('sensitive callback detail', body_text)
        config = json.loads(self.request('GET', '/api/phone-share/config')[2])
        self.assertFalse(config['connected'])
        self.assertEqual(
            config['connection_error'], SERVER.PHONE_SHARE_GOOGLE_BROKER_ERROR
        )
        exposed = body_text + json.dumps(config) + ' '.join(self.logs)
        self.assertNotIn('sensitive callback detail', exposed)
        self.assertNotIn(VALID_CLIENT_SECRET, exposed)

    def test_upload_and_delete_dispatch_only_after_connection(self):
        self.configure_builtin_broker()
        expected_envelope = self.envelope()
        payload = json.dumps(expected_envelope).encode('utf-8')
        headers = self.mutation_headers(json_body=True)
        status, _, body = self.request(
            'POST', '/api/phone-share', payload, headers
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {'error': 'Google Drive is not connected'})

        self.connect()
        self.assertEqual(self.complete_callback()[0], 200)
        status, _, body = self.request(
            'POST', '/api/phone-share', payload, headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True})
        provider = FakeGoogleDriveProvider.instances[0]
        self.assertEqual(provider.put_calls, [expected_envelope])

        status, _, body = self.request(
            'DELETE', '/api/phone-share/' + expected_envelope['share_id'],
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True})
        self.assertEqual(provider.delete_calls, [expected_envelope['share_id']])

    def test_monotonic_write_conflicts_are_retryable_and_secret_free(self):
        self.configure_google()
        self.connect()
        self.assertEqual(self.complete_callback()[0], 200)
        FakeGoogleDriveProvider.fail_operation = True
        FakeGoogleDriveProvider.failure_code = 'stale_envelope'
        payload = json.dumps(self.envelope()).encode('utf-8')

        status, _, body = self.request(
            'POST', '/api/phone-share', payload,
            self.mutation_headers(json_body=True),
        )

        self.assertEqual(status, 409)
        self.assertEqual(
            json.loads(body),
            {'error': 'encrypted snapshot update conflict; retry'},
        )
        self.assertNotIn(
            'oauth-super-secret', body.decode('utf-8') + ' '.join(self.logs)
        )

    def test_provider_failures_are_generic_and_secret_free(self):
        self.configure_google()
        self.connect()
        self.assertEqual(self.complete_callback()[0], 200)
        FakeGoogleDriveProvider.fail_operation = True
        payload = json.dumps(self.envelope()).encode('utf-8')
        status, _, body = self.request(
            'POST', '/api/phone-share', payload,
            self.mutation_headers(json_body=True),
        )
        self.assertEqual(status, 502)
        self.assertEqual(
            json.loads(body), {'error': 'phone sharing service unavailable'}
        )
        exposed = body.decode('utf-8') + ' '.join(self.logs)
        self.assertNotIn('oauth-super-secret', exposed)
        self.assertIn('drive_unavailable', exposed)

    def test_rejected_broker_client_downgrades_connected_state(self):
        self.configure_builtin_broker()
        self.connect()
        self.assertEqual(self.complete_callback()[0], 200)
        FakeGoogleDriveProvider.fail_operation = True
        FakeGoogleDriveProvider.failure_code = (
            'oauth_client_credentials_rejected'
        )
        payload = json.dumps(self.envelope()).encode('utf-8')

        status, _, body = self.request(
            'POST', '/api/phone-share', payload,
            self.mutation_headers(json_body=True),
        )

        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {'error': 'Google sign-in service needs attention'},
        )
        config = json.loads(self.request('GET', '/api/phone-share/config')[2])
        self.assertFalse(config['connected'])
        self.assertEqual(
            config['connection_error'],
            SERVER.PHONE_SHARE_GOOGLE_BROKER_ERROR,
        )
        exposed = body.decode('utf-8') + json.dumps(config) + ' '.join(self.logs)
        self.assertNotIn('oauth-super-secret', exposed)
        self.assertNotIn(VALID_CLIENT_SECRET, exposed)

    def test_server_rejects_expired_and_far_future_envelopes(self):
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        expired = self.envelope()
        expired['captured_at'] = '2026-08-03T09:00:00.000Z'
        expired['expires_at'] = '2026-08-03T11:00:00.000Z'
        future = self.envelope()
        future['captured_at'] = '2026-08-03T12:03:00.000Z'
        future['expires_at'] = '2026-08-03T13:03:00.000Z'

        for value in (expired, future):
            with self.subTest(value=value):
                self.assertIsNone(
                    SERVER._validate_phone_share_envelope(
                        value, 7200, now=now
                    )
                )

    def test_revoked_google_access_returns_to_reconnect_state(self):
        self.configure_google()
        self.connect()
        self.assertEqual(self.complete_callback()[0], 200)
        runtime = SERVER._google_runtime_for_server(
            self.server,
            SERVER._selected_phone_share_settings(self.server.server_port),
        )
        store = FakeCredentialStore.instances[0]
        self.assertTrue(runtime.session.is_authorized)
        self.assertEqual(len(store.saved), 1)

        FakeGoogleDriveProvider.fail_operation = True
        FakeGoogleDriveProvider.failure_code = 'google_authorization_required'
        payload = json.dumps(self.envelope()).encode('utf-8')
        status, _, body = self.request(
            'POST', '/api/phone-share', payload,
            self.mutation_headers(json_body=True),
        )

        self.assertEqual(status, 409)
        self.assertEqual(
            json.loads(body), {'error': 'Google Drive is not connected'}
        )
        self.assertFalse(runtime.session.is_authorized)
        self.assertEqual(store.clear_calls, 1)
        config = json.loads(
            self.request('GET', '/api/phone-share/config')[2]
        )
        self.assertFalse(config['connected'])


if __name__ == '__main__':
    unittest.main()
