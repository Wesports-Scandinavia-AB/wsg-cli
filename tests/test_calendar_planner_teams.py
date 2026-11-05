import json
import threading
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from wsg import auth, mscal, planner, teams
from wsg.dates import InputError
from tests.test_mail_workflows import run_cli


def event(eid="one", cancelled=False):
    return {"id": eid, "subject": "Review", "isCancelled": cancelled,
            "start": {"dateTime": "2026-10-22T08:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-22T09:00:00.0000000", "timeZone": "UTC"}}


class CalendarTests(unittest.TestCase):
    @patch("wsg.auth.graph")
    def test_calendar_view_includes_occurrences_and_follows_pages(self, graph):
        link = "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=opaque"
        graph.side_effect = [{"value": [event()], "@odata.nextLink": link},
                             {"value": [event("cancelled", True), event("recurring-occurrence")]}]
        rows = mscal.agenda("2026-10-22", days=7)
        self.assertEqual([r["id"] for r in rows], ["one", "recurring-occurrence"])
        query = parse_qs(urlsplit(graph.call_args_list[0].args[1]).query)
        self.assertEqual(query["$orderby"], ["start/dateTime"])
        start, end = (datetime.fromisoformat(query[k][0].replace("Z", "+00:00"))
                      for k in ("startDateTime", "endDateTime"))
        # Stockholm changes clocks during this range; either way, the end is
        # independently converted from local midnight rather than a fixed offset.
        self.assertEqual(start.astimezone().date().isoformat(), "2026-10-22")
        self.assertEqual(end.astimezone().date().isoformat(), "2026-10-29")
        self.assertEqual(start.astimezone().hour, 0)
        self.assertEqual(end.astimezone().hour, 0)
        self.assertEqual(graph.call_args.args, ("GET", link))
        self.assertEqual(graph.call_args.kwargs, {"headers": {"Prefer": 'outlook.timezone="UTC"'}})

    @patch("wsg.mscal.graph_pages")
    def test_invalid_calendar_range_does_not_request_graph(self, pages):
        for day, days in [("invalid", 7), (None, 0), (None, 367)]:
            with self.assertRaises(InputError):
                mscal.agenda(day, days)
        pages.assert_not_called()

    @patch("wsg.mscal.agenda", return_value=[event()])
    def test_today_and_agenda_json(self, agenda):
        for args, days in [(["today"], 1), (["agenda", "--days", "3"], 3)]:
            code, out, err = run_cli(["calendar"] + args + ["--json"])
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out), [event()])
            agenda.assert_called_with(None, days, False)

    @patch("wsg.mscal.agenda", return_value=[event()])
    def test_calendar_text_time_subject(self, agenda):
        code, out, err = run_cli(["calendar", "today"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Review", out)
        self.assertIn("2026-10-22", out)

    def test_existing_calendar_mirror_preserves_privacy_and_travel(self):
        source = {"id": "event-id", "summary": "Private appointment",
                  "start": {"dateTime": "2026-10-22T10:00:00+02:00"},
                  "end": {"dateTime": "2026-10-22T11:00:00+02:00"}}
        bodies = mscal.bodies_for(source, travel=30)
        self.assertEqual(len(bodies), 3)
        self.assertEqual(bodies[1]["start"]["dateTime"], "2026-10-22T08:00:00")
        self.assertNotIn("Private appointment", json.dumps(bodies))
        self.assertEqual(len({b["transactionId"] for b in bodies}), 3)

    @patch("wsg.mscal.find", return_value=[event()])
    @patch("wsg.mscal.graph")
    def test_unmirror_reuses_fetched_rows_without_second_search(self, graph, find):
        code, out, err = run_cli(["calendar", "unmirror", "google-id", "--yes", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"removed": 1})
        find.assert_called_once_with("google-id")
        graph.assert_called_once_with("DELETE", "/me/events/one")

    @patch("wsg.mscal.graph")
    def test_calendar_mirror_json_preview_does_not_write(self, graph):
        source = {"id": "google-id", "start": {"dateTime": "2026-10-22T08:00Z"},
                  "end": {"dateTime": "2026-10-22T09:00Z"}}
        code, out, err = run_cli(["calendar", "mirror", "--dry-run", "--json"], stdin=json.dumps(source))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(json.loads(out)), 3)
        graph.assert_not_called()


class PlannerTests(unittest.TestCase):
    @patch("wsg.planner.my_tasks", return_value=[{"id": "one"}])
    @patch("wsg.planner.plans")
    def test_mine_json_skips_plan_and_group_name_requests(self, plans, my_tasks):
        code, out, err = run_cli(["planner", "mine", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), [{"id": "one"}])
        plans.assert_not_called()

    @patch("wsg.planner.resolve_plan", return_value={"id": "plan"})
    @patch("wsg.planner.tasks", return_value=[])
    @patch("wsg.planner.buckets")
    def test_tasks_json_skips_bucket_request(self, buckets, tasks, resolve):
        self.assertEqual(run_cli(["planner", "tasks", "plan", "--json"]), (0, "[]\n", ""))
        buckets.assert_not_called()

    @patch("wsg.planner._paged", return_value=[{"id": "plan", "title": "Plan"}])
    @patch("wsg.planner.names")
    def test_titles_only_skips_owner_resolution(self, names, paged):
        self.assertEqual(planner.plans(with_names=False), [{"id": "plan", "title": "Plan"}])
        names.assert_not_called()

    @patch("wsg.planner._paged", return_value=[{"id": "task", "assignments": {"user": {}}}])
    @patch("wsg.planner.names")
    def test_mine_text_skips_assignee_names_it_does_not_display(self, names, paged):
        self.assertEqual(planner.my_tasks(with_assignees=False)[0]["id"], "task")
        names.assert_not_called()

    @patch("wsg.planner._call")
    def test_assignee_resolution_deduplicates_and_batches(self, call):
        call.return_value = {"value": [{"id": "user", "displayName": "Example"}]}
        self.assertEqual(planner.names(["user", "user", None]), {"user": "Example"})
        call.assert_called_once_with("/directoryObjects/getByIds", method="POST", body={"ids": ["user"]})


class TeamsAndParallelTests(unittest.TestCase):
    @patch("wsg.teams.graph")
    def test_edit_checks_ownership_then_patches_html_body(self, graph):
        graph.side_effect = [
            {"id": "message", "messageType": "message", "from": {"user": {"id": "me"}}},
            {"id": "me"},
            None,
        ]
        teams.edit("chat/id", "message/id", "<p>Updated</p>")
        self.assertEqual(graph.call_args_list[0].args,
                         ("GET", "/chats/chat/id/messages/message/id?$select=id,from,messageType"))
        self.assertEqual(graph.call_args_list[2].args,
                         ("PATCH", "/chats/chat/id/messages/message/id"))
        self.assertEqual(graph.call_args_list[2].kwargs["body"],
                         {"body": {"contentType": "html", "content": "<p>Updated</p>"}})
        self.assertEqual(graph.call_args_list[2].kwargs["scopes"], ["Chat.ReadWrite"])

    @patch("wsg.teams.graph")
    def test_edit_refuses_messages_written_by_someone_else(self, graph):
        graph.side_effect = [
            {"id": "message", "messageType": "message", "from": {"user": {"id": "other"}}},
            {"id": "me"},
        ]
        with self.assertRaises(PermissionError):
            teams.edit("chat", "message", "<p>Updated</p>")
        self.assertEqual(graph.call_count, 2)

    @patch("wsg.auth.get_token", return_value="test-only")
    def test_read_concurrency_is_bounded_and_results_ordered(self, get_token):
        barrier = threading.Barrier(4, timeout=5)
        lock = threading.Lock()
        active = 0
        peak = 0

        def work(i):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            with lock:
                active -= 1
            return i

        self.assertEqual(auth.parallel_reads(work, range(8)), list(range(8)))
        self.assertEqual(peak, 4)
        get_token.assert_called_once()

    @patch("wsg.teams.chats", return_value=[{"id": "one"}, {"id": "two"}])
    @patch("wsg.teams.messages")
    @patch("wsg.auth.get_token", return_value="test-only")
    def test_recent_search_keeps_newest_first_across_chats(self, token, messages, chats):
        def rows(chat_id, n, since):
            return [{"id": chat_id, "createdDateTime": "2026-10-22" if chat_id == "one" else "2026-10-23",
                     "body": {"contentType": "text", "content": "Invoice"}}]

        messages.side_effect = rows
        self.assertEqual([m["id"] for c, m in teams.find("invoice")], ["two", "one"])

    @patch("wsg.teams.graph")
    def test_existing_chat_messages_skip_system_events_and_page(self, graph):
        link = "https://graph.microsoft.com/v1.0/chats/id/messages?$skiptoken=opaque"
        graph.side_effect = [{"value": [{"id": "system", "createdDateTime": "2026-10-23", "messageType": "systemEvent"}],
                              "@odata.nextLink": link},
                             {"value": [{"id": "real", "createdDateTime": "2026-10-22", "messageType": "message"}]}]
        self.assertEqual([m["id"] for m in teams.messages("id", 1)], ["real"])
        self.assertEqual(graph.call_args.args, ("GET", link))


if __name__ == "__main__":
    unittest.main()
