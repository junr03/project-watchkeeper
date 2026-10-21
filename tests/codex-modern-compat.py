"""Optional Codex 0.162.1 modern-history check with a local fake Responses API.

Run with: python tests/codex-modern-compat.py --codex-binary /path/to/codex
The script creates only synthetic content in a temporary directory and prints
counts, never transcript text. It does not use or inspect an account home.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from watchkeeper.collector import CODEX_READER_VERSION, JsonLineAppServer, SOURCE_KINDS


class FakeResponsesHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        item = {"id": "msg_synthetic_final", "type": "message", "role": "assistant",
                "status": "completed", "content": [{"type": "output_text",
                "text": "Synthetic garden status: choose planting dates."}], "phase": "final_answer"}
        response = {"id": "resp_synthetic", "object": "response", "created_at": 1792617000,
                    "status": "completed", "model": "fixture", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18}}
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**item, "status": "in_progress", "content": []}},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def transcript_session_id(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("type") == "session_meta":
                payload = record.get("payload") or {}
                value = payload.get("id", payload.get("session_id"))
                if isinstance(value, str) and value:
                    return value
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-binary", required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="watchkeeper-modern-compat-"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponsesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app_server = None
    try:
        clean_env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        clean_env.update({"HOME": str(root), "CODEX_HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                          "XDG_CACHE_HOME": str(root / "cache")})
        version = subprocess.run([args.codex_binary, "--version"], capture_output=True, text=True, env=clean_env,
                                 cwd=root, timeout=20, check=True)
        if CODEX_READER_VERSION not in version.stdout:
            raise RuntimeError(f"expected Codex {CODEX_READER_VERSION}")
        url = f"http://127.0.0.1:{server.server_port}/v1"
        command = [args.codex_binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--model", "fixture",
                   "-c", 'model_provider="fixture"', "-c", 'model_providers.fixture.name="fixture"',
                   "-c", f'model_providers.fixture.base_url="{url}"', "-c", 'model_providers.fixture.wire_api="responses"',
                   "-c", "model_providers.fixture.requires_openai_auth=false",
                   "Synthetic compatibility check only. Write one short garden status asking me to choose planting dates. Do not call tools."]
        result = subprocess.run(command, capture_output=True, text=True, env=clean_env, cwd=root, timeout=60)
        if result.returncode:
            raise RuntimeError(f"synthetic Codex exec failed with exit {result.returncode}")

        mirror = root / "mirror"
        sessions = mirror / "sessions"
        sessions.mkdir(parents=True)
        transcript = next((path for path in (root / "sessions").rglob("*.jsonl")
                           if transcript_session_id(path)), None)
        if transcript is None:
            raise RuntimeError("synthetic Codex exec did not produce a session transcript")
        thread_id = transcript_session_id(transcript)
        shutil.copyfile(transcript, sessions / transcript.name)
        (mirror / ".watchkeeper-reader-state").write_text(json.dumps({
            "format": "watchkeeper-codex-reader-state-v1", "credentials_excluded": True, "transcripts_only": True,
        }), encoding="utf-8")

        source_db = root / "thread_history_1.sqlite"
        if not source_db.is_file():
            raise RuntimeError("synthetic Codex exec did not create thread_history_1.sqlite")
        source = sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            with sqlite3.connect(mirror / "thread_history_1.sqlite") as snapshot:
                source.backup(snapshot)
        finally:
            source.close()

        app_server = JsonLineAppServer.launch(args.codex_binary, mirror)
        listed = app_server.call("thread/list", {"archived": False, "cursor": None, "limit": 50,
                                                  "sortKey": "updated_at", "sortDirection": "desc",
                                                  "useStateDbOnly": False, "sourceKinds": SOURCE_KINDS,
                                                  "modelProviders": []})
        metadata = next((thread for thread in listed.get("data", []) if thread.get("id") == thread_id), None)
        if metadata is None:
            raise RuntimeError("modern synthetic thread was not listed with modelProviders=[]")
        turns = app_server.call("thread/turns/list", {"threadId": thread_id, "cursor": None, "limit": 50,
                                                        "sortDirection": "asc", "itemsView": "full"})
        if not turns.get("data") or not any(turn.get("itemsView") == "full" and turn.get("items") for turn in turns["data"]):
            raise RuntimeError("modern synthetic thread returned no full-view turn items")
        item_count = sum(len(turn.get("items", [])) for turn in turns["data"])
        print(json.dumps({"status": "passed", "codex_version": CODEX_READER_VERSION,
                          "listed_thread": True, "turns": len(turns["data"]), "items": item_count,
                          "projection_schema": "accepted"}))
    finally:
        if app_server is not None:
            app_server.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
