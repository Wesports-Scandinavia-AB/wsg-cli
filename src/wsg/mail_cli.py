"""Mail workflows added beside the original send/reply commands."""

import json

from . import mail


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _address(value):
    who = (value or {}).get("emailAddress") or {}
    return " ".join(filter(None, [who.get("name"),
                                 f"<{who['address']}>" if who.get("address") else None])) or "?"


def _print_message(msg):
    from .cli import _ts

    where = f"  [{msg['folder']}]" if msg.get("folder") else ""
    print(f"--- {_ts(msg.get('receivedDateTime'))}  {_address(msg.get('from'))}{where}")
    print(f"Ämne: {msg.get('subject') or '(utan ämne)'}")
    for key, label in (("toRecipients", "Till"), ("ccRecipients", "Kopia")):
        if msg.get(key):
            print(f"{label}: {', '.join(_address(r) for r in msg[key])}")
    print(f"Id: {msg.get('id', '?')}")
    if msg.get("hasAttachments"):
        print("Har bilagor (se wsg mail attachments).")
    print()
    print(mail.text_of(msg))


def cmd_read(a):
    row = mail.with_folders([mail.read(mail.message_id(a.message))])[0]
    _json(row) if a.json else _print_message(row)
    return 0


def cmd_thread(a):
    rows = mail.with_folders(mail.thread(mail.message_id(a.message)))
    if a.json:
        _json(rows)
    else:
        for row in rows:
            _print_message(row)
        if not rows:
            print("Inga mejl i tråden.")
    return 0


def cmd_find(a):
    from .cli import _print_mail_rows

    rows = mail.find(a.query, a.number, a.all_folders, a.since, a.before, a.folder)
    _json(rows) if a.json else _print_mail_rows(rows)
    return 0


def cmd_attachments(a):
    rows = mail.attachments(mail.message_id(a.message))
    if a.json:
        _json(rows)
    else:
        for row in rows:
            inline = " (inline)" if row.get("isInline") else ""
            print(f"{row.get('name') or '(utan namn)'}  {row.get('size', 0)} byte{inline}\n  {row['id']}")
        if not rows:
            print("Inga bilagor.")
    return 0


def cmd_save(a):
    row = mail.save_attachment(mail.message_id(a.message), a.attachment, a.out)
    _json(row) if a.json else print(f"Sparad: {row['path']} ({row['bytes']} byte)")
    return 0


def cmd_folders(a):
    rows = mail.folders(a.parent)
    if a.json:
        _json(rows)
    else:
        for row in rows:
            print(f"{row.get('displayName')}  {row.get('unreadItemCount', 0)} olästa, "
                  f"{row.get('childFolderCount', 0)} undermappar\n  {row['id']}")
        if not rows:
            print("Inga mappar.")
    return 0


def cmd_change(a):
    mid = mail.message_id(a.message)
    if a.action == "mark":
        method, body = "PATCH", {"isRead": a.read}
    elif a.action == "flag":
        method, body = "PATCH", {"flag": {"flagStatus": a.status}}
    else:
        method, body = "POST", {"destinationId": a.to}
    path = mail._path(mid) + ("/move" if a.action == "move" else "")
    if a.dry_run:
        result = {"dryRun": True, "method": method, "path": path, "body": body}
    else:
        if a.action == "mark":
            response = mail.mark(mid, a.read)
        elif a.action == "flag":
            response = mail.flag(mid, a.status)
        else:
            response = mail.move(mid, a.to)
        # PATCH/move can return a complete message; only expose action metadata.
        response = response or {}
        result = {"id": response.get("id", mid), **body}
    if a.json:
        _json(result)
    elif a.dry_run:
        print("[torrkörning] Inget ändrat.")
        _json(result)
    else:
        print(f"Uppdaterat mejl: {result['id']}")
    return 0


def add_scope(s):
    scope = s.add_mutually_exclusive_group()
    scope.add_argument("--all-folders", action="store_true", help="include all your mailbox folders")
    scope.add_argument("--folder", metavar="ID", help="folder id or well-known name, default inbox")
    s.add_argument("--since", metavar="DATE", help="received at or after this ISO date/time; local if no offset")
    s.add_argument("--before", metavar="DATE", help="received before this ISO date/time (exclusive)")


def register(sub, count_type):
    for name, help_text, fn in (
        ("read", "read one mail without changing read status", cmd_read),
        ("thread", "read the whole conversation across your folders, oldest first", cmd_thread),
        ("attachments", "list attachment metadata without downloading files", cmd_attachments),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("message", help="Graph message id or mail:///messages/<id>")
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("find", help="indexed mail search (words or KQL), at most 1000 hits, sent time descending")
    s.add_argument("query", help="words or KQL, e.g. subject:invoice")
    s.add_argument("-n", "--number", "--limit", type=count_type, default=30)
    add_scope(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("save", help="download one selected attachment; never overwrite an existing file")
    s.add_argument("message")
    s.add_argument("attachment", help="attachment id from mail attachments")
    s.add_argument("-o", "--out", required=True, help="output file or existing directory")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_save)

    s = sub.add_parser("folders", help="list your mail folders and ids, or children of --parent")
    s.add_argument("--parent", metavar="ID", help="folder id or well-known name")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_folders)

    for action, help_text in (("mark", "mark a mail read or unread"),
                              ("flag", "set, complete or clear a follow-up flag"),
                              ("move", "move one mail to a chosen folder; returns its new id")):
        s = sub.add_parser(action, help=help_text)
        s.add_argument("message")
        if action == "mark":
            state = s.add_mutually_exclusive_group(required=True)
            state.add_argument("--read", dest="read", action="store_true")
            state.add_argument("--unread", dest="read", action="store_false")
        elif action == "flag":
            s.add_argument("--status", choices=("flagged", "complete", "notFlagged"), default="flagged")
        else:
            s.add_argument("--to", required=True, help="folder id or well-known name, e.g. archive")
        s.add_argument("--dry-run", action="store_true", help="preview the action, change nothing")
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=cmd_change, action=action)
