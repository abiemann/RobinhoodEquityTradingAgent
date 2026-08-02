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

Run:   python3 dashboard/serve.py   (Windows: py -3 dashboard\\serve.py)
       then open http://127.0.0.1:8765/
       An optional first argument overrides the port: serve.py 9000

Stop:  Ctrl+C in this terminal. If it was started detached, kill it by port —
       PowerShell: Stop-Process -Id (Get-NetTCPConnection -LocalPort 8765 -State Listen).OwningProcess -Force
       macOS/Linux: lsof -ti:8765 | xargs kill
"""

import glob
import json
import os
import posixpath
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PORT = 8765

if REPO not in sys.path:
    sys.path.insert(0, REPO)
from validate_constants import ConstantsValidationError, validate_constants_file

ALLOWED_PREFIXES = ("/dashboard/", "/run-reports/")
ALLOWED_EXACT = ("/trade-ledger.csv",)


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
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard: http://127.0.0.1:{port}/  (serving {REPO}, localhost only, Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
