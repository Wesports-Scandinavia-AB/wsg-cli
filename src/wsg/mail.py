"""
Mail over Graph beyond sendMail: list mail, reply and archive threads.

Four things here that are easy to get wrong by hand:

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
3. **Graph does not render markdown.** A text body shows `**bold**` with its
   asterisks and code like the rest of the text, so a mail's text is rendered
   with `markdown.to_mail_html`, its look inline on every tag because Outlook
   on Windows ignores a <style> block. `plain` sends the text as it is.
4. **The connector's ids are Graph's ids.** The M365 connector hands out
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
import json
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
from urllib import parse
from urllib.error import HTTPError, URLError

from .auth import graph, graph_pages, AuthError, error_message
from .dates import InputError, bounds, moment, utc
from .markdown import MAIL_FONT, to_mail_html

# Graph caps a request at about 4 MB; base64 adds a third. Same ceiling as sendmail.
INLINE_LIMIT = 3 * 1024 * 1024
LIST_FIELDS = "id,receivedDateTime,from,subject,isRead,parentFolderId"
# Only asked for when a filter needs them: every listing would otherwise carry
# two recipient arrays it never reads.
PEOPLE_FIELDS = LIST_FIELDS + ",toRecipients,ccRecipients"
READ_FIELDS = LIST_FIELDS + ",toRecipients,ccRecipients,conversationId,body,hasAttachments,webLink,flag"
TEXT_HEADER = {"Prefer": 'outlook.body-content-type="text"'}


def _collection(path, headers=None):
    return graph_pages(path, headers=headers, fetch=graph)


def _mailbox(all_folders=False, folder=None):
    if all_folders and folder:
        raise InputError("Välj antingen --folder eller --all-folders.")
    return "/me/messages" if all_folders else f"/me/mailFolders/{parse.quote(folder or 'inbox', safe='')}/messages"


def people_on(msg):
    """Every name and address on a message: the sender, To and Cc.

    What "was X involved" means in practice. Graph can filter on the sender
    but not on a fragment across all three, so the match is local, like the
    sender filter beside it.
    """
    boxes = [msg.get("from") or {}, *(msg.get("toRecipients") or []), *(msg.get("ccRecipients") or [])]
    values = []
    for box in boxes:
        who = box.get("emailAddress") or {}
        values += [(who.get("name") or "").casefold(), (who.get("address") or "").casefold()]
    return values


def messages(n=30, senders=None, unread=False, all_folders=False, since=None, before=None, folder=None,
             involving=None):
    """Own inbox by default, newest first; n counts matches across Graph pages.

    Sender and participant fragments match locally; date/read filters run on
    Graph. The sort property comes first in the filter to avoid Graph's
    InefficientFilter.
    """
    if n < 1:
        raise InputError("Antalet måste vara minst 1.")
    terms = [s.strip().casefold() for s in senders or []]
    people = [s.strip().casefold() for s in involving or []]
    if any(not s for s in terms + people):
        raise InputError("Avsändarfiltret får inte vara tomt.")
    start, end = bounds(since, before)
    query = {
        "$select": PEOPLE_FIELDS if people else LIST_FIELDS,
        "$orderby": "receivedDateTime desc",
        "$top": 100 if terms or people else min(n, 100),
    }
    filters = []
    if start:
        filters.append(f"receivedDateTime ge {utc(start)}")
    if end:
        filters.append(f"receivedDateTime lt {utc(end)}")
    if unread:
        if not filters:
            filters.append("receivedDateTime ge 0001-01-01T00:00:00Z")
        filters.append("isRead eq false")
    if filters:
        query["$filter"] = " and ".join(filters)
    path = _mailbox(all_folders, folder) + "?" + parse.urlencode(query)
    out = []
    for msg in _collection(path):
        if unread and msg.get("isRead") is not False:
            continue
        sender = (msg.get("from") or {}).get("emailAddress") or {}
        values = [(sender.get(k) or "").casefold() for k in ("name", "address")]
        if terms and not any(term in value for term in terms for value in values):
            continue
        if people and not any(term in value for term in people for value in people_on(msg)):
            continue
        out.append(msg)
        if len(out) == n:
            break
    return out


def read(mid):
    return graph("GET", f"{_path(mid)}?" + parse.urlencode({"$select": READ_FIELDS}), headers=TEXT_HEADER)


def thread(mid, inbox=False, bodies=True):
    conv = get(mid, "conversationId")["conversationId"].replace("'", "''")
    query = parse.urlencode({"$filter": f"conversationId eq '{conv}'",
                             "$select": READ_FIELDS if bodies else LIST_FIELDS, "$top": 100})
    rows = list(_collection(_mailbox(not inbox) + "?" + query, TEXT_HEADER if bodies else None))
    return sorted(rows, key=lambda m: moment(m.get("receivedDateTime")) or datetime.min.replace(tzinfo=timezone.utc))


def find(query, n=30, all_folders=False, since=None, before=None, folder=None):
    """Graph's indexed KQL search (up to 1000 hits, sent time descending)."""
    if not query.strip() or not 1 <= n <= 1000:
        raise InputError("Söktext krävs och antalet måste vara mellan 1 och 1000.")
    start, end = bounds(since, before)
    search = f"({query})"
    # Date-only KQL narrows the index; exact timestamp boundaries are checked
    # locally because KQL date parsing has day precision.
    if start:
        search += f" AND received>={start:%Y-%m-%d}"
    if end:
        search += f" AND received<={end:%Y-%m-%d}"
    args = {"$search": json.dumps(search, ensure_ascii=False), "$select": LIST_FIELDS,
            "$top": 1000 if start or end else n}
    out = []
    for msg in _collection(_mailbox(all_folders, folder) + "?" + parse.urlencode(args)):
        when = moment(msg.get("receivedDateTime"))
        if (start or end) and (not when or (start and when < start) or (end and when >= end)):
            continue
        out.append(msg)
        if len(out) == n:
            break
    return out


class _BodyText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden and tag in ("br", "p", "div", "li", "tr", "blockquote"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "li", "tr", "blockquote"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def text_of(msg):
    body = msg.get("body") or {}
    content = body.get("content") or ""
    if body.get("contentType", "").lower() != "html":
        return content.strip()
    parser = _BodyText()
    parser.feed(content)
    return re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()


def attachments(mid):
    fields = "id,name,contentType,size,isInline"
    return list(_collection(f"{_path(mid)}/attachments?" + parse.urlencode({"$select": fields, "$top": 100})))


def save_attachment(mid, aid, destination):
    """Save exactly one selected file; exclusive creation prevents overwrites."""
    path = f"{_path(mid)}/attachments/{parse.quote(aid, safe='')}"
    meta = graph("GET", path + "?" + parse.urlencode({"$select": "id,name,contentType,size"}))
    if meta.get("@odata.type") == "#microsoft.graph.referenceAttachment":
        raise InputError("Detta är en molnlänk, inte en filbilaga. Öppna den i Outlook.")
    dest = Path(destination).expanduser()
    if dest.is_dir():
        # Treat both slash conventions as separators on every OS. Never use a
        # server-supplied filename to escape the selected directory.
        name = re.split(r"[/\\]", meta.get("name") or "attachment")[-1]
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")
        if not name or name in (".", ".."):
            name = "attachment"
        if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                                           *[f"LPT{i}" for i in range(1, 10)]}:
            name = "_" + name
        dest = dest / name
    if dest.exists():
        raise InputError("Målfilen finns redan. Välj ett annat filnamn.")
    content = graph("GET", path + "/$value", raw=True)
    try:
        with dest.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        raise InputError("Målfilen finns redan. Välj ett annat filnamn.") from None
    return {"id": aid, "name": meta.get("name"), "path": str(dest.resolve()), "bytes": len(content)}


# One lookup per distinct folder, kept for the length of the command: a thread
# touches two or three folders and they do not move while it runs.
_FOLDER_NAMES = {}


def folder_name(fid):
    """The folder's display name, or the id itself when it cannot be read."""
    if not fid:
        return None
    if fid not in _FOLDER_NAMES:
        try:
            got = graph("GET", f"/me/mailFolders/{parse.quote(fid, safe='')}?$select=displayName")
            _FOLDER_NAMES[fid] = got.get("displayName") or fid
        except (HTTPError, URLError, AuthError, OSError, ValueError):
            _FOLDER_NAMES[fid] = fid
    return _FOLDER_NAMES[fid]


def with_folders(rows):
    """Name the folder each message sits in, beside Graph's parentFolderId.

    Where a message is answers "have I already archived this thread"; the id on
    its own does not, and a thread is often half archived with only the newest
    messages left in the inbox.
    """
    for row in rows:
        row["folder"] = folder_name(row.get("parentFolderId"))
    return rows


def deeplink(msg):
    """A link to the message in Outlook on the web that actually opens.

    Graph's own webLink is the old OWA form on outlook.office365.com with an
    ItemID, and current Outlook on the web does not resolve it; deeplink/read
    does. The ItemID is taken from webLink rather than from the message id,
    because Graph encodes + as _ in the id and OWA wants standard base64.
    """
    link = msg.get("webLink") or ""
    if not link:
        return ""
    item = parse.parse_qs(parse.urlsplit(link).query).get("ItemID", [""])[0]
    return "https://outlook.office.com/mail/deeplink/read/" + parse.quote(item, safe="") if item else link


def with_links(rows):
    """Add `deeplink` beside each message, so a listing can be clicked into."""
    for row in rows:
        row["deeplink"] = deeplink(row)
    return rows


def folders(parent=None):
    path = f"/me/mailFolders/{parse.quote(parent, safe='')}/childFolders" if parent else "/me/mailFolders"
    return list(_collection(path + "?" + parse.urlencode({
        "$select": "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount", "$top": 100})))


def mark(mid, is_read):
    return graph("PATCH", _path(mid), body={"isRead": is_read})


def flag(mid, status="flagged"):
    if status not in ("flagged", "complete", "notFlagged"):
        raise InputError("Ogiltig flaggstatus.")
    return graph("PATCH", _path(mid), body={"flag": {"flagStatus": status}})


def move(mid, folder):
    if not folder.strip():
        raise InputError("En målmapp krävs.")
    return graph("POST", _path(mid) + "/move", body={"destinationId": folder})


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


def body_html(text, plain=False):
    """A mail's text as HTML: markdown rendered (bold, code, lists, tables), or
    with plain=True escaped as it is, a line break per line."""
    if plain:
        lines = "<br>".join(html.escape(l) for l in text.split("\n"))
        return f'<div style="{MAIL_FONT}">{lines}</div>'
    return to_mail_html(text)


def reply(mid, text, reply_all=False, attachments=None, plain=False):
    """Answer in the thread; returns the sent draft's id."""
    action = "createReplyAll" if reply_all else "createReply"
    draft = graph("POST", f"{_path(mid)}/{action}", body={})
    # `comment` would be simpler, but Graph joins its lines into one (2026-10-13),
    # so the text goes into the draft's HTML right above the quote instead.
    content = draft["body"]["content"]
    head = re.search(r"<body[^>]*>", content, re.I)
    at = head.end() if head else 0
    content = content[:at] + body_html(text, plain) + "<br>" + content[at:]
    graph("PATCH", _path(draft["id"]), body={"body": {"contentType": "HTML", "content": content}})
    for att in attachments or []:
        graph("POST", f"{_path(draft['id'])}/attachments", body=att)
    graph("POST", f"{_path(draft['id'])}/send")
    return draft["id"]


def thread_in_inbox(mid):
    """Every message in the inbox from the same conversation as `mid`."""
    return thread(mid, inbox=True, bodies=False)


def archive(msgs):
    """Move the messages to the Archive folder; returns how many moved."""
    for moved, m in enumerate(msgs):
        try:
            graph("POST", f"{_path(m['id'])}/move", body={"destinationId": "archive"})
        except (HTTPError, URLError, AuthError, OSError, ValueError) as e:
            raise InputError(f"{moved} meddelanden flyttades före felet; det sista anropets utfall "
                             f"kan behöva kontrolleras. {error_message(e)}") from None
    return len(msgs)
