#!/usr/bin/env python3
"""Safely verify the optional phone-share uploader configuration.

The check calls a dedicated data-free Worker endpoint. A 204 response proves
that Cloudflare Access, both uploader credentials, Worker configuration, and
the Durable Object binding accepted the request. No dashboard data or share
record is created, and credentials are never printed.
"""

import http.client
import json
import os
import re
import ssl
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import serve


EXPECTED_STATUS = 204
MAX_ERROR_BYTES = 4096
MAX_ERROR_TEXT = 240
ERROR_CODE_RE = re.compile(r'[a-z0-9_]{1,80}\Z')


def _safe_error(raw):
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(value, dict):
        return None, None
    code = value.get('error')
    message = value.get('message')
    if not isinstance(code, str) or not isinstance(message, str):
        return None, None
    if (not ERROR_CODE_RE.fullmatch(code) or len(message) > MAX_ERROR_TEXT
            or any(ord(character) < 32 or ord(character) > 126
                   for character in message)):
        return None, None
    return code, message


def run_preflight(settings, connection_factory=http.client.HTTPSConnection):
    """Return a secret-free diagnostic result for validated settings."""
    parsed = urllib.parse.urlsplit(settings['origin'])
    connection = connection_factory(
        parsed.hostname,
        parsed.port or 443,
        timeout=serve.PHONE_SHARE_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    headers = {
        'Accept': 'application/json',
        'Authorization': 'Bearer ' + settings['upload_token'],
        'CF-Access-Client-Id': settings['access_id'],
        'CF-Access-Client-Secret': settings['access_secret'],
        'Content-Type': 'application/json',
        'User-Agent': 'RHMRA-Phone-Share-Preflight/1',
    }
    try:
        connection.request(
            'POST',
            '/api/preflight',
            headers=headers,
        )
        response = connection.getresponse()
        status = response.status
        raw = response.read(MAX_ERROR_BYTES)
    except (OSError, TimeoutError, http.client.HTTPException):
        return {
            'ok': False,
            'status': None,
            'error': 'network_or_tls_error',
            'message': 'No usable Worker response was received.',
        }
    finally:
        connection.close()

    code, message = _safe_error(raw)
    if status == EXPECTED_STATUS and not raw:
        return {
            'ok': True,
            'status': status,
            'error': None,
            'message': 'Uploader credentials and Worker storage are ready.',
        }
    if code is None:
        if status in (301, 302, 303, 307, 308, 401, 403):
            code = 'cloudflare_access_denied'
            message = 'Cloudflare Access did not accept the uploader service token.'
        else:
            code = 'non_json_worker_response'
            message = 'The endpoint did not return the expected Worker JSON.'
    return {
        'ok': False,
        'status': status,
        'error': code,
        'message': message,
    }


def main():
    settings = serve._phone_share_settings()
    if settings is None:
        print(
            'Preflight not run: one or more RHMRA_PHONE_SHARE_* values are '
            'missing or malformed.',
            file=sys.stderr,
        )
        return 2

    result = run_preflight(settings)
    if result['ok']:
        print('Phone-share uploader preflight passed.')
        return 0

    status = 'no HTTP response' if result['status'] is None else (
        'HTTP ' + str(result['status'])
    )
    print(
        'Phone-share uploader preflight failed: '
        f"{status} — {result['error']}: {result['message']}",
        file=sys.stderr,
    )
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
