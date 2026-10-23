"""Run the local HTTP service using environment supplied credentials."""

from .config import Config
from .db import Database
from .delivery import FileEmailSink
from .http import serve
from .service import WatchkeeperService
import threading
import time


def main():
    config = Config.from_env()
    service = WatchkeeperService(Database(config.database),
        FileEmailSink(config.email_dir, config.recipient, config.public_base_url),
        stale_after_seconds=config.stale_after_seconds,
        inferred_after_days=config.inferred_after_days,
        schedule_timezone=config.schedule_timezone,
        review_time=config.review_time,
        digest_time=config.digest_time,
        review_window_limit=config.review_window_limit,
        review_windows_per_day=config.review_windows_per_day,
        review_window_bytes=config.review_window_bytes)
    stopping = threading.Event()
    def scheduler_loop():
        while not stopping.is_set():
            try:
                service.scheduled_tick()
            except Exception:
                # Persisted run/incident state is the health surface; avoid
                # logging user content or transport details.
                pass
            stopping.wait(max(5, min(300, config.scheduler_poll_seconds)))
    scheduler_thread = threading.Thread(target=scheduler_loop, name="watchkeeper-scheduler", daemon=True)
    scheduler_thread.start()
    server = serve(type("Application", (), {"config": config, "service": service})(),
                   host="0.0.0.0", port=int(__import__("os").environ.get("WATCHKEEPER_PORT", "8765")))
    print(f"Watchkeeper listening on {server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    finally:
        stopping.set()
        scheduler_thread.join(timeout=3)
        server.server_close()


if __name__ == "__main__":
    main()
