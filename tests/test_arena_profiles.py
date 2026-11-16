import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from wsg import arena
from wsg.auth import AuthError
from wsg.cli import main


class Profiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            "WSG_ARENA_TOKEN_CACHE": str(Path(self.tmp.name) / "tokens.json"),
            "WSG_ARENA_URL": "https://arena.example.invalid",
            "WSG_ARENA_NO_RELOGIN": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = patch("wsg.arena.client", return_value=("cl_test", None))
        self.client.start()
        self.addCleanup(self.client.stop)

    def token(self, sensitive=False):
        return {"scope": arena.SENSITIVE_SCOPE if sensitive else arena.STANDARD_SCOPE,
                "access_token": "sensitive" if sensitive else "standard",
                "refresh_token": "refresh-sensitive" if sensitive else "refresh-standard",
                "expires_in": 3600, "refresh_expires_in": 43200 if sensitive else 2592000}

    def test_caches_and_logout_are_separate(self):
        arena._write_cache(self.token())
        arena._write_cache(self.token(True), sensitive=True)
        self.assertNotEqual(arena.cache_path(), arena.cache_path(True))
        self.assertEqual(arena.get_token(), "standard")
        self.assertEqual(arena.get_token(sensitive=True), "sensitive")
        self.assertTrue(arena.logout(sensitive=True))
        self.assertEqual(arena.get_token(), "standard")
        with self.assertRaisesRegex(AuthError, "login --sensitive"):
            arena.get_token(sensitive=True)

    def test_legacy_and_copied_sensitive_cache_cannot_be_everyday_access(self):
        path = arena.cache_path()
        for scope in (None, arena.SENSITIVE_SCOPE):
            path.write_text(json.dumps({"scope": scope, "client_id": "cl_test", "access_token": "old"}), encoding="utf-8")
            self.assertIsNone(arena._read_cache())
        with self.assertRaises(AuthError):
            arena._write_cache(self.token(True))
        with self.assertRaises(AuthError):
            arena._write_cache(self.token(), sensitive=True)

    def test_refresh_preserves_profile_and_has_server_expiry(self):
        with patch("wsg.arena.time.time", return_value=1000):
            arena._write_cache(self.token(), signed_in_at=100)
            arena._write_cache(self.token(True), signed_in_at=200, sensitive=True)
        with patch("wsg.arena.time.time", return_value=5000), patch("wsg.arena._token_endpoint", return_value=(self.token(), None)) as endpoint:
            self.assertEqual(arena.get_token(), "standard")
            self.assertEqual(endpoint.call_args.args[0]["refresh_token"], "refresh-standard")
        self.assertEqual(arena.status()["signed_in_at"], 100)
        self.assertEqual(arena.status()["refresh_expires_at"], 5000 + 2592000)
        self.assertEqual(arena.status(sensitive=True)["signed_in_at"], 200)

    @patch("wsg.arena.get_token", side_effect=["ordinary", "separate-sensitive"])
    @patch("wsg.arena.request.urlopen")
    def test_server_signal_selects_sensitive_once_before_a_write(self, urlopen, token):
        denied = HTTPError("https://arena.example.invalid/api/test", 403, "", {}, io.BytesIO(b'{"error":"sensitive_token_required"}'))
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok":true}'
        response.headers = {"Content-Type": "application/json"}
        urlopen.side_effect = [denied, response]
        self.assertEqual(arena.api("POST", "/api/test", body={"x": 1}), {"ok": True})
        self.assertEqual(token.call_args_list, [unittest.mock.call(sensitive=False), unittest.mock.call(sensitive=True)])
        self.assertEqual(urlopen.call_args_list[0].args[0].data, urlopen.call_args_list[1].args[0].data)
        self.assertEqual(urlopen.call_args.args[0].get_header("Authorization"), "Bearer separate-sensitive")

    @patch("wsg.arena.get_token", return_value="ordinary")
    @patch("wsg.arena.request.urlopen")
    def test_permission_denials_and_server_errors_do_not_elevate_or_retry(self, urlopen, token):
        for code, body in [(403, b'{"error":"permission denied"}'), (500, b'{"error":"server_error"}')]:
            urlopen.reset_mock()
            token.reset_mock()
            urlopen.side_effect = HTTPError("https://arena.example.invalid/api/test", code, "", {}, io.BytesIO(body))
            with self.assertRaises(AuthError):
                arena.api("POST", "/api/test", body={})
            urlopen.assert_called_once()
            token.assert_called_once_with(sensitive=False)

    @patch("wsg.arena.get_token")
    @patch("wsg.arena.request.urlopen")
    def test_explicit_token_never_switches_credentials(self, urlopen, token):
        urlopen.side_effect = HTTPError("https://arena.example.invalid/api/test", 403, "", {}, io.BytesIO(b'{"error":"sensitive_token_required"}'))
        with self.assertRaises(AuthError):
            arena.api("GET", "/api/test", token="caller-token")
        token.assert_not_called()
        urlopen.assert_called_once()

    def test_cli_profile_flags(self):
        for command, fn in [("login", "login"), ("token", "get_token"), ("status", "status"), ("logout", "logout")]:
            with patch("wsg.cli.arena." + fn) as action, patch("wsg.cli.arena.get", return_value={}), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                if command == "status":
                    action.return_value = None
                main(["arena", command, "--sensitive"])
                self.assertTrue(action.call_args.kwargs["sensitive"])


if __name__ == "__main__":
    unittest.main()
