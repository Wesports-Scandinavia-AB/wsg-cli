import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from wsg import auth


def response(data):
    r = MagicMock()
    r.__enter__.return_value.read.return_value = json.dumps(data).encode()
    return r


def http_error(code=429, retry=None):
    return HTTPError("https://example.com/private-credential", code, "private-credential",
                     {"Retry-After": retry} if retry is not None else {},
                     io.BytesIO(b'{"error":{"message":"private-credential"}}'))


class GraphTests(unittest.TestCase):
    @patch("wsg.auth.get_token", return_value="private-credential")
    @patch("wsg.auth._graph_open")
    def test_session_reuses_token_and_resets_for_next_command(self, urlopen, get_token):
        urlopen.return_value = response({"id": "me"})
        for _ in range(2):
            with auth.graph_session():
                auth.graph("GET", "/me")
                auth.graph("GET", "/me/messages")
        self.assertEqual(get_token.call_count, 2)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 30})

    @patch("wsg.auth.get_token", return_value="private-credential")
    @patch("wsg.auth._graph_open")
    @patch("wsg.auth.time.sleep")
    def test_get_retries_throttling_and_transient_failure(self, sleep, urlopen, get_token):
        urlopen.side_effect = [http_error(429, "3"), http_error(503), response({"ok": True})]
        self.assertEqual(auth.graph("GET", "/me"), {"ok": True})
        self.assertEqual([c.args for c in sleep.call_args_list], [(3.0,), (2,)])
        get_token.assert_called_once()

    @patch("wsg.auth._graph_open")
    @patch("wsg.auth.time.sleep")
    def test_mutations_never_retry(self, sleep, urlopen):
        for method in ("POST", "PATCH", "DELETE"):
            urlopen.reset_mock()
            urlopen.side_effect = http_error(503)
            with self.assertRaises(HTTPError):
                auth.graph(method, "/me/messages/id", token="test", body={})
            urlopen.assert_called_once()
        sleep.assert_not_called()

    @patch("wsg.auth._graph_open")
    @patch("wsg.auth.time.sleep")
    def test_retries_bounded_and_long_retry_after_not_ignored(self, sleep, urlopen):
        urlopen.side_effect = http_error(429)
        with self.assertRaises(HTTPError):
            auth.graph("GET", "/me", token="test")
        self.assertEqual(urlopen.call_count, 3)
        sleep.reset_mock()
        urlopen.reset_mock()
        urlopen.side_effect = http_error(429, "120")
        with self.assertRaises(HTTPError):
            auth.graph("GET", "/me", token="test")
        urlopen.assert_called_once()
        sleep.assert_not_called()

    @patch("wsg.auth._graph_open")
    @patch("wsg.auth.time.sleep")
    def test_network_errors_retry_only_reads(self, sleep, urlopen):
        urlopen.side_effect = [URLError("private-credential"), TimeoutError(), response({})]
        self.assertEqual(auth.graph("GET", "/me", token="test"), {})
        self.assertEqual(sleep.call_count, 2)
        urlopen.reset_mock()
        urlopen.side_effect = URLError("private-credential")
        with self.assertRaises(URLError):
            auth.graph("POST", "/me/sendMail", token="test", body={})
        urlopen.assert_called_once()

    @patch("wsg.auth.get_token")
    @patch("wsg.auth._graph_open")
    def test_invalid_graph_origin_rejected_before_auth(self, urlopen, get_token):
        for url in ("https://example.com/me", "http://graph.microsoft.com/v1.0/me",
                    "https://graph.microsoft.com.evil/v1.0/me", "https://user@graph.microsoft.com/v1.0/me"):
            with self.assertRaises(ValueError):
                auth.graph("GET", url)
        get_token.assert_not_called()
        urlopen.assert_not_called()

    @patch("wsg.auth.graph")
    def test_pagination_reuses_headers_and_rejects_cycles_and_changed_scope(self, graph):
        link = "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=opaque%2Bvalue"
        graph.side_effect = [{"value": [{"id": "one"}], "@odata.nextLink": link},
                             {"value": [{"id": "two"}]}]
        headers = {"Prefer": 'outlook.timezone="UTC"'}
        self.assertEqual(list(auth.graph_pages("/me/calendarView", headers)), [{"id": "one"}, {"id": "two"}])
        self.assertEqual(graph.call_args.args, ("GET", link))
        self.assertEqual(graph.call_args.kwargs, {"headers": headers})
        graph.side_effect = None
        graph.return_value = {"value": [], "@odata.nextLink": link}
        with self.assertRaises(ValueError):
            list(auth.graph_pages("/me/calendarView"))
        graph.return_value = {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users"}
        with self.assertRaises(ValueError):
            list(auth.graph_pages("/me/calendarView"))

    def test_error_messages_never_echo_response_url_or_exception(self):
        for error in (http_error(403), http_error(404), http_error(500), auth.AuthError("private-credential"),
                      URLError("private-credential"), ValueError("private-credential")):
            self.assertNotIn("private-credential", auth.error_message(error))
        self.assertIn("403", auth.error_message(http_error(403)))

    def test_redirect_cannot_forward_authorization_to_other_origin(self):
        req = auth.request.Request("https://graph.microsoft.com/v1.0/me", headers={"Authorization": "Bearer private"})
        with self.assertRaises(HTTPError):
            auth._GraphRedirect().redirect_request(req, io.BytesIO(), 302, "Found", {}, "https://example.com/collect")

    @patch("wsg.auth.graph")
    def test_malformed_collection_and_nextlink_fail_cleanly(self, graph):
        for data in (None, [], {"value": {}}, {"value": ["invalid"]}, {"value": [], "@odata.nextLink": 123}):
            graph.return_value = data
            with self.assertRaises(ValueError):
                list(auth.graph_pages("/me/messages"))

    @patch("wsg.auth.datetime")
    def test_retry_after_date_and_invalid_values(self, clock):
        clock.now.return_value = datetime(2026, 10, 22, 8, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(auth._retry_delay(http_error(429, "Thu, 22 Oct 2026 08:00:05 GMT"), 0), 5)
        self.assertEqual(auth._retry_delay(http_error(429, "invalid"), 1), 2)
        self.assertIsNone(auth._retry_delay(http_error(429, "NaN"), 0))

    @patch("wsg.auth.get_token", side_effect=["core", "other", "scoped"])
    def test_token_cache_keeps_clients_and_scopes_separate(self, token):
        other = auth.Client("other", "other.json", "wsg graph login")
        with auth.graph_session():
            self.assertEqual(auth.graph_token(), "core")
            self.assertEqual(auth.graph_token(client=other), "other")
            self.assertEqual(auth.graph_token(scopes=["Mail.ReadWrite"]), "scoped")
            self.assertEqual(auth.graph_token(), "core")
        self.assertEqual(token.call_count, 3)

    @patch("wsg.auth.get_token", return_value="private-credential")
    @patch("wsg.auth._graph_open")
    def test_raw_attachment_bytes_and_prefer_header(self, urlopen, get_token):
        urlopen.return_value.__enter__.return_value.read.return_value = b"\x00\xfffile"
        self.assertEqual(auth.graph("GET", "/me/messages/id/attachments/a/$value", raw=True), b"\x00\xfffile")
        auth.graph("GET", "/me/messages/id", raw=True, headers={"Prefer": 'outlook.body-content-type="text"'})
        self.assertEqual(urlopen.call_args.args[0].get_header("Prefer"), 'outlook.body-content-type="text"')

    @patch("wsg.auth.get_token", return_value="private-credential")
    def test_parallel_reads_preserve_order_and_share_auth(self, get_token):
        def read(i):
            return i, auth.graph_token()

        self.assertEqual(auth.parallel_reads(read, [3, 1, 2]),
                         [(3, "private-credential"), (1, "private-credential"), (2, "private-credential")])
        get_token.assert_called_once()


if __name__ == "__main__":
    unittest.main()
