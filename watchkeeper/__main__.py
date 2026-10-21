"""Run the single-writer Watchkeeper application and reviewer broker."""

import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading
from contextlib import contextmanager

from .config import Config
from .db import Database
from .delivery import FileEmailSink, SMTPEmailSink
from .http import serve
from .reviewer_broker import ReviewerBroker
from .service import WatchkeeperService


@contextmanager
def _state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another Watchkeeper process owns this state directory") from exc
        with os.fdopen(descriptor, "r+") as stream:
            descriptor = -1
            yield
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _email_sink(config):
    if config.mail_transport == "smtp":
        return SMTPEmailSink(config.mail_host, config.mail_port, config.recipient, config.mail_sender,
                             config.public_base_url, security=config.mail_security,
                             username=config.mail_username, password_file=config.mail_password_file,
                             timeout=config.mail_timeout_seconds)
    return FileEmailSink(config.email_dir, config.recipient, config.public_base_url)


def _application(config):
    sink = _email_sink(config)
    service = WatchkeeperService(Database(config.database), sink,
        stale_after_seconds=config.stale_after_seconds,
        transcript_retention_days=config.transcript_retention_days,
        privacy_journal=config.privacy_journal,
        captured_email_dir=config.email_dir if config.mail_transport == "file" else None,
        inferred_after_days=config.inferred_after_days,
        schedule_timezone=config.schedule_timezone,
        review_time=config.review_time,
        digest_time=config.digest_time,
        review_window_limit=config.review_window_limit,
        review_windows_per_day=config.review_windows_per_day,
        review_window_bytes=config.review_window_bytes)
    app = type("Application", (), {})()
    app.config = config
    app.service = service
    app.ready = True
    return app


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv not in ([], ["--check-config"]):
        print("usage: python -m watchkeeper [--check-config]", file=sys.stderr)
        return 2
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(json.dumps({"status": "invalid_configuration", "error": str(exc)}), file=sys.stderr)
        return 2
    if argv == ["--check-config"]:
        print(json.dumps({"status": "valid", "version": "0.1.0"}))
        return 0

    stopping = threading.Event()
    with _state_lock(config.state_lock):
        app = _application(config)
        broker = ReviewerBroker(app, config.reviewer_broker_socket, config.reviewer_broker_token).start()
        scheduler_thread = threading.Thread(
            target=_scheduler_loop, args=(app.service, stopping, config.scheduler_poll_seconds),
            name="watchkeeper-scheduler", daemon=False)
        scheduler_thread.start()
        server = serve(app, config.bind_host, config.bind_port)

        def stop(_signum, _frame):
            app.ready = False
            stopping.set()
            # shutdown() is called from the signal handler only to release the
            # serve_forever loop; worker threads remain daemonized and bounded.
            threading.Thread(target=server.shutdown, daemon=True).start()

        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        print(json.dumps({"status": "ready", "version": "0.1.0", "port": server.server_address[1]}), flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            app.ready = False
            stopping.set()
            server.server_close()
            broker.close()
            # SMTP calls and database operations have explicit deadlines. Keep
            # the process lock until the scheduler has left its current call.
            while scheduler_thread.is_alive():
                scheduler_thread.join(timeout=1)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return 0


def _scheduler_loop(service, stopping, poll_seconds):
    while not stopping.is_set():
        try:
            service.scheduled_tick()
            service.expire_retention()
        except Exception:
            # Persisted run/incident state is the health surface; never log
            # user content, credentials, or transport details.
            pass
        stopping.wait(max(5, min(300, poll_seconds)))


if __name__ == "__main__":
    sys.exit(main())
