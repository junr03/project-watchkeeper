"""Allowlisted HTTPS CONNECT proxy and loopback-to-Unix socket bridge.

Only configured public hostnames on TCP/443 can be tunneled. DNS results are
checked and the selected public IP is connected directly to avoid rebinding.
"""

from ipaddress import ip_address
import os
import select
import socket
import socketserver
import stat
import sys
import threading
import re
from pathlib import Path
from urllib.parse import urlsplit


class ProxyConfigurationError(ValueError):
    pass


def parse_host_allowlist(value: str) -> frozenset[str]:
    hosts = {part.strip().lower().rstrip(".") for part in value.split(",") if part.strip()}
    dns_name = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z")
    if not hosts or any(not dns_name.fullmatch(host) for host in hosts):
        raise ProxyConfigurationError("inference host allowlist must contain exact DNS names")
    for host in hosts:
        try:
            ip_address(host)
        except ValueError:
            continue
        raise ProxyConfigurationError("inference host allowlist must not contain IP literals")
    return frozenset(hosts)


class _TunnelHandler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self):
        self.request.settimeout(self.timeout)
        line = self.rfile.readline(4097)
        if len(line) > 4096:
            return self._deny(400, "Bad Request")
        try:
            method, authority, version = line.decode("ascii").strip().split(" ", 2)
        except (UnicodeDecodeError, ValueError):
            return self._deny(400, "Bad Request")
        if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
            return self._deny(405, "Method Not Allowed")
        try:
            target = urlsplit("//" + authority)
            hostname = (target.hostname or "").lower().rstrip(".")
            port = target.port
        except ValueError:
            return self._deny(400, "Bad Request")
        if not hostname or port != 443 or target.username or target.password or hostname not in self.server.allow_hosts:
            return self._deny(403, "Forbidden")
        headers = 0
        while True:
            header = self.rfile.readline(8193)
            headers += len(header)
            if not header or len(header) > 8192 or headers > 32768:
                return self._deny(400, "Bad Request")
            if header in (b"\r\n", b"\n"):
                break
        try:
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            return self._deny(502, "Bad Gateway")
        candidates = []
        for family, socktype, proto, _, sockaddr in addresses:
            address = ip_address(sockaddr[0])
            if not address.is_global:
                return self._deny(403, "Forbidden")
            candidates.append((family, socktype, proto, sockaddr))
        if not candidates:
            return self._deny(502, "Bad Gateway")
        upstream = None
        for family, socktype, proto, sockaddr in candidates:
            try:
                upstream = socket.socket(family, socktype, proto)
                upstream.settimeout(15)
                upstream.connect(sockaddr)
                break
            except OSError:
                if upstream is not None:
                    upstream.close()
                    upstream = None
        if upstream is None:
            return self._deny(502, "Bad Gateway")
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.request.settimeout(None)
        upstream.settimeout(None)
        try:
            _tunnel(self.request, upstream)
        finally:
            upstream.close()

    def _deny(self, status, reason):
        self.request.sendall(f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode())


def _tunnel(left: socket.socket, right: socket.socket):
    peers = (left, right)
    while True:
        readable, _, _ = select.select(peers, [], [], 60)
        if not readable:
            return
        for source in readable:
            target = right if source is left else left
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                target.sendall(data)
            except OSError:
                return


class HTTPSConnectProxy:
    """Networked process exposing only the configured inference destinations."""

    def __init__(self, socket_path: str, allow_hosts: frozenset[str]):
        if not allow_hosts:
            raise ProxyConfigurationError("inference host allowlist is required")
        self.socket_path = socket_path
        self.allow_hosts = frozenset(host.lower().rstrip(".") for host in allow_hosts)
        Path(socket_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.path.lexists(socket_path):
            if not stat.S_ISSOCK(os.lstat(socket_path).st_mode):
                raise ProxyConfigurationError("inference proxy path exists and is not a socket")
            os.unlink(socket_path)
        self.server = socketserver.ThreadingUnixStreamServer(socket_path, _TunnelHandler)
        self.server.allow_hosts = self.allow_hosts
        os.chmod(socket_path, 0o660)
        self.thread = threading.Thread(target=self.server.serve_forever, name="inference-connect-proxy", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        try:
            if stat.S_ISSOCK(os.lstat(self.socket_path).st_mode):
                os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(self.server.unix_path)
            _tunnel(self.request, upstream)
        except OSError:
            return
        finally:
            upstream.close()


class _LoopbackHTTPProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


class UnixProxyBridge:
    """Expose the Unix proxy as a temporary loopback HTTP proxy to Node."""

    def __init__(self, socket_path: str, host: str = "127.0.0.1"):
        self.server = _LoopbackHTTPProxy((host, 0), _BridgeHandler)
        self.server.unix_path = socket_path
        self.url = f"http://{host}:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, name="inference-proxy-bridge", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def main() -> int:
    """Run the separately networked, exact-host inference proxy."""
    try:
        allow_hosts = parse_host_allowlist(os.environ.get("WATCHKEEPER_INFERENCE_HOSTS", ""))
        proxy = HTTPSConnectProxy(os.environ.get("WATCHKEEPER_PROXY_SOCKET", "/run/watchkeeper/proxy/inference.sock"),
                                  allow_hosts).start()
    except (ProxyConfigurationError, OSError):
        print("inference proxy configuration failed", file=sys.stderr)
        return 2
    print("inference proxy ready")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
