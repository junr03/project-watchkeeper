"""Small local-only SMTP capture sink for synthetic Compose acceptance."""

import os
from pathlib import Path
import socketserver
import sys
import uuid


class _SMTPHandler(socketserver.StreamRequestHandler):
    timeout = 15
    max_message_bytes = 2_000_000

    def handle(self):
        self.request.settimeout(self.timeout)
        self._reply(b"220 watchkeeper-smtp-sink ESMTP")
        while True:
            line = self.rfile.readline(8194)
            if not line or len(line) > 8192:
                return
            command = line.decode("ascii", errors="replace").strip()
            verb = command.split(None, 1)[0].upper() if command else ""
            if verb in {"EHLO", "HELO"}:
                self.wfile.write(b"250-watchkeeper-smtp-sink\r\n250 SIZE 2000000\r\n")
                self.wfile.flush()
            elif verb in {"MAIL", "RCPT"}:
                self._reply(b"250 accepted")
            elif verb == "NOOP" or verb == "RSET":
                self._reply(b"250 ok")
            elif verb == "DATA":
                self._reply(b"354 end with <CRLF>.<CRLF>")
                content = bytearray()
                while True:
                    row = self.rfile.readline(8194)
                    if not row or len(row) > 8192:
                        return
                    if row in (b".\r\n", b".\n"):
                        break
                    if row.startswith(b".."):
                        row = row[1:]
                    content.extend(row)
                    if len(content) > self.max_message_bytes:
                        self._reply(b"552 message too large")
                        return
                self._capture(bytes(content))
                self._reply(b"250 captured")
            elif verb == "QUIT":
                self._reply(b"221 bye")
                return
            else:
                self._reply(b"502 command not implemented")

    def _reply(self, value):
        self.wfile.write(value + b"\r\n")
        self.wfile.flush()

    def _capture(self, content):
        directory = Path(os.environ.get("WATCHKEEPER_SMTP_CAPTURE_DIR", "/var/lib/watchkeeper/smtp"))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (uuid.uuid4().hex + ".eml")
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())


class _SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True


def main():
    host = os.environ.get("WATCHKEEPER_SMTP_SINK_HOST", "0.0.0.0")
    port = int(os.environ.get("WATCHKEEPER_SMTP_SINK_PORT", "1025"))
    server = _SMTPServer((host, port), _SMTPHandler)
    print("Watchkeeper synthetic SMTP sink ready", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
