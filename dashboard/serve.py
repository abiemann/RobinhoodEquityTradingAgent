#!/usr/bin/env python3
"""Local dashboard server for the Robinhood momentum routine.

Stdlib only, matching the repo's no-install requirement. Binds to 127.0.0.1
ONLY — the files served include account activity and must never be exposed
off-machine. Serves exactly three things and refuses everything else:

  /dashboard/...       the viewer (index.html and any future assets)
  /run-reports/...     telemetry the runs publish (status + gates JSON, reports)
  /trade-ledger.csv    the append-only fill ledger

plus three conveniences so the page never has to guess filenames or mode:

  /api/index           {"status": [...], "gates": [...]} sorted filename lists
  /api/latest          {"filename": ..., "data": {...}} newest status snapshot
  /api/config          current DRY_RUN and opening-blackout settings

Optional phone sharing adds a loopback-only configuration endpoint plus
same-origin POST/DELETE proxies. The proxy sends encrypted snapshots outbound
to one fixed HTTPS origin; it never accepts inbound remote connections.

Run:   python3 dashboard/serve.py   (Windows: py -3 dashboard\\serve.py)
       then open http://127.0.0.1:8765/
       An optional first argument overrides the port: serve.py 9000

Stop:  Ctrl+C in this terminal. If it was started detached, kill it by port —
       PowerShell: Stop-Process -Id (Get-NetTCPConnection -LocalPort 8765 -State Listen).OwningProcess -Force
       macOS/Linux: lsof -ti:8765 | xargs kill
"""

import glob
import hmac
import http.client
import json
import os
import posixpath
import re
import secrets
import ssl
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PORT = 8765
PHONE_SHARE_CSRF_TOKEN = secrets.token_urlsafe(32)
PHONE_SHARE_MAX_BODY_BYTES = 393216
PHONE_SHARE_TIMEOUT_SECONDS = 10
PHONE_SHARE_DEFAULT_TTL_SECONDS = 2 * 60 * 60
PHONE_SHARE_MAX_TTL_SECONDS = 8 * 60 * 60

PHONE_SHARE_URL_ENV = 'RHMRA_PHONE_SHARE_URL'
PHONE_SHARE_TOKEN_ENV = 'RHMRA_PHONE_SHARE_UPLOAD_TOKEN'
PHONE_SHARE_VIEWER_URL_ENV = 'RHMRA_PHONE_SHARE_VIEWER_URL'
PHONE_SHARE_TTL_ENV = 'RHMRA_PHONE_SHARE_TTL_SECONDS'
PHONE_SHARE_ACCESS_ID_ENV = 'RHMRA_PHONE_SHARE_CF_CLIENT_ID'
PHONE_SHARE_ACCESS_SECRET_ENV = 'RHMRA_PHONE_SHARE_CF_CLIENT_SECRET'

PHONE_SHARE_FIELDS = frozenset({
    'schema_version', 'share_id', 'sequence', 'captured_at',
    'expires_at', 'iv', 'ciphertext',
})
PHONE_SHARE_ID_RE = re.compile(r'[A-Za-z0-9_-]{22,64}\Z')
PHONE_SHARE_IV_RE = re.compile(r'[A-Za-z0-9_-]{16}\Z')
PHONE_SHARE_CIPHERTEXT_RE = re.compile(r'[A-Za-z0-9_-]{22,349526}\Z')
PHONE_SHARE_TIMESTAMP_RE = re.compile(
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z'
)

if REPO not in sys.path:
    sys.path.insert(0, REPO)
from validate_constants import ConstantsValidationError, validate_constants_file

ALLOWED_PREFIXES = ("/dashboard/", "/run-reports/")
ALLOWED_EXACT = ("/trade-ledger.csv",)


def _clean_header_secret(value):
    '''Return a non-empty header value, rejecting injection and huge secrets.'''
    if not value or not isinstance(value, str) or len(value) > 4096:
        return None
    if chr(13) in value or chr(10) in value:
        return None
    return value


def _https_origin(value):
    '''Validate and canonicalize a fixed HTTPS origin from configuration.'''
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme.lower() != 'https' or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        return None
    hostname = parsed.hostname.lower()
    authority = f'[{hostname}]' if ':' in hostname else hostname
    if port is not None and port != 443:
        authority += f':{port}'
    return f'https://{authority}'


def _https_viewer_url(value, origin):
    '''Validate a public viewer URL on the same fixed upstream origin.'''
    candidate = value or f'{origin}/view'
    try:
        parsed = urllib.parse.urlsplit(candidate)
        origin_parts = urllib.parse.urlsplit(origin)
        port = parsed.port
        origin_port = origin_parts.port
    except ValueError:
        return None
    if (parsed.scheme.lower() != 'https' or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        return None
    if ((parsed.hostname.lower(), port or 443)
            != (origin_parts.hostname.lower(), origin_port or 443)):
        return None
    path = parsed.path or '/view'
    if path != '/view':
        return None
    return urllib.parse.urlunsplit(('https', parsed.netloc, path, '', ''))


def _phone_share_settings():
    '''Load phone-sharing configuration without ever returning its secrets.'''
    origin = _https_origin(os.environ.get(PHONE_SHARE_URL_ENV))
    upload_token = _clean_header_secret(os.environ.get(PHONE_SHARE_TOKEN_ENV))
    access_id = _clean_header_secret(os.environ.get(PHONE_SHARE_ACCESS_ID_ENV))
    access_secret = _clean_header_secret(
        os.environ.get(PHONE_SHARE_ACCESS_SECRET_ENV)
    )
    if (origin is None or upload_token is None or len(upload_token) < 32
            or access_id is None or len(access_id) < 16
            or access_secret is None or len(access_secret) < 32):
        return None
    viewer_url = _https_viewer_url(
        os.environ.get(PHONE_SHARE_VIEWER_URL_ENV), origin
    )
    if viewer_url is None:
        return None
    raw_ttl = os.environ.get(
        PHONE_SHARE_TTL_ENV, str(PHONE_SHARE_DEFAULT_TTL_SECONDS)
    )
    try:
        ttl_seconds = int(raw_ttl)
    except (TypeError, ValueError):
        return None
    if not 300 <= ttl_seconds <= PHONE_SHARE_MAX_TTL_SECONDS:
        return None
    return {
        'origin': origin,
        'viewer_url': viewer_url,
        'ttl_seconds': ttl_seconds,
        'upload_token': upload_token,
        'access_id': access_id,
        'access_secret': access_secret,
    }


def _phone_share_public_config():
    '''Expose capability metadata and the ephemeral CSRF token, never secrets.'''
    settings = _phone_share_settings()
    if settings is None:
        return {'configured': False}
    return {
        'configured': True,
        'csrf_token': PHONE_SHARE_CSRF_TOKEN,
        'viewer_url': settings['viewer_url'],
        'ttl_seconds': settings['ttl_seconds'],
    }


def _parse_phone_share_timestamp(value):
    if not isinstance(value, str) or not PHONE_SHARE_TIMESTAMP_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:
        return None
    return parsed if parsed.tzinfo == timezone.utc else None


def _validate_phone_share_envelope(document, ttl_seconds):
    '''Return a safe, exact encrypted envelope or None on any schema error.'''
    if not isinstance(document, dict) or set(document) != PHONE_SHARE_FIELDS:
        return None
    if document.get('schema_version') != 1:
        return None
    share_id = document.get('share_id')
    if not isinstance(share_id, str) or not PHONE_SHARE_ID_RE.fullmatch(share_id):
        return None
    sequence = document.get('sequence')
    if (isinstance(sequence, bool) or not isinstance(sequence, int)
            or not 1 <= sequence <= 9007199254740991):
        return None
    iv = document.get('iv')
    ciphertext = document.get('ciphertext')
    if not isinstance(iv, str) or not PHONE_SHARE_IV_RE.fullmatch(iv):
        return None
    if (not isinstance(ciphertext, str)
            or not PHONE_SHARE_CIPHERTEXT_RE.fullmatch(ciphertext)):
        return None
    captured_at = _parse_phone_share_timestamp(document.get('captured_at'))
    expires_at = _parse_phone_share_timestamp(document.get('expires_at'))
    if captured_at is None or expires_at is None or expires_at <= captured_at:
        return None
    if (expires_at - captured_at).total_seconds() > ttl_seconds:
        return None
    return {
        'schema_version': 1,
        'share_id': share_id,
        'sequence': sequence,
        'captured_at': document['captured_at'],
        'expires_at': document['expires_at'],
        'iv': iv,
        'ciphertext': ciphertext,
    }


def _phone_share_upstream(method, settings, share_id, envelope=None):
    '''Send one bounded request to the fixed HTTPS origin without redirects.'''
    parsed = urllib.parse.urlsplit(settings['origin'])
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port or 443,
        timeout=PHONE_SHARE_TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    headers = {
        'Accept': 'application/json',
        'Authorization': 'Bearer ' + settings['upload_token'],
        'CF-Access-Client-Id': settings['access_id'],
        'CF-Access-Client-Secret': settings['access_secret'],
        'User-Agent': 'RHMRA-Dashboard/1',
    }
    body = None
    if envelope is not None:
        body = json.dumps(
            envelope, separators=(',', ':'), ensure_ascii=True
        ).encode('ascii')
        headers['Content-Type'] = 'application/json'
    quoted_id = urllib.parse.quote(share_id, safe='')
    path = '/api/shares/' + quoted_id
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response.read(4096)
        if not 200 <= status < 300:
            raise OSError('phone-sharing upstream rejected request')
    finally:
        connection.close()


def _json_object_no_duplicates(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError('duplicate JSON key')
        document[key] = value
    return document


def _reject_json_constant(value):
    raise ValueError('non-finite JSON number')


def _reports(pattern):
    files = glob.glob(os.path.join(REPO, "run-reports", pattern))
    return sorted(os.path.basename(f) for f in files)


def _dashboard_config():
    """Return only dashboard-safe values from the validated configuration."""
    try:
        validated = validate_constants_file(os.path.join(REPO, "constants.md"))
    except ConstantsValidationError as exc:
        return {
            "dry_run": None,
            "no_buy_first_minutes": None,
            "error": exc.errors[0],
        }
    return {
        "dry_run": validated.values["DRY_RUN"],
        "no_buy_first_minutes": validated.values["NO_BUY_FIRST_MINUTES"],
    }


def _canonical_request_path(request_path):
    """Decode and normalize a URL path before authorizing it.

    SimpleHTTPRequestHandler does this same one-pass decoding and POSIX-path
    normalization when it maps a request to disk.  The whitelist must see the
    same path; checking the raw URL lets /dashboard/%2e%2e/ escape it.
    """
    path = request_path.split("?", 1)[0].split("#", 1)[0]
    try:
        path = urllib.parse.unquote(path, errors="surrogatepass")
    except UnicodeDecodeError:
        path = urllib.parse.unquote(path)
    if "\x00" in path or any(part in (".", "..") or "\\" in part
                         for part in path.split("/")):
        return None
    trailing_slash = path.rstrip().endswith("/")
    path = posixpath.normpath(path)
    if trailing_slash and path != "/":
        path += "/"
    return path


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=REPO, **kwargs)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_error(self, status, code):
        self._json({'error': code}, status=status)

    def _same_origin(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host')
        if not origin or not host:
            return False
        try:
            source = urllib.parse.urlsplit(origin)
            target = urllib.parse.urlsplit('//' + host)
            source_port = source.port or 80
            target_port = target.port or 80
        except ValueError:
            return False
        return (
            source.scheme.lower() == 'http'
            and source.hostname is not None
            and target.hostname is not None
            and source.hostname.lower() == target.hostname.lower()
            and source_port == target_port
            and source.username is None
            and source.password is None
            and source.path in ('', '/')
            and not source.query
            and not source.fragment
        )

    def _authorize_phone_share(self):
        if not self._same_origin():
            self._api_error(403, 'same-origin request required')
            return False
        supplied = self.headers.get('X-RHMRA-CSRF') or ''
        if not hmac.compare_digest(supplied, PHONE_SHARE_CSRF_TOKEN):
            self._api_error(403, 'invalid request token')
            return False
        return True

    def _read_phone_share_json(self):
        if self.headers.get('Transfer-Encoding'):
            self._api_error(400, 'transfer encoding is not supported')
            return False, None
        content_type = self.headers.get('Content-Type') or ''
        media_parts = [part.strip().lower() for part in content_type.split(';')]
        parameters = media_parts[1:]
        if (not media_parts or media_parts[0] != 'application/json'
                or len(parameters) > 1
                or (parameters and parameters[0] != 'charset=utf-8')):
            self._api_error(415, 'application/json is required')
            return False, None
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,7}', lengths[0]):
            self._api_error(411, 'a valid Content-Length is required')
            return False, None
        length = int(lengths[0])
        if length == 0:
            self._api_error(400, 'JSON body is required')
            return False, None
        if length > PHONE_SHARE_MAX_BODY_BYTES:
            self._api_error(413, 'encrypted snapshot is too large')
            return False, None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._api_error(400, 'incomplete request body')
            return False, None
        try:
            document = json.loads(
                raw.decode('utf-8'),
                object_pairs_hook=_json_object_no_duplicates,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError):
            self._api_error(400, 'invalid JSON body')
            return False, None
        return True, document

    def _guard(self):
        """Returns the request path, or None having already sent an error.

        Rejects anything whose Host header is not loopback: a browser's
        same-origin policy does NOT protect a localhost server from a page that
        resolves its own domain to 127.0.0.1 (DNS rebinding), and this server
        reads brokerage account data. Also enforces the path whitelist, which
        do_GET and do_HEAD must BOTH honour — do_HEAD is inherited and would
        otherwise serve headers for any file in the repo."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            self.send_error(403, "non-loopback Host header")
            return None
        path = _canonical_request_path(self.path)
        if path is None:
            self.send_error(403, "path traversal")
            return None
        if not path.startswith("/"):
            self.send_error(400, "invalid request path")
            return None
        # Leave self.path untouched: SimpleHTTPRequestHandler performs the
        # same single decode when mapping to disk. Replacing it here would make
        # the superclass decode a second time and reintroduce traversal via
        # double-encoded dot segments.
        return path

    def do_HEAD(self):
        path = self._guard()
        if path is None:
            return
        if path.startswith(ALLOWED_PREFIXES) or path in ALLOWED_EXACT:
            return super().do_HEAD()
        self.send_error(403, "not served")

    def do_GET(self):
        path = self._guard()
        if path is None:
            return
        if path == '/api/phone-share/config':
            return self._json(_phone_share_public_config())
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard/index.html")
            self.end_headers()
            return
        if path == "/api/index":
            return self._json({"status": _reports("rhmra-status-*.json"),
                               "gates": _reports("rhmra-gates-*.json")})
        if path == "/api/config":
            return self._json(_dashboard_config())
        if path == "/api/latest":
            names = _reports("rhmra-status-*.json")
            if not names:
                return self._json({"filename": None, "data": None})
            newest = names[-1]  # timestamps in names sort chronologically
            try:
                with open(os.path.join(REPO, "run-reports", newest), encoding="utf-8") as f:
                    return self._json({"filename": newest, "data": json.load(f)})
            except (OSError, ValueError) as e:
                return self._json({"filename": newest, "error": str(e)}, status=500)
        if path.startswith(ALLOWED_PREFIXES) or path in ALLOWED_EXACT:
            return super().do_GET()
        self.send_error(403, "not served")

    def do_POST(self):
        path = self._guard()
        if path is None:
            return
        if path != '/api/phone-share':
            self.send_error(403, 'not served')
            return
        if not self._authorize_phone_share():
            return
        settings = _phone_share_settings()
        if settings is None:
            self._api_error(503, 'phone sharing is not configured')
            return
        ok, document = self._read_phone_share_json()
        if not ok:
            return
        envelope = _validate_phone_share_envelope(
            document, settings['ttl_seconds']
        )
        if envelope is None:
            self._api_error(400, 'invalid encrypted snapshot')
            return
        try:
            _phone_share_upstream(
                'PUT', settings, envelope['share_id'], envelope
            )
        except (OSError, TimeoutError, http.client.HTTPException):
            self.log_error('phone-sharing upstream unavailable')
            self._api_error(502, 'phone sharing service unavailable')
            return
        self._json({'ok': True})

    def do_DELETE(self):
        path = self._guard()
        if path is None:
            return
        prefix = '/api/phone-share/'
        if not path.startswith(prefix):
            self.send_error(403, 'not served')
            return
        if not self._authorize_phone_share():
            return
        share_id = path[len(prefix):]
        if not PHONE_SHARE_ID_RE.fullmatch(share_id):
            self._api_error(400, 'invalid share identifier')
            return
        if self.headers.get('Transfer-Encoding'):
            self._api_error(400, 'DELETE request body is not allowed')
            return
        lengths = self.headers.get_all('Content-Length') or []
        if lengths and (len(lengths) != 1 or lengths[0] != '0'):
            self._api_error(400, 'DELETE request body is not allowed')
            return
        settings = _phone_share_settings()
        if settings is None:
            self._api_error(503, 'phone sharing is not configured')
            return
        try:
            _phone_share_upstream('DELETE', settings, share_id)
        except (OSError, TimeoutError, http.client.HTTPException):
            self.log_error('phone-sharing upstream unavailable')
            self._api_error(502, 'phone sharing service unavailable')
            return
        self._json({'ok': True})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard: http://127.0.0.1:{port}/  (serving {REPO}, localhost only, Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
