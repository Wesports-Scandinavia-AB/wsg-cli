"""
Mail over Graph beyond sendMail: answer in the thread, then archive the thread.

Three things here that are easy to get wrong by hand:

1. **A reply is not a new mail.** `sendMail` with "Re: " in the subject starts a
   new conversation: the recipient's client shows it apart from the thread, and
   the original is not quoted. `createReply` makes a draft that carries the
   thread's headers and the quote; the text goes into the draft's HTML above the
   quote (not as `comment`, which joins its lines into one). Attachments are
   added to that draft before it is sent.
2. **A thread is not one message.** Archiving the message that was answered
   leaves the rest of the conversation in the inbox, where it looks like it is
   still waiting. `archive` moves every message in the inbox that shares the
   answered message's `conversationId`.
3. **The connector's ids are Graph's ids.** The M365 connector hands out
   `mail:///messages/<id>`; the part after the last slash is the Graph message
   id, so both commands take either form. A message moved to Archive gets a new
   id there; the old one stops working.

Replying and moving need `Mail.ReadWrite` beside `Mail.Send`; both are in the
default scope set.
"""

import base64
import html
import mimetypes
import re
from pathlib import Path
from urllib import parse

from .auth import graph

# Graph caps a request at about 4 MB; base64 adds a third. Same ceiling as sendmail.
INLINE_LIMIT = 3 * 1024 * 1024


def message_id(ref):
    """The Graph id from a bare id or the connector's mail:///messages/<id>."""
    ref = ref.strip()
    if ref.startswith("mail:"):
        ref = ref.rstrip("/").rsplit("/", 1)[-1]
    return parse.unquote(ref)


def _path(mid):
    return "/me/messages/" + parse.quote(mid, safe="")


def get(mid, fields="id,subject,from,toRecipients,ccRecipients,replyTo,conversationId,receivedDateTime"):
    return graph("GET", f"{_path(mid)}?$select={fields}")


def me_address():
    me = graph("GET", "/me?$select=mail,userPrincipalName")
    return (me.get("mail") or me.get("userPrincipalName") or "").lower()


def _addr(r):
    return ((r or {}).get("emailAddress") or {}).get("address", "")


def reply_recipients(msg, reply_all, me):
    """Who a reply goes to, worked out the way Outlook does, for a dry run."""
    to = [_addr(r) for r in msg.get("replyTo") or []] or [_addr(msg.get("from"))]
    if to == [me]:
        # Answering your own mail goes to whoever it went to, as in Outlook; a
        # mail you sent yourself keeps you.
        to = [_addr(r) for r in msg.get("toRecipients") or []]
        return [a for a in to if a], []
    cc = []
    if reply_all:
        to += [_addr(r) for r in msg.get("toRecipients") or []]
        cc = [_addr(r) for r in msg.get("ccRecipients") or []]
    seen = set()

    def keep(a):
        k = a.lower()
        if not a or k == me or k in seen:
            return False
        seen.add(k)
        return True

    return [a for a in to if keep(a)], [a for a in cc if keep(a)]


def file_attachments(paths):
    """fileAttachment bodies for the paths, or raise ValueError over the ceiling."""
    out, total = [], 0
    for p in paths or []:
        f = Path(p)
        if not f.is_file():
            raise ValueError(f"Bilagan finns inte: {p}")
        raw = f.read_bytes()
        total += len(raw)
        out.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": f.name,
            "contentType": mimetypes.guess_type(f.name)[0] or "application/octet-stream",
            "contentBytes": base64.b64encode(raw).decode(),
        })
    if total * 4 // 3 > INLINE_LIMIT:
        raise ValueError(f"Bilagorna är {total // 1024} kB, för stort för ett inline-utskick "
                         "(~3 MB efter base64). Lägg filen i OneDrive och skicka en länk.")
    return out


def reply(mid, text, reply_all=False, attachments=None):
    """Answer in the thread; returns the sent draft's id."""
    action = "createReplyAll" if reply_all else "createReply"
    draft = graph("POST", f"{_path(mid)}/{action}", body={})
    # `comment` would be simpler, but Graph joins its lines into one (2026-10-13),
    # so the text goes into the draft's HTML right above the quote instead.
    lines = "<br>".join(html.escape(l) for l in text.split("\n"))
    content = draft["body"]["content"]
    head = re.search(r"<body[^>]*>", content, re.I)
    at = head.end() if head else 0
    content = content[:at] + f'<div style="font-family:Aptos,Calibri,sans-serif">{lines}</div><br>' + content[at:]
    graph("PATCH", _path(draft["id"]), body={"body": {"contentType": "HTML", "content": content}})
    for att in attachments or []:
        graph("POST", f"{_path(draft['id'])}/attachments", body=att)
    graph("POST", f"{_path(draft['id'])}/send")
    return draft["id"]


def thread_in_inbox(mid):
    """Every message in the inbox from the same conversation as `mid`."""
    conv = get(mid, "conversationId")["conversationId"]
    q = parse.urlencode({
        "$filter": f"conversationId eq '{conv}'",
        "$select": "id,subject,from,receivedDateTime",
        "$top": "100",
    })
    r = graph("GET", f"/me/mailFolders/inbox/messages?{q}")
    msgs = r.get("value", [])
    return sorted(msgs, key=lambda m: m.get("receivedDateTime", ""))


def archive(msgs):
    """Move the messages to the Archive folder; returns how many moved."""
    for m in msgs:
        graph("POST", f"{_path(m['id'])}/move", body={"destinationId": "archive"})
    return len(msgs)
