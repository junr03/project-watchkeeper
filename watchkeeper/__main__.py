"""Run the local HTTP service using environment supplied credentials."""

from .config import Config
from .db import Database
from .delivery import FileEmailSink
from .http import serve
from .service import WatchkeeperService


def main():
    config = Config.from_env()
    service = WatchkeeperService(Database(config.database),
        FileEmailSink(config.email_dir, config.recipient, config.public_base_url),
        stale_after_seconds=config.stale_after_seconds,
        transcript_retention_days=config.transcript_retention_days,
        privacy_journal=config.privacy_journal,
        captured_email_dir=config.email_dir)
    server = serve(type("Application", (), {"config": config, "service": service})(),
                   host="0.0.0.0", port=int(__import__("os").environ.get("WATCHKEEPER_PORT", "8765")))
    print(f"Watchkeeper listening on {server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
