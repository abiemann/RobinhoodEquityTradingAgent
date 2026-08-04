import http.client
import importlib.util
import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE_PATH = os.path.join(ROOT, 'dashboard', 'serve.py')
SPEC = importlib.util.spec_from_file_location('phone_share_dashboard_server', SERVE_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class FakeUpstreamResponse:
    status = 204
    body = b''

    def read(self, limit=-1):
        return self.body[:limit]


class FakeHTTPSConnection:
    instances = []
    response_status = 204
    response_body = b''

    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.requests = []
        self.closed = False
        self.__class__.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        response = FakeUpstreamResponse()
        response.status = self.__class__.response_status
        response.body = self.__class__.response_body
        return response

    def close(self):
        self.closed = True


class PhoneShareServerTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        os.environ[SERVER.PHONE_SHARE_PROVIDER_ENV] = 'cloudflare'
        self.logs = []
        logs = self.logs

        class QuietHandler(SERVER.Handler):
            def log_message(self, format, *args):
                logs.append(format % args)

        self.server = SERVER.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        FakeHTTPSConnection.instances = []
        FakeHTTPSConnection.response_status = 204
        FakeHTTPSConnection.response_body = b''

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.environment.stop()

    @property
    def local_origin(self):
        return 'http://127.0.0.1:' + str(self.server.server_port)

    def configure(self):
        os.environ.update({
            SERVER.PHONE_SHARE_PROVIDER_ENV: 'cloudflare',
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
            'share_id': 'S' * 22,
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

    def mutation_headers(self):
        return {
            'Origin': self.local_origin,
            'X-RHMRA-CSRF': SERVER.PHONE_SHARE_CSRF_TOKEN,
            'Content-Type': 'application/json; charset=utf-8',
        }

    def test_disabled_config_is_safe_and_secret_free(self):
        status, headers, body = self.request('GET', '/api/phone-share/config')
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {'configured': False, 'provider': 'cloudflare'},
        )
        self.assertEqual(headers.get('Cache-Control'), 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)

        self.configure()
        status, headers, body = self.request('GET', '/api/phone-share/config')
        document = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(document['configured'], True)
        self.assertEqual(document['viewer_url'], 'https://share.example/view')
        self.assertEqual(document['ttl_seconds'], 7200)
        self.assertEqual(document['csrf_token'], SERVER.PHONE_SHARE_CSRF_TOKEN)
        body_text = body.decode('utf-8')
        self.assertNotIn(os.environ[SERVER.PHONE_SHARE_TOKEN_ENV], body_text)
        self.assertNotIn(os.environ[SERVER.PHONE_SHARE_ACCESS_SECRET_ENV], body_text)
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_invalid_or_incomplete_config_stays_disabled(self):
        self.configure()
        os.environ[SERVER.PHONE_SHARE_URL_ENV] = 'http://share.example'
        self.assertEqual(
            json.loads(self.request('GET', '/api/phone-share/config')[2]),
            {'configured': False, 'provider': 'cloudflare'},
        )
        os.environ[SERVER.PHONE_SHARE_URL_ENV] = 'https://share.example'
        del os.environ[SERVER.PHONE_SHARE_ACCESS_SECRET_ENV]
        self.assertEqual(
            json.loads(self.request('GET', '/api/phone-share/config')[2]),
            {'configured': False, 'provider': 'cloudflare'},
        )
        self.configure()
        os.environ[SERVER.PHONE_SHARE_VIEWER_URL_ENV] = (
            'https://different.example/view'
        )
        self.assertEqual(
            json.loads(self.request('GET', '/api/phone-share/config')[2]),
            {'configured': False, 'provider': 'cloudflare'},
        )
        self.configure()
        os.environ[SERVER.PHONE_SHARE_VIEWER_URL_ENV] = (
            'https://share.example/view'
        )
        self.assertTrue(
            json.loads(
                self.request('GET', '/api/phone-share/config')[2]
            )['configured']
        )
        for invalid_viewer in (
            'https://share.example/other',
            'https://share.example/view/',
            'https://share.example/view?source=unsafe',
            'https://share.example/view#fragment',
        ):
            os.environ[SERVER.PHONE_SHARE_VIEWER_URL_ENV] = invalid_viewer
            self.assertEqual(
                json.loads(self.request('GET', '/api/phone-share/config')[2]),
                {'configured': False, 'provider': 'cloudflare'},
                invalid_viewer,
            )

    def test_mutations_require_loopback_host_same_origin_and_csrf(self):
        self.configure()
        headers = self.mutation_headers()

        status = self.request(
            'POST', '/api/phone-share', None, headers, host='example.test'
        )[0]
        self.assertEqual(status, 403)

        bad_origin = dict(headers)
        bad_origin['Origin'] = 'https://attacker.example'
        self.assertEqual(
            self.request('POST', '/api/phone-share', None, bad_origin)[0], 403
        )

        bad_token = dict(headers)
        bad_token['X-RHMRA-CSRF'] = 'wrong'
        self.assertEqual(
            self.request('POST', '/api/phone-share', None, bad_token)[0], 403
        )
        self.assertEqual(FakeHTTPSConnection.instances, [])

    def test_mutations_reject_content_type_body_size_and_schema(self):
        self.configure()
        body = json.dumps(self.envelope()).encode('utf-8')
        headers = self.mutation_headers()

        wrong_type = dict(headers)
        wrong_type['Content-Type'] = 'text/plain'
        self.assertEqual(
            self.request('POST', '/api/phone-share', None, wrong_type)[0], 415
        )

        duplicate_charset = dict(headers)
        duplicate_charset['Content-Type'] = (
            'application/json; charset=utf-8; charset=utf-8'
        )
        self.assertEqual(
            self.request(
                'POST', '/api/phone-share', None, duplicate_charset
            )[0],
            415,
        )

        too_large = dict(headers)
        too_large['Content-Length'] = str(SERVER.PHONE_SHARE_MAX_BODY_BYTES + 1)
        self.assertEqual(
            self.request('POST', '/api/phone-share', None, too_large)[0], 413
        )

        extra = self.envelope()
        extra['account_number'] = 'must-not-pass'
        self.assertEqual(
            self.request(
                'POST', '/api/phone-share', json.dumps(extra).encode(), headers
            )[0],
            400,
        )
        self.assertEqual(
            self.request('POST', '/api/phone-share', b'{', headers)[0], 400
        )
        key = json.dumps('schema_version')
        duplicate = ('{' + key + ':1,' + key + ':1}').encode('utf-8')
        status, _, response_body = self.request(
            'POST', '/api/phone-share', duplicate, headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(response_body), {'error': 'invalid JSON body'})
        self.assertEqual(FakeHTTPSConnection.instances, [])

    def test_delete_rejects_a_body_before_contacting_upstream(self):
        self.configure()
        share_id = self.envelope()['share_id']
        status, _, response_body = self.request(
            'DELETE', '/api/phone-share/' + share_id, body=None, headers={
                'Origin': self.local_origin,
                'X-RHMRA-CSRF': SERVER.PHONE_SHARE_CSRF_TOKEN,
                'Content-Length': '1',
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(response_body),
            {'error': 'DELETE request body is not allowed'},
        )
        self.assertEqual(FakeHTTPSConnection.instances, [])

    def test_envelope_bounds_fail_closed(self):
        document = self.envelope()
        document['share_id'] = 'short'
        self.assertIsNone(
            SERVER._validate_phone_share_envelope(document, 7200)
        )

        document = self.envelope()
        document['captured_at'] = document['captured_at'].replace('.000Z', 'Z')
        self.assertIsNone(
            SERVER._validate_phone_share_envelope(document, 7200)
        )

        document = self.envelope()
        captured = datetime.now(timezone.utc).replace(microsecond=0)
        document['captured_at'] = captured.isoformat(
            timespec='milliseconds'
        ).replace('+00:00', 'Z')
        document['expires_at'] = (
            captured + timedelta(seconds=7201)
        ).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        self.assertIsNone(
            SERVER._validate_phone_share_envelope(document, 7200)
        )

    def test_unconfigured_mutation_fails_without_network(self):
        status, response_headers, response_body = self.request(
            'POST', '/api/phone-share', None, self.mutation_headers()
        )
        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(response_body),
            {'error': 'phone sharing is not configured'},
        )
        self.assertNotIn('Access-Control-Allow-Origin', response_headers)
        self.assertEqual(FakeHTTPSConnection.instances, [])

    def test_forward_and_revoke_use_fixed_https_origin_and_allowlist(self):
        self.configure()
        envelope = self.envelope()
        body = json.dumps(envelope).encode('utf-8')
        with mock.patch.object(
            SERVER.http.client, 'HTTPSConnection', FakeHTTPSConnection
        ):
            with mock.patch.object(
                SERVER.ssl, 'create_default_context', return_value=object()
            ):
                status, headers, response_body = self.request(
                    'POST', '/api/phone-share', body, self.mutation_headers()
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(response_body), {'ok': True})
                self.assertNotIn('Access-Control-Allow-Origin', headers)

                share_id = envelope['share_id']
                status, _, response_body = self.request(
                    'DELETE', '/api/phone-share/' + share_id,
                    headers={
                        'Origin': self.local_origin,
                        'X-RHMRA-CSRF': SERVER.PHONE_SHARE_CSRF_TOKEN,
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(response_body), {'ok': True})

        self.assertEqual(len(FakeHTTPSConnection.instances), 2)
        upload = FakeHTTPSConnection.instances[0]
        self.assertEqual((upload.host, upload.port), ('share.example', 443))
        self.assertTrue(upload.closed)
        method, path, upstream_body, upstream_headers = upload.requests[0]
        self.assertEqual(method, 'PUT')
        self.assertEqual(path, '/api/shares/' + envelope['share_id'])
        self.assertEqual(json.loads(upstream_body), envelope)
        self.assertEqual(set(json.loads(upstream_body)), SERVER.PHONE_SHARE_FIELDS)
        self.assertEqual(
            upstream_headers['Authorization'],
            'Bearer ' + os.environ[SERVER.PHONE_SHARE_TOKEN_ENV],
        )
        self.assertEqual(
            upstream_headers['CF-Access-Client-Id'],
            os.environ[SERVER.PHONE_SHARE_ACCESS_ID_ENV],
        )
        self.assertEqual(
            upstream_headers['CF-Access-Client-Secret'],
            os.environ[SERVER.PHONE_SHARE_ACCESS_SECRET_ENV],
        )
        revoke = FakeHTTPSConnection.instances[1]
        self.assertEqual(revoke.requests[0][0:3], (
            'DELETE', '/api/shares/' + envelope['share_id'], None,
        ))

    def test_upstream_failure_is_generic_and_leaks_no_secrets(self):
        self.configure()
        upload_token = os.environ[SERVER.PHONE_SHARE_TOKEN_ENV]
        access_secret = os.environ[SERVER.PHONE_SHARE_ACCESS_SECRET_ENV]
        FakeHTTPSConnection.response_status = 503
        FakeHTTPSConnection.response_body = (
            upload_token + access_secret
        ).encode('utf-8')
        body = json.dumps(self.envelope()).encode('utf-8')
        with mock.patch.object(
            SERVER.http.client, 'HTTPSConnection', FakeHTTPSConnection
        ):
            with mock.patch.object(
                SERVER.ssl, 'create_default_context', return_value=object()
            ):
                status, _, response_body = self.request(
                    'POST', '/api/phone-share', body, self.mutation_headers()
                )
        self.assertEqual(status, 502)
        self.assertEqual(
            json.loads(response_body),
            {'error': 'phone sharing service unavailable'},
        )
        exposed = response_body.decode('utf-8') + ' '.join(self.logs)
        self.assertNotIn(upload_token, exposed)
        self.assertNotIn(access_secret, exposed)


if __name__ == '__main__':
    unittest.main()
