"""A narrow reviewer-only HTTP broker carried over a Unix domain socket."""

from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
from pathlib import Path
import socketserver
import stat
import threading


MAX_REVIEW_BYTES = 256_000


class _UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    def __init__(self, path, app, token):
        self.server_address = str(path)
        self.app = app
        self.token = token.encode("utf-8")
        # Keep the boundary fixed while allowing the application to select a
        # durable bounded review window in a later scheduler integration.
        self.review_input_provider = app.review_input_provider
        self.proposal_submitter = app.proposal_submitter
        super().__init__(self.server_address, _BrokerHandler)

    def server_bind(self):
        Path(self.server_address).parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chmod(Path(self.server_address).parent, 0o750)
        if os.path.lexists(self.server_address):
            if not stat.S_ISSOCK(os.lstat(self.server_address).st_mode):
                raise ValueError("reviewer broker path exists and is not a socket")
            os.unlink(self.server_address)
        super().server_bind()
        os.chmod(self.server_address, 0o660)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(5)
        return request, client_address


class _BrokerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        return

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        value = self.headers.get("Authorization", "")
        try:
            candidate = value[7:].encode("ascii") if value.startswith("Bearer ") else b""
        except UnicodeEncodeError:
            return False
        return bool(candidate and hmac.compare_digest(candidate, self.server.token))

    def do_GET(self):
        if self.path == "/api/reviewer/health":
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
            else:
                self._reply(200, self.app.service.scheduler.health_backoff_seconds("reviewer", "openclaw"))
        elif self.path != "/api/review-input":
            self._reply(404, {"error": "not found"})
        elif not self._authorized():
            self._reply(401, {"error": "unauthorized"})
        else:
            try:
                self._reply(200, self.server.review_input_provider())
            except Exception:
                self._reply(503, {"error": "review input unavailable"})

    def do_POST(self):
        if not self._authorized():
            self.close_connection = True
            self._reply(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            self._reply(400, {"error": "invalid content length"})
            return
        if length < 0 or length > MAX_REVIEW_BYTES:
            self.close_connection = True
            self._reply(413, {"error": "review body exceeds broker limit"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete review body")
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            path = self.path.split("?", 1)[0]
            service = self.app.service
            if path == "/api/reviewer/jobs/claim":
                if payload:
                    raise ValueError("claim does not accept fields")
                result = {"job": service.claim_review_job()}
            elif path == "/api/reviewer/health":
                if set(payload) != {"failure_kind"} or payload["failure_kind"] not in {
                        "quota", "authentication", "unavailable", "invalid_output", "protocol"}:
                    raise ValueError("invalid typed reviewer health report")
                import uuid
                health = service.scheduler.record_failure("reviewer", "openclaw", payload["failure_kind"], uuid.uuid4().hex)
                result = {"recorded": True, **health}
            elif path.startswith("/api/reviewer/jobs/"):
                parts = path.split("/")
                if len(parts) != 6:
                    self._reply(404, {"error": "not found"})
                    return
                run_id, action = parts[4], parts[5]
                if action == "complete" and set(payload) == {"lease_token", "proposals"}:
                    result = service.complete_review_job(run_id, payload["lease_token"], payload["proposals"])
                elif action == "fail" and set(payload) == {"lease_token", "failure_kind"}:
                    service.fail_review_job(run_id, payload["lease_token"], payload["failure_kind"])
                    result = {"recorded": True}
                else:
                    raise ValueError("invalid reviewer job action payload")
            elif path == "/api/reviews" and payload:
                result = self.server.proposal_submitter(payload)
            else:
                self._reply(404, {"error": "not found"})
                return
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": str(exc)[:300]})
        except Exception:
            self._reply(503, {"error": "review unavailable"})
        else:
            self._reply(200, result)

    def do_PUT(self):
        self.close_connection = True
        self._reply(405, {"error": "method not allowed"})

    do_PATCH = do_PUT
    do_DELETE = do_PUT


class ReviewerBroker:
    """Expose exactly the read-input and submit-proposal service operations."""

    def __init__(self, app, socket_path: str | Path, token: str, *,
                 review_input_provider=None, proposal_submitter=None):
        if not token or not token.isascii() or any(ch in token for ch in "\r\n") or len(token) > 4096:
            raise ValueError("reviewer broker token must be a bounded ASCII secret")
        # Defaults expose only the existing two service operations. Injection
        # is for application-owned bounded-window selection and test control;
        # it does not add a route or a user-action capability.
        app.review_input_provider = review_input_provider or app.service.review_input
        app.proposal_submitter = proposal_submitter or app.service.review
        self.server = _UnixHTTPServer(socket_path, app, token)
        self.thread = threading.Thread(target=self.server.serve_forever, name="reviewer-broker", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        try:
            if stat.S_ISSOCK(os.lstat(self.server.server_address).st_mode):
                os.unlink(self.server.server_address)
        except FileNotFoundError:
            pass
