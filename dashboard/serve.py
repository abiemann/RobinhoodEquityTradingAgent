#!/usr/bin/env python3
"""Local dashboard server for the Robinhood momentum routine.

Stdlib only, matching the repo's no-install requirement. Binds to 127.0.0.1
ONLY — the files served include account activity and must never be exposed
off-machine. Serves exactly three things and refuses everything else:

  /dashboard/...       the viewer (index.html and any future assets)
  /run-reports/...     telemetry the runs publish (status + gates JSON, reports)
  /trade-ledger.csv    the append-only fill ledger

plus two conveniences so the page never has to guess filenames:

  /api/index           {"status": [...], "gates": [...]} sorted filename lists
  /api/latest          {"filename": ..., "data": {...}} newest status snapshot

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
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

ALLOWED_PREFIXES = ("/dashboard/", "/run-reports/")
ALLOWED_EXACT = ("/trade-ledger.csv",)


def _reports(pattern):
    files = glob.glob(os.path.join(REPO, "run-reports", pattern))
    return sorted(os.path.basename(f) for f in files)


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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard/index.html")
            self.end_headers()
            return
        if path == "/api/index":
            return self._json({"status": _reports("rhmra-status-*.json"),
                               "gates": _reports("rhmra-gates-*.json")})
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


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard: http://127.0.0.1:{PORT}/  (serving {REPO}, localhost only, Ctrl+C to stop)")
    server.serve_forever()
