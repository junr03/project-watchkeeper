import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

from watchkeeper.config import Config
from watchkeeper.delivery import DeliveryNotAccepted, SMTPEmailSink
from watchkeeper.reviewer_proxy import ProxyConfigurationError, parse_host_allowlist
from watchkeeper.smtp_sink import _SMTPHandler, _SMTPServer


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = {
            "WATCHKEEPER_INTAKE_TOKEN_FILE": str(self._secret("intake", "intake-token")),
            "WATCHKEEPER_REVIEWER_TOKEN_FILE": str(self._secret("reviewer", "reviewer-token")),
            "WATCHKEEPER_UI_USERNAME_FILE": str(self._secret("username", "demo-user")),
            "WATCHKEEPER_UI_PASSWORD_FILE": str(self._secret("password", "demo-password")),
            "WATCHKEEPER_DATABASE": str(self.root / "state" / "watchkeeper.sqlite3"),
            "WATCHKEEPER_EMAIL_DIR": str(self.root / "state" / "email"),
            "WATCHKEEPER_PUBLIC_BASE_URL": "https://watchkeeper.example.invalid",
            "WATCHKEEPER_ALLOWED_ORIGINS": "https://watchkeeper.example.invalid",
        }

    def tearDown(self):
        self.temp.cleanup()

    def _secret(self, name, value):
        path = self.root / name
        path.write_text(value)
        path.chmod(0o400)
        return path

    def test_secret_files_create_valid_production_configuration(self):
        with patch.dict(os.environ, self.env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.intake_token, "intake-token")
        self.assertEqual(config.reviewer_token, "reviewer-token")
        self.assertEqual(config.database, self.root / "state" / "watchkeeper.sqlite3")
        self.assertEqual(config.state_lock, (self.root / "state").resolve() / ".watchkeeper.lock")
        self.assertEqual(config.proxy_mode, "ignore")

    def test_missing_settings_and_insecure_secret_files_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "missing required"):
                Config.from_env()
        secret = Path(self.env["WATCHKEEPER_INTAKE_TOKEN_FILE"])
        secret.chmod(0o644)
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ValueError, "private regular"):
                Config.from_env()

    def test_secret_file_size_and_direct_file_ambiguity_are_rejected(self):
        large = self._secret("large", "x" * 4097)
        env = dict(self.env, WATCHKEEPER_INTAKE_TOKEN_FILE=str(large))
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "exceeds 4096"):
                Config.from_env()
        env = dict(self.env, WATCHKEEPER_INTAKE_TOKEN="inline")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "only one"):
                Config.from_env()

    def test_url_origin_schedule_proxy_and_smtp_configuration_are_validated(self):
        for updates, message in (
            ({"WATCHKEEPER_PUBLIC_BASE_URL": "https://host.example.invalid:99999"}, "port"),
            ({"WATCHKEEPER_ALLOWED_ORIGINS": "https://other.example.invalid"}, "include"),
            ({"WATCHKEEPER_PROXY_MODE": "trust-forwarded-user"}, "proxy mode"),
            ({"WATCHKEEPER_TRUSTED_PROXIES": "10.0.0.0/8"}, "unsupported"),
            ({"WATCHKEEPER_REVIEW_TIME": "25:00"}, "HH:MM"),
            ({"WATCHKEEPER_SCHEDULE_TIMEZONE": "Not/AZone"}, "time zone"),
            ({"WATCHKEEPER_REVIEW_WINDOW_BYTES": "100"}, "review window bytes"),
            ({"WATCHKEEPER_MAIL_TRANSPORT": "smtp", "WATCHKEEPER_MAIL_SECURITY": "plain",
              "WATCHKEEPER_MAIL_USERNAME": "user", "WATCHKEEPER_MAIL_PASSWORD_FILE": str(self._secret("mail", "pw"))},
             "plaintext transport"),
            ({"WATCHKEEPER_MAIL_TRANSPORT": "smtp", "WATCHKEEPER_MAIL_SECURITY": "plain",
              "WATCHKEEPER_MAIL_HOST": "relay.example.invalid"}, "limited to localhost"),
            ({"WATCHKEEPER_RECIPIENT": "naïve@example.invalid"}, "SMTPUTF8"),
        ):
            with self.subTest(updates=updates):
                env = dict(self.env)
                env.update(updates)
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ValueError, message):
                        Config.from_env()

    def test_inference_proxy_requires_exact_dns_names(self):
        self.assertEqual(parse_host_allowlist("api.example.invalid"), frozenset({"api.example.invalid"}))
        for value in ("192.0.2.8", "api.example.invalid,127.0.0.1", "https://api.example.invalid"):
            with self.subTest(value=value), self.assertRaises(ProxyConfigurationError):
                parse_host_allowlist(value)

    def test_fixed_state_lock_cannot_be_moved(self):
        values = dict(database=self.root / "state.sqlite", email_dir=self.root / "email",
                      intake_token="intake", reviewer_token="review", ui_username="user", ui_password="pass",
                      state_lock=self.root / "other.lock")
        with self.assertRaisesRegex(ValueError, "fixed beside"):
            Config(**values)


class _FakeTLSContext:
    def wrap_socket(self, sock, server_hostname=None):
        return sock


class _ScriptedSMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        behavior = self.server.behavior
        self._reply(b"220 synthetic ESMTP")
        in_data = False
        while True:
            line = self.rfile.readline(8192)
            if not line:
                return
            command = line.decode("ascii", errors="replace").strip()
            verb = command.split(None, 1)[0].upper()
            if verb in {"EHLO", "HELO"}:
                self.wfile.write(b"250-synthetic\r\n250 STARTTLS\r\n")
                self.wfile.flush()
            elif verb == "STARTTLS":
                self._reply(b"220 ready for TLS")
            elif verb == "MAIL":
                self._reply(b"250 sender ok")
            elif verb == "RCPT":
                if behavior == "reject_before_data":
                    self._reply(b"550 recipient rejected")
                else:
                    self._reply(b"250 recipient ok")
            elif verb == "DATA":
                self._reply(b"354 continue")
                in_data = True
                while in_data:
                    row = self.rfile.readline(8192)
                    if row in (b".\r\n", b".\n"):
                        in_data = False
                        break
                    if not row:
                        return
                if behavior == "disconnect_after_data":
                    self.request.shutdown(socket.SHUT_RDWR)
                    self.request.close()
                    return
                self._reply(b"250 accepted")
            elif verb == "QUIT":
                self._reply(b"221 bye")
                return
            else:
                self._reply(b"250 ok")

    def _reply(self, text):
        self.wfile.write(text + b"\r\n")
        self.wfile.flush()


class SMTPTransportTest(unittest.TestCase):
    def _sink(self, port, security):
        return SMTPEmailSink("127.0.0.1", port, "recipient@example.invalid", "watchkeeper@example.invalid",
                             "https://watchkeeper.example.invalid", security=security, timeout=2)

    def _projects(self):
        return [{"outcome": "Synthetic outcome", "next_action": "Choose a date", "reasoning": "Synthetic reason",
                 "evidence": [{"source_id": "synthetic", "thread_id": "thread-1", "locator": "fixture"}]}]

    def test_acceptance_classification_for_plain_starttls_and_ssl(self):
        for security in ("plain", "starttls", "ssl"):
            for behavior in ("accepted", "reject_before_data", "disconnect_after_data"):
                with self.subTest(security=security, behavior=behavior):
                    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ScriptedSMTPHandler)
                    server.behavior = behavior
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        sink = self._sink(server.server_address[1], security)
                        if security in {"starttls", "ssl"}:
                            with patch("watchkeeper.delivery.ssl.create_default_context", return_value=_FakeTLSContext()):
                                self._assert_behavior(sink, behavior)
                        else:
                            self._assert_behavior(sink, behavior)
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=2)

    def _assert_behavior(self, sink, behavior):
        if behavior == "accepted":
            result = sink.deliver_digest("delivery-1", self._projects())
            self.assertEqual(result, {"accepted": True})
        elif behavior == "reject_before_data":
            with self.assertRaises(DeliveryNotAccepted):
                sink.deliver_digest("delivery-1", self._projects())
        else:
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                sink.deliver_digest("delivery-1", self._projects())

    def test_real_local_capture_sink_accepts_fixed_recipient_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            server = _SMTPServer(("127.0.0.1", 0), _SMTPHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"WATCHKEEPER_SMTP_CAPTURE_DIR": temp}):
                    sink = self._sink(server.server_address[1], "plain")
                    sink.deliver_digest("delivery-42", self._projects())
                captures = list(Path(temp).glob("*.eml"))
                self.assertEqual(len(captures), 1)
                body = captures[0].read_text()
                self.assertIn("To: recipient@example.invalid", body)
                self.assertIn("Synthetic outcome", body)
                self.assertIn("X-Watchkeeper-Delivery-ID: delivery-42", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
