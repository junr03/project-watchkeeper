"""Small authenticated HTTP boundary for the first single-user slice."""

from base64 import b64decode
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import html
import json
import secrets
from urllib.parse import parse_qs, quote, unquote, urlparse
from .privacy import ConflictError


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
            secure = "; Secure" if urlparse(self.app.config.public_base_url).scheme == "https" else ""
            self.send_header("Set-Cookie", f"pw_csrf={csrf}; SameSite=Strict; Path=/; HttpOnly{secure}")
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
        return header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode("utf-8"), expected.encode("utf-8"))

    def _csrf(self):
        cookie = ""
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "pw_csrf":
                cookie = value
        supplied = self.headers.get("X-CSRF-Token", "")
        origin = urlparse(self.headers.get("Origin", ""))
        canonical = f"{origin.scheme}://{origin.netloc}" if origin.scheme and origin.netloc else ""
        same_origin = canonical in self.app.config.allowed_origins
        return bool(same_origin and cookie and supplied and hmac.compare_digest(cookie.encode("utf-8"), supplied.encode("utf-8")))

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
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if path == "/readyz":
            ready = getattr(self.app, "ready", False) is True
            self._json(200 if ready else 503, {"status": "ready" if ready else "starting"})
            return
        preview_project = self._deletion_preview_part(path)
        if preview_project is not None:
            if not self._basic():
                self._unauthorized()
                return
            self._run(lambda: self.app.service.deletion_preview(preview_project))
            return
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
            archived_view = parse_qs(parsed_url.query).get("view") == ["archived"]
            visible = state["archived_projects"] if archived_view else state["active_projects"]
            projects = "".join(self._project_html(p, state["projects"], archived_view) for p in visible)
            sources = "".join(self._source_html(s) for s in state["sources"])
            exclusions = "".join(self._exclusion_html(item) for item in state["excluded_threads"])
            exclusions += "".join(self._source_exclusion_html(source_id) for source_id in state["excluded_sources"])
            proposals = "".join(self._proposal_html(item) for item in state["proposals"])
            notices = "".join(
                f"<li>{html.escape(item['category'])} · {html.escape(item['subject'])}: "
                f"{html.escape(item['failure_kind'])} ({item['occurrences']} failed attempts)</li>"
                for item in state.get("monitoring", {}).get("open_notices", []))
            delivery_labels = {"accepted": "Accepted by the configured transport",
                               "retryable": "Not accepted; waiting to retry",
                               "ambiguous": "May have been accepted; will not retry automatically",
                               "cancelled": "Cancelled", "sending": "Sending"}
            delivery_history = "".join(
                f"<li>{html.escape(delivery_labels.get(item['status'], 'Delivery status unavailable'))} · "
                f"{html.escape(item['updated_at'])} · {item['findings']} finding(s), {item['notices']} notice(s)"
                + (f" · retry after {html.escape(item['retry_after'])}" if item.get("retry_after") else "")
                + "</li>" for item in state.get("monitoring", {}).get("deliveries", []))
            page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Project Watchkeeper</title>
<style>body{{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem}}article{{border:1px solid #bbb;border-radius:8px;padding:1rem;margin:1rem 0}}small{{color:#555}}form{{display:inline-block;margin:.25rem}}</style></head><body>
<h1>Project Watchkeeper</h1><p>Observed sources</p><ul>{sources or '<li>None yet</li>'}</ul>
<h2>Collection and review notices</h2><ul>{notices or '<li>None.</li>'}</ul>
<h2>Pending proposals</h2>{proposals or '<p>None.</p>'}<h2>Exclusions</h2>{exclusions or '<p>None.</p>'}
<p><a href="/">Active projects</a> · <a href="/?view=archived">Archived projects</a></p>
<h2>{'Archived projects' if archived_view else 'Active projects'}</h2>{projects or '<p>No projects in this view.</p>'}<button id="deliver" type="button">Send pending digest</button><h2>Delivery history</h2><ul>{delivery_history or '<li>No messages sent.</li>'}</ul><script>
const csrf={json.dumps(csrf)};
document.querySelectorAll('form[data-action]').forEach(form=>form.addEventListener('submit',async ev=>{{ev.preventDefault();const data={{}};if(form.elements.until)data.until=new Date(form.elements.until.value).toISOString();if(form.elements.project_id)data.project_id=form.elements.project_id.value;if(form.elements.from_project_id)data.from_project_id=form.elements.from_project_id.value;if(form.elements.new_project_id&&form.elements.new_project_id.value.trim())data.project_id=form.elements.new_project_id.value.trim();const r=await fetch(form.dataset.action,{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},body:JSON.stringify(data)}});if(r.ok)location.reload();else alert('Action was not saved.');}}));
document.querySelectorAll('form[data-delete-project]').forEach(form=>form.addEventListener('submit',async ev=>{{ev.preventDefault();const id=form.dataset.deleteProject;const encoded=encodeURIComponent(id);const p=await fetch('/api/projects/'+encoded+'/deletion-preview');if(!p.ok){{alert('Could not load deletion preview.');return;}}const impact=await p.json();const threads=impact.affected_threads.map(t=>t.source_id+' / '+t.thread_id).join(', ')||'none';const projects=impact.affected_projects.join(', ')||'none';const message='Delete '+id+'? This deletes the selected project, excludes '+impact.affected_threads.length+' known conversation(s): '+threads+'. It also clears derived summaries for directly affected shared projects: '+projects+'. It removes '+impact.pending_notifications+' pending notification(s) and '+impact.captured_emails+' app-controlled captured email(s). Mail already accepted by an external server cannot be recalled. Unrelated conversations remain.';if(!window.confirm(message))return;const r=await fetch('/api/projects/'+encoded+'/delete',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}},body:JSON.stringify({{confirm:true,impact_token:impact.impact_token}})}});if(r.ok)location.reload();else if(r.status===409)alert('Shared conversations changed. Reload and review the updated impact.');else alert('Deletion was not completed.');}}));
document.getElementById('deliver').addEventListener('click',async()=>{{const r=await fetch('/api/deliver',{{method:'POST',headers:{{'X-CSRF-Token':csrf,'Content-Length':'0'}}}});const d=await r.json();let message;if(!r.ok)message=d.error||'Delivery failed';else if(d.status==='ambiguous')message='The message may have been accepted. Watchkeeper will not retry it automatically.';else if(d.status==='retryable')message='The message was not accepted and is waiting for a retry.';else if(d.status==='cancelled_stale')message='The digest was cancelled because its findings changed.';else message='Processed '+d.emails+' digest message(s) covering '+d.delivered+' finding(s).';alert(message);if(r.ok)location.reload();}});
</script></body></html>"""
            self._html(page, csrf)
            return
        self._json(404, {"error": "not found"})

    def _project_html(self, project, all_projects, archived_view):
        evidence = "".join("<li>" + html.escape(f"{e['source_id']} · {e['thread_id']} · {e['locator']}") +
                           "<br><small>" + html.escape(e["text"][:1000]) + "</small>" +
                           self._thread_controls(project, e, all_projects) + "</li>" for e in project["evidence"])
        snooze = (f"<p>Snoozed until {html.escape(project['snooze_until'])}</p>" if project["snooze_until"] else
                  f"<form data-action=\"/api/projects/{html.escape(project['project_id'], quote=True)}/actions/snooze\"><label>Snooze until <input name=\"until\" type=\"datetime-local\" required></label> <button>Snooze</button></form>")
        action = lambda name: f"<form data-action=\"/api/projects/{html.escape(project['project_id'], quote=True)}/actions/{name}\"><button>{name.title()}</button></form>"
        if archived_view:
            controls = action("restore")
        else:
            controls = action("complete") + action("abandon") + action("reopen") + action("archive")
        controls += (f"<form data-delete-project=\"{html.escape(project['project_id'], quote=True)}\"><button>Delete project…</button></form>")
        if project["disposition"] == "completed":
            review_status = "Completed"
        elif project["disposition"] == "abandoned":
            review_status = "Abandoned"
        elif project.get("archived"):
            review_status = "Archived"
        elif project["assessment_kind"] == "proposal":
            review_status = "Proposal for your review"
        elif project["eligible"]:
            review_status = "Needs your attention"
        else:
            review_status = "Not currently eligible for a reminder"
        status = f"<p><small>Status: {html.escape(review_status)}</small></p>"
        if project["eligibility_reason"]:
            status += f"<p><small>{html.escape(project['eligibility_reason'])}</small></p>"
        return f"<article><h3>{html.escape(project['outcome'])}</h3><p><b>Next action:</b> {html.escape(project['next_action'])}</p><p>{html.escape(project['reasoning'])}</p>{status}<h4>Evidence and grouping</h4><ul>{evidence}</ul>{snooze}{controls}</article>"

    @staticmethod
    def _source_html(source):
        coverage = source.get("coverage", {})
        if not isinstance(coverage, dict):
            coverage = {}
        gaps = ", ".join(html.escape(str(g)) for g in coverage.get("gaps", [])) or "none reported"
        threads = source.get("threads", {})
        thread_details = "; ".join(
            f"{html.escape(str(thread_id))}: {html.escape('complete' if item.get('coverage_complete') else 'incomplete')}, "
            f"activity {html.escape(str(item.get('activity', 'unknown')))}"
            for thread_id, item in threads.items() if isinstance(item, dict))
        return (f"<li><b>{html.escape(source['source_id'])}</b>: {html.escape(source['freshness'])}; "
                f"coverage {html.escape(str(coverage.get('state', 'unknown')))}; "
                f"threads {html.escape(str(coverage.get('threads_read', 0)))} / {html.escape(str(coverage.get('threads_listed', 0)))}; "
                f"messages {html.escape(str(coverage.get('messages_read', 0)))}; "
                f"completeness {html.escape(str(coverage.get('message_completeness', 'unknown')))}; "
                f"archived history scanned {html.escape(str(coverage.get('archived_scanned', False))).lower()}; "
                f"version {html.escape(str(coverage.get('source_version', 'unknown')))}; "
                f"failure {html.escape(str(coverage.get('failure', 'none')))}; gaps: {gaps}; "
                f"per-thread: {thread_details or 'no thread-level report'}</li>")

    def _thread_controls(self, project, evidence, all_projects):
        source = quote(evidence["source_id"], safe="")
        thread = quote(evidence["thread_id"], safe="")
        base = f"/api/threads/{html.escape(source, quote=True)}/{html.escape(thread, quote=True)}"
        exclude = f"<form data-action=\"{base}/exclude\"><button>Exclude conversation</button></form>"
        choices = "".join(f"<option value=\"{html.escape(p['project_id'], quote=True)}\" {'selected' if p['project_id'] == project['project_id'] else ''}>{html.escape(p['project_id'])}</option>"
                          for p in all_projects)
        assign = f"<form data-action=\"{base}/assign\"><input type=\"hidden\" name=\"from_project_id\" value=\"{html.escape(project['project_id'], quote=True)}\"><label>Group in <select name=\"project_id\">{choices}</select> or split to <input name=\"new_project_id\" maxlength=\"128\"></label> <button>Move conversation</button></form>"
        source_exclude = f"<form data-action=\"/api/sources/{source}/exclude\"><button>Exclude source</button></form>"
        return exclude + source_exclude + assign

    def _exclusion_html(self, item):
        source, thread = quote(item["source_id"], safe=""), quote(item["thread_id"], safe="")
        source, thread = html.escape(source, quote=True), html.escape(thread, quote=True)
        return (f"<form data-action=\"/api/threads/{source}/{thread}/include\">"
                f"<span>{html.escape(item['source_id'])} · {html.escape(item['thread_id'])}</span> "
                "<button>Re-enable conversation</button></form>")

    def _source_exclusion_html(self, source_id):
        source = html.escape(quote(source_id, safe=""), quote=True)
        return (f"<form data-action=\"/api/sources/{source}/include\">"
                f"<span>{html.escape(source_id)}</span> <button>Re-enable source</button></form>")

    def _proposal_html(self, proposal):
        return (f"<article><h3>{html.escape(proposal['outcome'])}</h3>"
                f"<p><b>Proposed project:</b> {html.escape(proposal['requested_project_id'])}</p>"
                f"<p><b>Next action:</b> {html.escape(proposal['next_action'])}</p>"
                f"<p>{html.escape(proposal['reasoning'])}</p>"
                f"<p><small>{html.escape(proposal['status'])}: {html.escape(proposal['reason'])}</small></p></article>")

    def do_POST(self):
        path = urlparse(self.path).path
        delete_project = self._delete_project_part(path)
        if delete_project is not None:
            if not self._basic():
                self._unauthorized()
                return
            if not self._csrf():
                self._json(403, {"error": "csrf validation failed"})
                return
            def delete_action():
                payload = self._body()
                if set(payload) != {"confirm", "impact_token"}:
                    raise ValueError("delete requires explicit confirmation and the preview impact token")
                return self.app.service.delete_project(delete_project, payload["impact_token"],
                                                       confirmed=payload["confirm"])
            self._run(delete_action)
            return
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
        project_action = self._project_action(path)
        legacy_project = self._project_part(path)
        if project_action or legacy_project != "__invalid__":
            if not self._basic():
                self._unauthorized()
                return
            if not self._csrf():
                self._json(403, {"error": "csrf validation failed"})
                return
            project_id, action_name = project_action if project_action else (unquote(legacy_project), "snooze")
            def perform_action():
                payload = self._body()
                if action_name == "snooze":
                    if set(payload) != {"until"}:
                        raise ValueError("snooze accepts only until")
                    return self.app.service.snooze(project_id, payload["until"])
                if payload:
                    raise ValueError("project action does not accept fields")
                return self.app.service.decide(project_id, action_name)
            self._run(perform_action)
            return
        source_action = self._source_action(path)
        if source_action:
            if not self._basic():
                self._unauthorized()
                return
            if not self._csrf():
                self._json(403, {"error": "csrf validation failed"})
                return
            source_id, action = source_action
            def source_decision():
                if self._body():
                    raise ValueError("source action does not accept fields")
                return self.app.service.exclude_source(source_id, action == "exclude")
            self._run(source_decision)
            return
        thread_action = self._thread_action(path)
        if thread_action:
            if not self._basic():
                self._unauthorized()
                return
            if not self._csrf():
                self._json(403, {"error": "csrf validation failed"})
                return
            source_id, thread_id, action = thread_action
            def thread_decision():
                payload = self._body()
                if action in ("exclude", "include"):
                    if payload:
                        raise ValueError("conversation exclusion accepts no fields")
                    return self.app.service.exclude_thread(source_id, thread_id, action == "exclude")
                if action == "assign":
                    if set(payload) - {"project_id", "from_project_id"} or "project_id" not in payload or not isinstance(payload["project_id"], str):
                        raise ValueError("assign requires project_id")
                    return self.app.service.assign_thread(source_id, thread_id, payload["project_id"], payload.get("from_project_id"))
                raise ValueError("unknown conversation action")
            self._run(thread_decision)
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

    @staticmethod
    def _deletion_preview_part(path):
        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "projects"] and parts[4] == "deletion-preview":
            return unquote(parts[3])
        return None

    @staticmethod
    def _delete_project_part(path):
        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "projects"] and parts[4] == "delete":
            return unquote(parts[3])
        return None

    @staticmethod
    def _project_action(path):
        parts = path.split("/")
        if len(parts) == 6 and parts[1:3] == ["api", "projects"] and parts[4] == "actions" and parts[5] in {"complete", "abandon", "reopen", "snooze", "archive", "restore"}:
            return unquote(parts[3]), parts[5]
        return None

    @staticmethod
    def _source_action(path):
        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "sources"] and parts[4] in {"exclude", "include"}:
            return unquote(parts[3]), parts[4]
        return None

    @staticmethod
    def _thread_action(path):
        parts = path.split("/")
        if len(parts) == 6 and parts[1:3] == ["api", "threads"] and parts[5] in {"exclude", "include", "assign"}:
            return unquote(parts[3]), unquote(parts[4]), parts[5]
        return None

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Watchkeeper", charset="UTF-8"')
        self.end_headers()

    def _run(self, operation):
        try:
            self._json(200, operation())
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except ConflictError as exc:
            self._json(409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "operation failed"})


class WatchkeeperHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(30)
        return request, address


def serve(app, host="127.0.0.1", port=8765):
    return WatchkeeperHTTPServer((host, port), app)
