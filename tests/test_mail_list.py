import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from wsg import auth, cli, mail


def message(mid, name="Alex", address="alex@example.com", read=False):
    return {"id": mid, "receivedDateTime": "2026-10-22T08:30:00Z",
            "from": {"emailAddress": {"name": name, "address": address}},
            "subject": "Möte", "isRead": read}


class MailMessagesTests(unittest.TestCase):
    @patch("wsg.mail.graph")
    def test_all_folders_opt_in_uses_mailbox_endpoint_and_pagination(self, graph):
        link = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=opaque%2B1"
        rows = [message("first"), message("second")]
        graph.side_effect = [{"value": rows[:1], "@odata.nextLink": link}, {"value": rows[1:]}]
        self.assertEqual(mail.messages(2, all_folders=True), rows)
        self.assertEqual(urlsplit(graph.call_args_list[0].args[1]).path, "/me/messages")
        self.assertEqual(graph.call_args_list[1].args, ("GET", link))

    @patch("wsg.mail.graph")
    def test_inbox_pagination_cannot_expand_to_all_folders(self, graph):
        graph.return_value = {"value": [],
                              "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=1"}
        with self.assertRaises(ValueError):
            mail.messages()
        graph.assert_called_once()

    @patch("wsg.mail.graph")
    def test_query_and_limit_stop_before_another_page(self, graph):
        rows = [message(str(i)) for i in range(3)]
        graph.return_value = {"value": rows, "@odata.nextLink": "unused"}
        self.assertEqual(mail.messages(2), rows[:2])
        graph.assert_called_once()
        method, url = graph.call_args.args
        self.assertEqual(method, "GET")
        self.assertEqual(urlsplit(url).path, "/me/mailFolders/inbox/messages")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["$orderby"], ["receivedDateTime desc"])
        self.assertEqual(query["$top"], ["2"])
        self.assertEqual(query["$select"], [mail.LIST_FIELDS])
        self.assertNotIn("$search", query)
        self.assertNotIn("$filter", query)

    @patch("wsg.mail.graph")
    def test_filters_count_matches_across_empty_pages_and_preserve_nextlink(self, graph):
        links = [f"https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=opaque%2B{i}"
                 for i in range(3)]
        wanted = [message("name", name="ÅLEX Berg"),
                  message("address", name="Other", address="TEAM@example.com")]
        graph.side_effect = [
            {"value": [message("read", read=True), message("other", name="Other",
                                                            address="other@example.com")],
             "@odata.nextLink": links[0]},
            {"value": [], "@odata.nextLink": links[1]},
            {"value": [wanted[0]], "@odata.nextLink": links[2]},
            {"value": [wanted[1], message("extra")]},
        ]
        self.assertEqual(mail.messages(2, [" ålex ", "team@"], unread=True), wanted)
        self.assertEqual([c.args for c in graph.call_args_list[1:]],
                         [("GET", link) for link in links])

    @patch("wsg.mail.graph")
    def test_exhaustion_missing_fields_and_no_duplicates_from_multiple_terms(self, graph):
        row = message("both")
        graph.return_value = {"value": [{"id": "missing", "from": None}, row]}
        self.assertEqual(mail.messages(10, ["alex", "example.com"]), [row])
        self.assertEqual(len(mail.messages(10)), 2)
        self.assertEqual(mail.messages(10, unread=True), [row])

    @patch("wsg.mail.graph")
    def test_large_count_pages_and_caps_page_size(self, graph):
        link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skip=100"
        first = [message(str(i)) for i in range(100)]
        last = message("last")
        graph.side_effect = [{"value": first, "@odata.nextLink": link}, {"value": [last]}]
        self.assertEqual(mail.messages(101), first + [last])
        self.assertEqual(parse_qs(urlsplit(graph.call_args_list[0].args[1]).query)["$top"], ["100"])

    @patch("wsg.mail.graph")
    def test_invalid_input_does_not_call_graph(self, graph):
        for n, senders in [(0, None), (-1, None), (1, [" "])]:
            with self.assertRaises(ValueError):
                mail.messages(n, senders)
        graph.assert_not_called()

    @patch("wsg.mail.graph")
    def test_reject_untrusted_pagination_before_sending_credentials(self, graph):
        for link in ["https://example.com/v1.0/me/mailFolders/inbox/messages", "http://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
                     "https://graph.microsoft.com.evil/v1.0/me/mailFolders/inbox/messages",
                     "https://graph.microsoft.com/v1.0/users/other/messages"]:
            with self.subTest(link=link):
                graph.reset_mock()
                graph.return_value = {"value": [], "@odata.nextLink": link}
                with self.assertRaises(ValueError):
                    mail.messages()
                graph.assert_called_once()


class MailListCliTests(unittest.TestCase):
    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("wsg.cli._plugins", return_value=[]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["mail", "list"] + args)
        return code, stdout.getvalue(), stderr.getvalue()

    @patch("wsg.mail.messages")
    def test_all_folders_option(self, messages):
        messages.return_value = []
        self.assertEqual(self.run_cli(["--all-folders", "--json"]), (0, "[]\n", ""))
        messages.assert_called_once_with(30, None, False, True, since=None, before=None, folder=None)

    @patch("wsg.mail.messages")
    def test_json_and_parser_options(self, messages):
        rows = [message("graph-id")]
        messages.return_value = rows
        code, stdout, stderr = self.run_cli(["--from", "Alex", "--from", "Sam", "-n", "7",
                                              "--unread", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), rows)
        self.assertEqual(stderr, "")
        messages.assert_called_once_with(7, ["Alex", "Sam"], True, False, since=None, before=None, folder=None)

    @patch("wsg.mail.messages")
    def test_default_and_count_aliases(self, messages):
        messages.return_value = []
        for args, n in [([], 30), (["--number", "4"], 4), (["--limit", "5"], 5)]:
            self.assertEqual(self.run_cli(args)[0], 0)
            messages.assert_called_with(n, None, False, False, since=None, before=None, folder=None)

    @patch("wsg.mail.messages")
    def test_invalid_arguments_fail_before_auth(self, messages):
        for args in [["-n", "0"], ["-n", "-1"], ["-n", "bad"], ["--from", "  "]]:
            with self.assertRaises(SystemExit) as error:
                self.run_cli(args)
            self.assertEqual(error.exception.code, 2)
        messages.assert_not_called()

    @patch("wsg.mail.messages")
    def test_text_columns_missing_values_and_single_line_fields(self, messages):
        row = message("one")
        row["subject"] = "Möte\nny rad\ttext"
        messages.return_value = [row, message("two", read=True), {"from": None}]
        with patch("wsg.cli._ts", return_value="2026-10-22 10:30"):
            code, stdout, stderr = self.run_cli([])
        self.assertEqual((code, stderr), (0, ""))
        for text in ["2026-10-22 10:30", "Alex <alex@example.com>", "Möte ny rad text",
                     "oläst", "läst", "(utan ämne)"]:
            self.assertIn(text, stdout)
        self.assertEqual(len(stdout.splitlines()), 4)

    @patch("wsg.mail.messages", return_value=[])
    def test_empty_text_and_json(self, messages):
        self.assertEqual(self.run_cli([]), (0, "Inga mejl matchar.\n", ""))
        self.assertEqual(self.run_cli(["--json"]), (0, "[]\n", ""))

    @patch("wsg.mail.graph")
    def test_later_page_error_has_no_partial_json_or_sensitive_detail(self, graph):
        secret = "credential-must-never-appear"
        errors = [auth.AuthError(secret),
                  HTTPError("https://example.com/" + secret, 403, secret, {}, io.BytesIO(secret.encode())),
                  URLError(secret), ValueError(secret)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                graph.side_effect = [
                    {"value": [message("partial")],
                     "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skip=1"},
                    error,
                ]
                code, stdout, stderr = self.run_cli(["--json"])
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr)
                self.assertNotIn(secret, stderr)
                if isinstance(error, HTTPError):
                    self.assertIn("403", stderr)

    @patch("wsg.auth._graph_open")
    @patch("wsg.auth.get_token", return_value="test-only-credential")
    def test_existing_graph_auth_used_without_exposing_token(self, get_token, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"value": [message("one")]}).encode()
        code, stdout, stderr = self.run_cli(["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)[0]["id"], "one")
        get_token.assert_called_once_with(None, None)
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.startswith("https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?"))
        self.assertEqual(req.get_header("Authorization"), "Bearer test-only-credential")
        self.assertNotIn("test-only-credential", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
