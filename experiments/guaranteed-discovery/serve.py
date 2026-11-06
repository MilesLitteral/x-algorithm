"""Loopback HTTP adapter for trusted publication, audience and impression feeds."""
import argparse
import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ledger import Ledger, Rejected


def handler(ledger, token):
    routes = {
        "/publish": ledger.publish,
        "/audience": ledger.replace_audience,
        "/candidates": ledger.candidates,
        "/offer": ledger.offer,
        "/confirm": ledger.confirm,
        "/report": ledger.report,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                if not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()
                ):
                    raise Rejected("Unauthorized", 401)
                action = routes.get(self.path)
                if action is None:
                    raise Rejected("Unknown route", 404)
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 1024 * 1024:
                    raise Rejected("Expected a JSON body of at most 1 MiB")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise Rejected("Expected a JSON object")
                response = action(**body)
                self.respond(200, response)
            except Rejected as exc:
                self.respond(exc.status, {"error": str(exc)})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"error": "Invalid request fields"})
            except sqlite3.OperationalError:
                self.respond(503, {"error": "Discovery storage unavailable"})

        def respond(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--max-initial", default=100, type=int)
    parser.add_argument("--author-hourly", default=3, type=int)
    args = parser.parse_args()
    token = os.environ.get("GUARANTEED_DISCOVERY_TOKEN", "")
    if not token:
        parser.error("Set GUARANTEED_DISCOVERY_TOKEN")
    os.umask(0o077)
    ledger = Ledger(args.db, args.max_initial, args.author_hourly)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler(ledger, token)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
