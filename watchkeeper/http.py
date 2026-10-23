"""Small authenticated HTTP boundary for the first single-user slice."""

from base64 import b64decode
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import html
import json
import secrets
from urllib.parse import unquote, urlparse


class Handler(BaseHTTPRequestHandler):
    server_version = "Watchkeeper/0.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # Avoid placing request bodies, credentials, or user content in logs.
        return

    def _json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body, csrf=None):
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if csrf:
            self.send_header("Set-Cookie", f"pw_csrf={csrf}; SameSite=Strict; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(encoded)

    def _basic(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            pair = b64decode(header[6:], validate=True).decode()
            username, password = pair.split(":", 1)
        except Exception:
            return False
        return hmac.compare_digest(username.encode(), self.app.config.ui_username.encode()) and hmac.compare_digest(password.encode(), self.app.config.ui_password.encode())

    def _bearer(self, expected):
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ") and hmac.compare_digest(header[7:], expected)

    def _csrf(self):
        cookie = ""
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "pw_csrf":
                cookie = value
        supplied = self.headers.get("X-CSRF-Token", "")
        configured = urlparse(self.app.config.public_base_url)
        origin = urlparse(self.headers.get("Origin", ""))
        same_origin = (configured.scheme, configured.netloc) == (origin.scheme, origin.netloc)
        return bool(same_origin and cookie and supplied and hmac.compare_digest(cookie, supplied))

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length")
        if length <= 0 or length > 1_000_000:
            raise ValueError("request body must be between 1 and 1000000 bytes")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/review-input":
            if not self._bearer(self.app.config.reviewer_token):
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, self.app.service.review_input())
            return
        if path == "/api/state" or path == "/":
            if not self._basic():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Watchkeeper", charset="UTF-8"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            state = self.app.service.state()
            if path == "/api/state":
                self._json(200, state)
                return
            csrf = secrets.token_urlsafe(24)
            projects = "".join(self._project_html(p) for p in state["projects"])
            sources = "".join(f"<li>{html.escape(s['source_id'])}: {html.escape(s['freshness'])}</li>" for s in state["sources"])
            page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Project Watchkeeper</title>
<style>body{{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem}}article{{border:1px solid #bbb;border-radius:8px;padding:1rem;margin:1rem 0}}small{{color:#555}}</style></head><body>
<h1>Project Watchkeeper</h1><p>Observed sources</p><ul>{sources or '<li>None yet</li>'}</ul>
<h2>Projects</h2>{projects or '<p>No projects yet.</p>'}<script>
const csrf={json.dumps(csrf)};
document.querySelectorAll('form[data-project]').forEach(form=>form.addEventListener('submit',async ev=>{{ev.preventDefault();const until=new Date(form.elements.until.value);if(Number.isNaN(until.valueOf()))return;const r=await fetch('/api/projects/'+encodeURIComponent(form.dataset.project)+'/snooze',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},body:JSON.stringify({{until:until.toISOString()}})}});if(r.ok)location.reload();else alert('Snooze was not saved.');}}));
document.getElementById('deliver').addEventListener('click',async()=>{{const r=await fetch('/api/deliver',{{method:'POST',headers:{{'X-CSRF-Token':csrf,'Content-Length':'0'}}}});const d=await r.json();alert(r.ok?'Captured '+d.delivered+' digest email(s).':(d.error||'Delivery failed'));}});
</script><button id="deliver" type="button">Capture pending digest</button></body></html>"""
            self._html(page, csrf)
            return
        self._json(404, {"error": "not found"})

    def _project_html(self, project):
        evidence = "".join("<li>" + html.escape(f"{e['source_id']} · {e['thread_id']} · {e['locator']}") +
                           "<br><small>" + html.escape(e["text"][:1000]) + "</small></li>" for e in project["evidence"])
        snooze = (f"<p>Snoozed until {html.escape(project['snooze_until'])}</p>" if project["snooze_until"] else
                  f"<form data-project=\"{html.escape(project['project_id'], quote=True)}\"><label>Snooze until <input name=\"until\" type=\"datetime-local\" required></label> <button>Snooze</button></form>")
        return f"<article><h3>{html.escape(project['outcome'])}</h3><p><b>Next action:</b> {html.escape(project['next_action'])}</p><p>{html.escape(project['reasoning'])}</p><h4>Evidence</h4><ul>{evidence}</ul>{snooze}</article>"

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/observations":
            if not self._bearer(self.app.config.intake_token):
                self._json(401, {"error": "unauthorized"})
                return
            self._run(lambda: self.app.service.ingest(self._body()))
            return
        if path == "/api/reviews":
            if not self._bearer(self.app.config.reviewer_token):
                self._json(401, {"error": "unauthorized"})
                return
            self._run(lambda: self.app.service.review(self._body()))
            return
        if self._project_part(path) != "__invalid__":
            if not self._basic():
                self._unauthorized()
                return
            if not self._csrf():
                self._json(403, {"error": "csrf validation failed"})
                return
            project_id = unquote(self._project_part(path))
            def action():
                payload = self._body()
                if set(payload) != {"until"}:
                    raise ValueError("snooze accepts only until")
                return self.app.service.snooze(project_id, payload["until"])
            self._run(action)
            return
        if path == "/api/deliver":
            if not self._basic():
                self._unauthorized()
                return
            if not self._csrf():
                self._json(403, {"error": "csrf validation failed"})
                return
            self._run(self.app.service.deliver_pending)
            return
        self._json(404, {"error": "not found"})

    @staticmethod
    def _project_part(path):
        prefix = "/api/projects/"
        suffix = "/snooze"
        return path[len(prefix):-len(suffix)] if path.startswith(prefix) and path.endswith(suffix) else "__invalid__"

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Watchkeeper", charset="UTF-8"')
        self.end_headers()

    def _run(self, operation):
        try:
            self._json(200, operation())
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "operation failed"})


class WatchkeeperHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


def serve(app, host="127.0.0.1", port=8765):
    return WatchkeeperHTTPServer((host, port), app)
