import importlib.util
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFLIGHT_PATH = os.path.join(ROOT, 'dashboard', 'phone_share_preflight.py')
SPEC = importlib.util.spec_from_file_location(
    'phone_share_preflight', PREFLIGHT_PATH
)
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self, limit=-1):
        return self.body[:limit]


class FakeConnection:
    response_status = 204
    response_body = b''
    instances = []

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
        return FakeResponse(self.response_status, self.response_body)

    def close(self):
        self.closed = True


class PhoneSharePreflightTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances = []
        FakeConnection.response_status = 204
        FakeConnection.response_body = b''
        self.settings = {
            'origin': 'https://share.example',
            'upload_token': 'upload-' + ('t' * 40),
            'access_id': 'access-client-id',
            'access_secret': 'access-' + ('s' * 40),
        }

    def test_success_proves_auth_without_creating_share(self):
        result = PREFLIGHT.run_preflight(self.settings, FakeConnection)

        self.assertTrue(result['ok'])
        connection = FakeConnection.instances[0]
        method, path, body, headers = connection.requests[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(path, '/api/preflight')
        self.assertIsNone(body)
        self.assertEqual(
            headers['Authorization'],
            'Bearer ' + self.settings['upload_token'],
        )
        self.assertEqual(
            headers['CF-Access-Client-Id'],
            self.settings['access_id'],
        )
        self.assertEqual(
            headers['CF-Access-Client-Secret'],
            self.settings['access_secret'],
        )
        self.assertTrue(connection.closed)

    def test_worker_json_error_is_reported_without_credentials(self):
        FakeConnection.response_status = 401
        FakeConnection.response_body = (
            b'{"error":"unauthorized",'
            b'"message":"Valid uploader credentials are required."}'
        )

        result = PREFLIGHT.run_preflight(self.settings, FakeConnection)

        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 401)
        self.assertEqual(result['error'], 'unauthorized')
        rendered = repr(result)
        self.assertNotIn(self.settings['upload_token'], rendered)
        self.assertNotIn(self.settings['access_secret'], rendered)

    def test_access_html_is_reduced_to_safe_diagnostic(self):
        FakeConnection.response_status = 403
        FakeConnection.response_body = b'<html>sensitive provider page</html>'

        result = PREFLIGHT.run_preflight(self.settings, FakeConnection)

        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'cloudflare_access_denied')
        self.assertNotIn('sensitive provider page', repr(result))

    def test_control_characters_in_worker_error_are_not_printable(self):
        FakeConnection.response_status = 503
        FakeConnection.response_body = (
            b'{"error":"bad\\u001bcode","message":"unsafe\\u001b[31m"}'
        )

        result = PREFLIGHT.run_preflight(self.settings, FakeConnection)

        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'non_json_worker_response')
        self.assertNotIn('unsafe', repr(result))

    def test_network_exception_does_not_echo_exception_details(self):
        class FailingConnection(FakeConnection):
            def request(self, method, path, body=None, headers=None):
                raise OSError('secret-bearing diagnostic detail')

        result = PREFLIGHT.run_preflight(self.settings, FailingConnection)

        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'network_or_tls_error')
        self.assertNotIn('secret-bearing', repr(result))
        self.assertTrue(FailingConnection.instances[-1].closed)


if __name__ == '__main__':
    unittest.main()
