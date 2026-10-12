"""
Teams chat over Graph: post a message as the signed-in user.

Three things here that are easy to get wrong by hand:

1. **Graph does not render markdown.** A message sent as `contentType: text`
   shows `**bold**` with the asterisks, and a numbered list is one long line.
   `to_html` turns the markdown people (and assistants) write into the small HTML
   subset Teams shows, and `send` posts that as `contentType: html`. The
   converter is our own, standard library only: bold, italic, strikethrough,
   `code`, fenced code blocks, links, headings, quotes, tables and lists.
2. **A chat line is not a markdown line.** In a chat, a line break is a line
   break, and a list right under a line of text ("do this:\\n1. open") is a
   list. Markdown wants a blank line before the list and joins lines into one
   paragraph. Here a line break becomes `<br>` and a list needs no blank line.
3. **A 1:1 chat has an id nobody knows.** `POST /chats` with the two members
   returns the chat that already exists rather than making a second one, so
   `chat_with(address)` finds the chat to post in from a mail address alone.
   Group chats have no such lookup; post in one by its id (`19:...@thread.v2`).

Sending needs `ChatMessage.Send`, finding a 1:1 chat `Chat.Create`. Both are in
the default scope set.
"""

import html
import re
from urllib import parse

from .auth import graph

GRAPH = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------- markdown

_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _inline(s):
    """Escape the text and render the inline markup in it.

    Code spans are cut out first so that nothing inside them is touched: a
    `**kwargs` in backticks stays literally that.
    """
    held = []

    def hold(fragment):
        held.append(fragment)
        return f"\x00{len(held) - 1}\x00"

    s = re.sub(r"(`+)(.+?)\1", lambda m: hold(f"<code>{html.escape(m.group(2).strip())}</code>"), s)
    s = html.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\((\S+?)\)",
               lambda m: hold(f'<a href="{html.escape(html.unescape(m.group(2)))}">{m.group(1)}</a>'), s)
    s = re.sub(r"(?<![\"'>=])\b(https?://[^\s<]+[^\s<.,;:!?)\]])",
               lambda m: hold(f'<a href="{m.group(1)}">{m.group(1)}</a>'), s)
    s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", s)
    s = re.sub(r"\*(?=\S)(.+?)(?<=\S)\*", r"<em>\1</em>", s)
    s = re.sub(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", s)
    for _ in range(2):  # a link label may hold a code span
        s = re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], s)
    return s


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _starts_block(lines, i):
    """Whether line i opens something other than paragraph text."""
    line = lines[i]
    return bool(_FENCE.match(line) or _HEADING.match(line) or _HR.match(line)
                or _ITEM.match(line) or _QUOTE.match(line)
                or ("|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1])))


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _list(lines, i):
    """One list from line i: same kind (ordered or not), same indentation.

    An ordered and a bulleted list next to each other are two lists, as in
    Python-Markdown's sane_lists. A line indented further than the marker
    belongs to the item above it, which is how a list nests.
    """
    first = _ITEM.match(lines[i])
    assert first, "called on a list item"
    base = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    items = []
    while i < len(lines):
        m = _ITEM.match(lines[i])
        if not m or len(m.group(1)) != base or m.group(2)[0].isdigit() != ordered:
            break
        body = [m.group(3)]
        i += 1
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                # A blank line ends the item unless the next line is indented under it.
                if i + 1 < len(lines) and lines[i + 1].strip() and _indent(lines[i + 1]) > base:
                    body.append("")
                    i += 1
                    continue
                break
            if _indent(line) > base:
                body.append(line)
                i += 1
                continue
            if _ITEM.match(line) or _starts_block(lines, i):
                break
            body.append(line)  # a lazy continuation of the item's text
            i += 1
        items.append(body)
    start = int(first.group(2)[:-1]) if ordered else 1
    tag = "ol" if ordered else "ul"
    out = [f'<{tag} start="{start}">' if ordered and start != 1 else f"<{tag}>"]
    for body in items:
        nested = [body[0]] + [_dedent(b) for b in body[1:]]
        inner = _blocks(nested)
        # A tight item is its text, not a paragraph holding its text.
        if inner.startswith("<p>"):
            end = inner.index("</p>")
            inner = inner[3:end] + inner[end + 4:]
        out.append(f"<li>{inner}</li>")
    out.append(f"</{tag}>")
    return "".join(out), i


def _dedent(line):
    return re.sub(r"^ {1,4}|^\t", "", line)


def _blocks(lines):
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(m.group(1)):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
            continue
        m = _HEADING.match(line)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
            continue
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue
        if _ITEM.match(line):
            block, i = _list(lines, i)
            out.append(block)
            continue
        if _QUOTE.match(line):
            quoted = []
            while i < len(lines) and lines[i].strip():
                q = _QUOTE.match(lines[i])
                quoted.append(q.group(1) if q else lines[i])
                i += 1
            out.append(f"<blockquote>{_blocks(quoted)}</blockquote>")
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            head = _cells(line)
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>")
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not _starts_block(lines, i):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
    return "".join(out)


def to_html(text):
    """Markdown as written in a chat, to the HTML Teams shows.

    Plain chat text comes out looking as it was typed: one paragraph per
    blank-line-separated block, a line break where the text has one.
    """
    text = text.replace("\r\n", "\n").replace("\t", "    ").strip("\n")
    return _blocks(text.split("\n"))


# ---------------------------------------------------------------- Graph

def chat_with(address):
    """The id of the 1:1 chat with `address`, made if it does not exist yet.

    POST /chats answers with the chat the two already share, so this is a
    lookup as much as a create. It needs Chat.Create.
    """
    me = graph("GET", "/me?$select=id")["id"]
    member = "#microsoft.graph.aadUserConversationMember"
    body = {
        "chatType": "oneOnOne",
        "members": [
            {"@odata.type": member, "roles": ["owner"],
             "user@odata.bind": f"{GRAPH}/users('{me}')"},
            {"@odata.type": member, "roles": ["owner"],
             "user@odata.bind": f"{GRAPH}/users('{parse.quote(address)}')"},
        ],
    }
    return graph("POST", "/chats", body=body)["id"]


def send(chat_id, content_html):
    """Post one message in a chat as the signed-in user; returns Graph's message."""
    return graph("POST", f"/chats/{parse.quote(chat_id)}/messages",
                 body={"body": {"contentType": "html", "content": content_html}})
