"""Local HTTP reverse-proxy fixture for private-ingress acceptance tests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import os
import sys
from urllib.parse import urlsplit


_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade"}


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = 10

    def log_message(self, *_args):
        return

    def _forward(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length < 0 or length > 1_000_000:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else None
        if body is not None and len(body) != length:
            self.send_error(400)
            return
        try:
            connection = http.client.HTTPConnection(self.server.upstream_host,
                                                     self.server.upstream_port, timeout=10)
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in _HOP_BY_HOP and key.lower() != "host"}
            headers["Host"] = self.server.upstream_host
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(1_100_000)
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in _HOP_BY_HOP and key.lower() not in {"content-length", "server", "date"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            connection.close()
        except (OSError, http.client.HTTPException):
            self.send_error(502)

    do_GET = _forward
    do_POST = _forward

    def do_PUT(self):
        self._forward()

    def do_DELETE(self):
        self._forward()

    def do_PATCH(self):
        self._forward()


def main():
    listen = os.environ.get("WATCHKEEPER_INGRESS_LISTEN", "127.0.0.1:8080")
    upstream = os.environ.get("WATCHKEEPER_INGRESS_UPSTREAM", "127.0.0.1:8765")
    try:
        host, port = listen.rsplit(":", 1)
        upstream_host, upstream_port = upstream.rsplit(":", 1)
        server = ThreadingHTTPServer((host, int(port)), _ProxyHandler)
        server.daemon_threads = False
        server.block_on_close = True
        server.upstream_host = upstream_host
        server.upstream_port = int(upstream_port)
    except (ValueError, OSError):
        print("private ingress fixture configuration failed", file=sys.stderr)
        return 2
    print("private ingress fixture ready", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
