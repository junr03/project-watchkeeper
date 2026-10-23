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
from watchkeeper.codex_mirror import prepare_codex_mirror


class FakeResponsesHandler(BaseHTTPRequestHandler):
    count = 0
    count_lock = threading.Lock()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.count_lock:
            type(self).count += 1
            count = type(self).count
        item = {"id": f"msg_synthetic_final_{count}", "type": "message", "role": "assistant",
                "status": "completed", "content": [{"type": "output_text",
                "text": f"Synthetic garden status {count}: choose planting dates."}], "phase": "final_answer"}
        response = {"id": f"resp_synthetic_{count}", "object": "response", "created_at": 1792617000 + count,
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


def run_codex(binary, root, env, provider_config, *args):
    if len(args) < 2:
        raise ValueError("synthetic Codex invocation needs a command and prompt")
    command = [binary, *args[:-1], *provider_config, args[-1]]
    result = subprocess.run(command, capture_output=True, text=True,
                            env=env, cwd=root, timeout=60)
    if result.returncode:
        raise RuntimeError(f"synthetic Codex exec failed with exit {result.returncode}")


def session_transcripts(root):
    found = {}
    directory = root / "sessions"
    if directory.is_dir():
        for path in directory.rglob("*.jsonl"):
            thread_id = transcript_session_id(path)
            if thread_id:
                found[thread_id] = path
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-binary", required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="watchkeeper-modern-compat-"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponsesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app_server = None
    prepared_mirror = None
    try:
        clean_env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        clean_env.update({"HOME": str(root), "CODEX_HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                          "XDG_CACHE_HOME": str(root / "cache")})
        version = subprocess.run([args.codex_binary, "--version"], capture_output=True, text=True, env=clean_env,
                                 cwd=root, timeout=20, check=True)
        if CODEX_READER_VERSION not in version.stdout:
            raise RuntimeError(f"expected Codex {CODEX_READER_VERSION}")
        url = f"http://127.0.0.1:{server.server_port}/v1"
        provider_config = ["-c", 'model_provider="fixture"', "-c", 'model_providers.fixture.name="fixture"',
                           "-c", f'model_providers.fixture.base_url="{url}"', "-c", 'model_providers.fixture.wire_api="responses"',
                           "-c", "model_providers.fixture.requires_openai_auth=false"]
        run_codex(args.codex_binary, root, clean_env, provider_config,
                  "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--model", "fixture",
                  "Synthetic compatibility check only. Write one short garden status asking me to choose planting dates. Do not call tools.")
        first_sessions = session_transcripts(root)
        if len(first_sessions) != 1:
            raise RuntimeError("synthetic Codex exec did not create exactly one initial session")
        first_thread_id = next(iter(first_sessions))
        # Resume locally to create a second turn in the same synthetic thread.
        run_codex(args.codex_binary, root, clean_env, provider_config,
                  "exec", "resume", "--skip-git-repo-check", "--model", "fixture", first_thread_id,
                  "Continue the synthetic garden status with one additional short sentence. Do not call tools.")
        run_codex(args.codex_binary, root, clean_env, provider_config,
                  "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--model", "fixture",
                  "Synthetic compatibility check only. Write one short garden status asking me to choose planting dates. Do not call tools.")
        run_codex(args.codex_binary, root, clean_env, provider_config,
                  "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--model", "fixture",
                  "Synthetic compatibility check only. Write one short garden status asking me to choose planting dates. Do not call tools.")
        transcripts = session_transcripts(root)
        if len(transcripts) != 3 or first_thread_id not in transcripts:
            raise RuntimeError("synthetic Codex exec did not produce the expected three threads")

        selected_source = root / "selected-source"
        sessions = selected_source / "sessions"
        sessions.mkdir(parents=True)
        archived = selected_source / "archived_sessions"
        archived.mkdir()
        thread_ids = sorted(transcripts)
        active_ids = set(thread_ids[:2])
        archived_id = next(thread_id for thread_id in thread_ids if thread_id not in active_ids)
        for thread_id, transcript in transcripts.items():
            destination_dir = sessions if thread_id in active_ids else archived
            shutil.copyfile(transcript, destination_dir / transcript.name)
        # These generated decoys demonstrate that preparation ignores root
        # config and general state without opening or copying them.
        (selected_source / "config.toml").write_text("synthetic config sentinel")
        (selected_source / "state_5.sqlite").write_bytes(b"synthetic general-state sentinel")

        source_db = root / "thread_history_1.sqlite"
        if not source_db.is_file():
            raise RuntimeError("synthetic Codex exec did not create thread_history_1.sqlite")
        source = sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            with sqlite3.connect(selected_source / "thread_history_1.sqlite") as snapshot:
                source.backup(snapshot)
        finally:
            source.close()

        prepared_mirror = prepare_codex_mirror(selected_source)
        mirror = prepared_mirror.__enter__()
        app_server = JsonLineAppServer.launch(args.codex_binary, mirror)
        def list_page(archived_state, cursor):
            return app_server.call("thread/list", {"archived": archived_state, "cursor": cursor, "limit": 1,
                                                   "sortKey": "updated_at", "sortDirection": "desc",
                                                   "useStateDbOnly": False, "sourceKinds": SOURCE_KINDS,
                                                   "modelProviders": []})
        first_page = list_page(False, None)
        if len(first_page.get("data", [])) != 1 or not first_page.get("nextCursor"):
            raise RuntimeError("two active synthetic threads did not produce a paginated listing")
        second_page = list_page(False, first_page["nextCursor"])
        active_listed = {item.get("id") for item in first_page["data"] + second_page.get("data", [])}
        if active_listed != active_ids:
            raise RuntimeError("active synthetic thread pagination did not return both selected threads")
        archived_page = list_page(True, None)
        if {item.get("id") for item in archived_page.get("data", [])} != {archived_id}:
            raise RuntimeError("nonempty archived synthetic thread scan did not return the selected archive")

        turn_first = app_server.call("thread/turns/list", {"threadId": first_thread_id, "cursor": None, "limit": 1,
                                                            "sortDirection": "asc", "itemsView": "full"})
        if len(turn_first.get("data", [])) != 1 or not turn_first.get("nextCursor"):
            raise RuntimeError("resumed synthetic thread did not produce a paginated turn listing")
        turn_second = app_server.call("thread/turns/list", {"threadId": first_thread_id,
                                                             "cursor": turn_first["nextCursor"], "limit": 1,
                                                             "sortDirection": "asc", "itemsView": "full"})
        turns = turn_first["data"] + turn_second.get("data", [])
        if len(turns) != 2 or any(turn.get("itemsView") != "full" or not turn.get("items") for turn in turns):
            raise RuntimeError("turn continuation did not return two full-view synthetic turns")
        item_count = sum(len(turn.get("items", [])) for turn in turns)
        first_turn_id = turns[0].get("id")
        item_page = app_server.call("thread/items/list", {"threadId": first_thread_id,
                                                            "turnId": first_turn_id, "cursor": None,
                                                            "limit": 1, "sortDirection": "asc"})
        if len(item_page.get("data", [])) != 1 or not item_page.get("nextCursor"):
            raise RuntimeError("modern synthetic turn did not produce a paginated item listing")
        next_item_page = app_server.call("thread/items/list", {"threadId": first_thread_id,
                                                                "turnId": first_turn_id,
                                                                "cursor": item_page["nextCursor"],
                                                                "limit": 1, "sortDirection": "asc"})
        if len(next_item_page.get("data", [])) != 1:
            raise RuntimeError("modern synthetic item continuation did not return the next item")
        print(json.dumps({"status": "passed", "codex_version": CODEX_READER_VERSION,
                          "active_threads": len(active_listed), "archived_threads": 1,
                          "continued_turns": len(turns), "full_view_items": item_count,
                          "item_pages": 2,
                          "projection_schema": "accepted"}))
    finally:
        if app_server is not None:
            app_server.close()
        if prepared_mirror is not None:
            prepared_mirror.__exit__(None, None, None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
