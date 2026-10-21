"""User-launched loopback companion. Internal bridge is not a remote endpoint."""
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from .contracts import QUERY, validate
from .engine import Engine
from .mcp import call
from .policy import StorageError

ASSETS = Path(__file__).parent / "web"
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def secret_matches(value, expected):
    return isinstance(value, str) and value.isascii() and hmac.compare_digest(value, expected)


class Companion(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, engine=None, synthetic_grant_id=None):
        self.engine = engine or Engine()
        # Only the demo launcher supplies its freshly generated fixture grant.
        # This immutable binding is checked in the trusted local executor, not
        # in the descriptor, widget, model arguments or tunnel caller metadata.
        self.synthetic_grant_id = synthetic_grant_id
        self.secret = secrets.token_hex(32)
        self.pairing = secrets.token_hex(16)
        self.cookies = set()
        self.pair_attempts = 0
        self.cookie_lock = threading.Lock()
        self.export_preview = None
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_port}"
        self.host = f"127.0.0.1:{self.server_port}"

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def descriptor(self):
        return {"url": self.url, "secret": self.secret, "pid": os.getpid()}

    def service_actions(self):
        with self.engine.lock:
            if self.engine.grant and self.engine.grant.expires <= time.monotonic():
                self.engine.release()
            self.prune_export_preview()

    def prune_export_preview(self):
        """Purge unapproved bytes when their local snapshot binding ends."""
        with self.engine.lock:
            preview = self.export_preview
            if preview is None:
                return
            grant, scan = self.engine.grant, self.engine.scan
            if (not grant or not scan or grant.grant_id != preview["grantId"]
                    or scan["scanId"] != preview["scanId"]
                    or scan["scanVersion"] != preview["scanVersion"]
                    or grant.expires <= time.monotonic()
                    or preview["deadline"] <= time.time()):
                self.export_preview = None


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Never log paths, bodies, headers, tokens or names.

    def respond(self, status, body, content_type="application/json", cookie=None):
        # Covers paired and private bridge lifecycle operations before a
        # release/rescan response, plus rejected stale approval paths.
        self.server.prune_export_preview()
        payload = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        if cookie:
            self.send_header("Set-Cookie", f"storage_session={cookie}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(payload)

    def boundary(self):
        return self.headers.get("Host") == self.server.host and self.headers.get("Origin") in (None, self.server.url)

    def paired(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie.get("storage_session") and cookie["storage_session"].value in self.server.cookies
        except Exception:
            return False

    def do_GET(self):
        if not self.boundary():
            self.respond(403, {"error": "Local origin required."})
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path in assets:
            name, content_type = assets[self.path]
            self.respond(200, (ASSETS / name).read_bytes(), content_type)
        else:
            self.respond(404, {"error": "Not found."})

    def do_POST(self):
        if not self.boundary():
            self.respond(403, {"error": "Local origin required."})
            return
        if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
            self.respond(415, {"error": "JSON required."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                raise ValueError()
            args = json.loads(self.rfile.read(size))
            if not isinstance(args, dict):
                raise ValueError()
        except Exception:
            self.respond(400, {"error": "Invalid request."})
            return
        if self.path in ("/bridge", "/chatgpt-bridge"):
            authorization = self.headers.get("Authorization", "")
            if self.headers.get("Origin") is not None or not secret_matches(authorization, "Bearer " + self.server.secret):
                self.respond(403, {"error": "Session authorization required."})
            elif set(args) != {"name", "arguments"} or not isinstance(args["name"], str):
                self.respond(400, {"error": "Invalid tool request."})
            else:
                engine = self.server.engine
                if self.path == "/chatgpt-bridge":
                    with engine.lock:
                        if not self.server.synthetic_grant_id or not engine.grant or engine.grant.grant_id != self.server.synthetic_grant_id:
                            error = {"ok": False, "error": {"code": "NOT_AUTHORIZED", "message": "ChatGPT route requires the current generated demo fixture. Live metadata is disabled pending trusted caller validation.", "retryable": False}}
                            result = {"structuredContent": error, "content": [{"type": "text", "text": json.dumps(error)}], "isError": True}
                        else:
                            from .apps import call_apps
                            result = call_apps(engine, args["name"], args["arguments"])
                else:
                    result = call(engine, args["name"], args["arguments"], self.server.url)
                self.respond(200, result)
            return
        if self.headers.get("Origin") != self.server.url or self.headers.get("X-Storage-Request") != "local-ui":
            self.respond(403, {"error": "Local UI required."})
            return
        if self.path == "/api/pair":
            with self.server.cookie_lock:
                self.server.pair_attempts += 1
                valid = self.server.pair_attempts <= 20 and set(args) == {"code"} and secret_matches(args["code"], self.server.pairing)
                if not valid:
                    self.respond(403, {"error": "Pairing failed. Restart after repeated failures."})
                    return
                cookie = secrets.token_hex(32)
                self.server.cookies.add(cookie)
            self.respond(200, {"paired": True}, cookie=cookie)
            return
        if not self.paired():
            self.respond(401, {"error": "Pair this browser using the local terminal code."})
            return
        try:
            engine = self.server.engine
            if self.path == "/api/state" and not args:
                result = engine.local_state()
            elif self.path == "/api/select" and set(args) == {"path", "disclosureAccepted"} and args["disclosureAccepted"] is True:
                self.server.export_preview = None
                result = engine.select(args["path"])
            elif self.path == "/api/query":
                validate(args, QUERY)
                result = engine.query(args, True)
            elif self.path == "/api/tool" and set(args) == {"name", "arguments"} and isinstance(args["name"], str):
                result = call(engine, args["name"], args["arguments"], self.server.url)["structuredContent"]
            else:
                raise StorageError("INVALID_INPUT", "Unknown request.")
            self.respond(200, result)
        except StorageError as exc:
            self.respond(400, {"error": exc.message, "code": exc.code})
        except Exception:
            self.respond(400, {"error": "Request failed without changing permissions."})

    def do_OPTIONS(self):
        self.respond(405, {"error": "Cross-origin requests are unavailable."})
