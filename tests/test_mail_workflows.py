import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError

from wsg import auth, cli, mail
from wsg.dates import InputError
from tests.test_mail_list import message


def run_cli(args, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with patch("wsg.cli._plugins", return_value=[]), patch("sys.stdin", io.StringIO(stdin)), \
            redirect_stdout(out), redirect_stderr(err):
        code = cli.main(args)
    return code, out.getvalue(), err.getvalue()


class MailWorkflowTests(unittest.TestCase):
    @patch("wsg.mail.graph")
    def test_server_filters_include_order_property_first_and_preserve_offsets(self, graph):
        graph.return_value = {"value": []}
        mail.messages(unread=True, since="2026-10-22T10:00+02:00", before="2026-10-23T00:00Z")
        query = parse_qs(urlsplit(graph.call_args.args[1]).query)
        self.assertEqual(query["$filter"], ["receivedDateTime ge 2026-10-22T08:00:00Z and "
                                            "receivedDateTime lt 2026-10-23T00:00:00Z and isRead eq false"])
        mail.messages(unread=True)
        query = parse_qs(urlsplit(graph.call_args.args[1]).query)
        self.assertTrue(query["$filter"][0].startswith("receivedDateTime ge "))
        self.assertTrue(query["$filter"][0].endswith("isRead eq false"))

    @patch("wsg.mail.graph")
    def test_custom_folder_encoded_and_conflicting_scope_refused(self, graph):
        graph.return_value = {"value": []}
        mail.messages(folder="folder/with+id")
        self.assertIn("/me/mailFolders/folder%2Fwith%2Bid/messages?", graph.call_args.args[1])
        with self.assertRaises(InputError):
            mail.messages(all_folders=True, folder="inbox")

    @patch("wsg.mail.graph")
    def test_invalid_dates_fail_before_graph(self, graph):
        for since, before in [("bad", None), ("2026-10-23", "2026-10-22")]:
            with self.assertRaises(InputError):
                mail.messages(since=since, before=before)
            with self.assertRaises(InputError):
                mail.find("invoice", since=since, before=before)
        graph.assert_not_called()

    @patch("wsg.mail.graph")
    def test_read_requests_text_and_only_gets(self, graph):
        graph.return_value = {"id": "a/b", "body": {"contentType": "text", "content": "hello"}}
        self.assertEqual(mail.read("a/b")["body"]["content"], "hello")
        self.assertEqual(graph.call_args.args[0], "GET")
        self.assertTrue(graph.call_args.args[1].startswith("/me/messages/a%2Fb?"))
        self.assertEqual(graph.call_args.kwargs["headers"], mail.TEXT_HEADER)

    @patch("wsg.mail.graph")
    def test_thread_paginates_all_folders_and_escapes_conversation_id(self, graph):
        early, late = message("early"), message("late")
        early["receivedDateTime"] = "2026-10-21T09:00:00Z"
        link = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=opaque"
        graph.side_effect = [{"conversationId": "a'b"}, {"value": [late], "@odata.nextLink": link},
                             {"value": [early]}]
        self.assertEqual(mail.thread("mid"), [early, late])
        query = parse_qs(urlsplit(graph.call_args_list[1].args[1]).query)
        self.assertEqual(query["$filter"], ["conversationId eq 'a''b'"])
        self.assertIn("body", query["$select"][0])
        self.assertEqual(graph.call_args.args, ("GET", link))
        self.assertTrue(all(c.args[0] == "GET" for c in graph.call_args_list))

    @patch("wsg.mail.graph")
    def test_archive_thread_pages_inbox_without_fetching_bodies(self, graph):
        link = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skip=100"
        graph.side_effect = [{"conversationId": "conv"}, {"value": [message("one")], "@odata.nextLink": link},
                             {"value": [message("two")]}]
        self.assertEqual(len(mail.thread_in_inbox("mid")), 2)
        self.assertNotIn("body", parse_qs(urlsplit(graph.call_args_list[1].args[1]).query)["$select"][0])

    @patch("wsg.mail.graph")
    def test_find_uses_index_and_exact_date_boundaries_without_mailbox_scan(self, graph):
        rows = [message(str(i)) for i in range(3)]
        rows[0]["receivedDateTime"] = "2026-10-22T07:59:59Z"
        rows[2]["receivedDateTime"] = "2026-10-22T09:00:00Z"
        graph.return_value = {"value": rows}
        found = mail.find('subject:invoice', since="2026-10-22T08:00Z", before="2026-10-22T09:00Z")
        self.assertEqual(found, [rows[1]])
        graph.assert_called_once()
        query = parse_qs(urlsplit(graph.call_args.args[1]).query)
        self.assertIn("subject:invoice", json.loads(query["$search"][0]))
        self.assertIn("received>=2026-10-22", json.loads(query["$search"][0]))
        self.assertNotIn("$orderby", query)
        self.assertNotIn("$filter", query)

    @patch("wsg.mail.graph")
    def test_find_limit_and_validation(self, graph):
        graph.return_value = {"value": [message("one"), message("two")]}
        self.assertEqual(len(mail.find("invoice", n=1)), 1)
        for query, n in [(" ", 1), ("invoice", 1001), ("invoice", 0)]:
            with self.assertRaises(InputError):
                mail.find(query, n=n)

    def test_text_rendering_preserves_lines_and_ignores_active_content(self):
        content = '<style>hidden</style><p>A &amp; B</p><div>next<br>line</div><script>hidden</script>'
        text = mail.text_of({"body": {"contentType": "HTML", "content": content}})
        self.assertIn("A & B", text)
        self.assertIn("next\nline", text)
        self.assertNotIn("hidden", text)

    @patch("wsg.mail.graph")
    def test_attachment_list_selects_metadata_and_follows_pages(self, graph):
        link = "https://graph.microsoft.com/v1.0/me/messages/mid/attachments?$skip=100"
        graph.side_effect = [{"value": [{"id": "one"}], "@odata.nextLink": link}, {"value": [{"id": "two"}]}]
        self.assertEqual(mail.attachments("mid"), [{"id": "one"}, {"id": "two"}])
        self.assertNotIn("contentBytes", graph.call_args_list[0].args[1])

    @patch("wsg.mail.graph")
    def test_attachment_save_binary_safe_filename_and_no_overwrite(self, graph):
        graph.side_effect = [{"name": "../../report.bin", "@odata.type": "#microsoft.graph.fileAttachment"}, b"\x00\xff"]
        with tempfile.TemporaryDirectory() as directory:
            result = mail.save_attachment("mid", "a/id", directory)
            dest = Path(directory) / "report.bin"
            self.assertEqual(dest.read_bytes(), b"\x00\xff")
            self.assertEqual(result["bytes"], 2)
            self.assertEqual(graph.call_args.args, ("GET", "/me/messages/mid/attachments/a%2Fid/$value"))
            self.assertEqual(graph.call_args.kwargs, {"raw": True})
            graph.side_effect = [{"name": "report.bin"}]
            with self.assertRaises(InputError):
                mail.save_attachment("mid", "aid", directory)
            self.assertEqual(dest.read_bytes(), b"\x00\xff")

    @patch("wsg.mail.graph")
    def test_reference_attachments_do_not_attempt_binary_download(self, graph):
        graph.return_value = {"@odata.type": "#microsoft.graph.referenceAttachment", "name": "cloud"}
        with self.assertRaises(InputError):
            mail.save_attachment("mid", "aid", "unused")
        graph.assert_called_once()

    @patch("wsg.mail.graph")
    def test_mutations_use_exact_body_and_move_returns_new_id(self, graph):
        mail.mark("a/b", False)
        self.assertEqual(graph.call_args.args, ("PATCH", "/me/messages/a%2Fb"))
        self.assertEqual(graph.call_args.kwargs, {"body": {"isRead": False}})
        mail.flag("mid", "complete")
        self.assertEqual(graph.call_args.kwargs, {"body": {"flag": {"flagStatus": "complete"}}})
        graph.return_value = {"id": "new"}
        self.assertEqual(mail.move("old", "archive"), {"id": "new"})
        self.assertEqual(graph.call_args.args, ("POST", "/me/messages/old/move"))

    @patch("wsg.mail.graph")
    def test_partial_archive_error_reports_completed_moves_without_repeating(self, graph):
        graph.side_effect = [{"id": "new"}, URLError("private-credential")]
        with self.assertRaises(InputError) as error:
            mail.archive([message("one"), message("two"), message("three")])
        self.assertIn("1 meddelanden", str(error.exception))
        self.assertNotIn("private-credential", str(error.exception))
        self.assertEqual(graph.call_count, 2)

    @patch("wsg.mail.graph")
    def test_folder_children_use_pagination_and_exact_parent(self, graph):
        link = "https://graph.microsoft.com/v1.0/me/mailFolders/p%2Fid/childFolders?$skip=100"
        graph.side_effect = [{"value": [{"id": "one"}], "@odata.nextLink": link},
                             {"value": [{"id": "two"}]}]
        self.assertEqual(mail.folders("p/id"), [{"id": "one"}, {"id": "two"}])
        self.assertEqual(graph.call_args.args, ("GET", link))


class MailCliWorkflowTests(unittest.TestCase):
    @patch("wsg.auth.get_token")
    @patch("wsg.mail.graph")
    def test_all_change_previews_are_json_without_auth_or_writes(self, graph, get_token):
        for args in (["mark", "mid", "--read"], ["flag", "mid"], ["move", "mid", "--to", "archive"]):
            code, out, err = run_cli(["mail"] + list(args) + ["--dry-run", "--json"])
            self.assertEqual((code, err), (0, ""))
            self.assertTrue(json.loads(out)["dryRun"])
        graph.assert_not_called()
        get_token.assert_not_called()

    @patch("wsg.mail.read", return_value=message("mid"))
    @patch("wsg.mail.thread", return_value=[message("mid")])
    @patch("wsg.mail.find", return_value=[message("mid")])
    def test_read_thread_and_find_have_clean_json(self, find, thread, read):
        for args in (["read", "mail:///messages/mid"], ["thread", "mid"], ["find", "invoice"]):
            code, out, err = run_cli(["mail"] + list(args) + ["--json"])
            self.assertEqual((code, err), (0, ""))
            self.assertTrue(json.loads(out))
        read.assert_called_once_with("mid")

    @patch("wsg.mail.move", return_value={"id": "new", "body": {"content": "not-output"}})
    def test_move_reports_new_id_only(self, move):
        code, out, err = run_cli(["mail", "move", "old", "--to", "archive", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"id": "new", "destinationId": "archive"})

    @patch("wsg.mail.thread_in_inbox", return_value=[message("mid")])
    @patch("wsg.mail.archive", return_value=1)
    def test_archive_json(self, archive, thread):
        code, out, err = run_cli(["mail", "archive", "mid", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"moved": 1, "originalIds": ["mid"]})

    @patch("wsg.mail.get", return_value=message("mid"))
    @patch("wsg.mail.me_address", return_value="me@example.com")
    @patch("wsg.mail.reply", return_value="sent-id")
    def test_reply_json_is_machine_readable(self, reply, me, get):
        code, out, err = run_cli(["mail", "reply", "mid", "--json"], stdin="Thanks")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["id"], "sent-id")

    @patch("wsg.mail.get", return_value=message("mid"))
    @patch("wsg.mail.me_address", return_value="me@example.com")
    def test_reply_utf8_file_preserves_accents(self, me, get):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reply.md"
            path.write_text("T\u00e4ck, vi h\u00f6rs", encoding="utf-8")
            code, out, err = run_cli(["mail", "reply", "mid", "--file", str(path), "--dry-run", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["text"], "T\u00e4ck, vi h\u00f6rs")

    @patch("wsg.auth.graph")
    def test_sendmail_json_acceptance_and_dry_run(self, graph):
        args = ["graph", "sendmail", "--to", "someone@example.com", "--subject", "Test", "--json"]
        code, out, err = run_cli(args + ["--dry-run"], stdin="Hello")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("message", json.loads(out))
        graph.assert_not_called()
        code, out, err = run_cli(args, stdin="Hello")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(json.loads(out)["accepted"])
        graph.assert_called_once()

    @patch("wsg.auth.graph")
    def test_sendmail_utf8_file_preserves_accents_and_markdown(self, graph):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mail.md"
            path.write_text("H\u00e4lsning\n\nF\u00f6rsta stycket", encoding="utf-8")
            code, out, err = run_cli(["graph", "sendmail", "--to", "someone@example.com",
                                      "--subject", "Test", "--file", str(path), "--dry-run", "--json"])
        self.assertEqual((code, err), (0, ""))
        body = json.loads(out)["message"]["body"]["content"]
        self.assertIn("H\u00e4lsning", body)
        self.assertIn("<p style=", body)
        self.assertNotIn("?", body)
        graph.assert_not_called()

    @patch("wsg.teams.send")
    @patch("wsg.auth.get_token")
    def test_teams_json_preview_does_not_authenticate_or_send(self, get_token, send):
        code, out, err = run_cli(["teams", "send", "--to", "someone@example.com", "--dry-run", "--json", "Hello"])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(json.loads(out)["dryRun"])
        get_token.assert_not_called()
        send.assert_not_called()

    @patch("wsg.teams.send")
    @patch("wsg.auth.get_token")
    def test_teams_send_renders_escaped_newlines_and_utf8_file(self, get_token, send):
        code, out, err = run_cli(["teams", "send", "--to", "someone@example.com",
                                  "--dry-run", r"First\n\n- One\n- Two"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("<ul>", out)
        self.assertIn("<li>One</li>", out)
        get_token.assert_not_called()
        send.assert_not_called()

    @patch("wsg.teams.send")
    @patch("wsg.auth.get_token")
    def test_teams_send_utf8_file_preserves_accents_and_paragraphs(self, get_token, send):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "message.md"
            path.write_text("Hälsning\n\n- Första\n- Andra", encoding="utf-8")
            code, out, err = run_cli(["teams", "send", "--to", "someone@example.com",
                                      "--file", str(path), "--dry-run"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Hälsning", out)
        self.assertIn("<li>Första</li>", out)
        get_token.assert_not_called()
        send.assert_not_called()

    @patch("wsg.teams.edit")
    @patch("wsg.auth.get_token")
    def test_teams_edit_dry_run_renders_markdown_without_auth(self, get_token, edit):
        code, out, err = run_cli(["teams", "edit", "--chat", "chat-id", "--message", "message-id",
                                  "--dry-run", "**Updated**"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("<strong>Updated</strong>", out)
        get_token.assert_not_called()
        edit.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class MailFolderTests(unittest.TestCase):
    @patch("wsg.mail.graph")
    def test_thread_selects_parent_folder_and_names_it_once_per_folder(self, graph):
        mail._FOLDER_NAMES.clear()
        rows = [dict(message("a"), parentFolderId="inbox-id"),
                dict(message("b"), parentFolderId="archive-id"),
                dict(message("c"), parentFolderId="archive-id")]
        graph.side_effect = [{"conversationId": "conv"}, {"value": rows},
                             {"displayName": "Inbox"}, {"displayName": "Archive"}]
        got = mail.with_folders(mail.thread("a"))
        self.assertEqual([r["folder"] for r in got], ["Inbox", "Archive", "Archive"])
        query = parse_qs(urlsplit(graph.call_args_list[1].args[1]).query)
        self.assertIn("parentFolderId", query["$select"][0])
        # The second message in Archive is answered from the cache, not a fourth call.
        self.assertEqual(graph.call_count, 4)

    @patch("wsg.mail.graph")
    def test_unreadable_folder_falls_back_to_the_id(self, graph):
        mail._FOLDER_NAMES.clear()
        graph.side_effect = URLError("offline")
        self.assertEqual(mail.folder_name("some-id"), "some-id")
        self.assertIsNone(mail.folder_name(None))
