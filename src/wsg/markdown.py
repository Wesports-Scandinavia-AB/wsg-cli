"""
Markdown to HTML, standard library only: what people (and assistants) write, as
the HTML that Teams and Outlook show.

Graph renders neither markdown nor line breaks in `contentType: text` the way
they were meant: `**bold**` keeps its asterisks in Teams, and a mail's code
looks like the rest of the text. `to_html` covers bold, italic,
strikethrough, `code`, fenced code blocks, links, headings, quotes, tables and
lists, with chat semantics: a line break is a line break, and a list right
under a line of text ("do this:
1. open") is a list without a blank line
before it.

Teams styles that HTML itself. A mail client does not, or does it badly
(Outlook on Windows renders with Word and ignores <style> in the head), so
`to_mail_html` puts the look on each tag as an inline style.
"""

import html
import re

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


# ---------------------------------------------------------------- mail

_MONO = "font-family:Consolas,'Courier New',monospace"
_LINE = "#d0d7de"
_TINT = "#f6f8fa"

# Inline styles per tag, in the order they are applied. <pre><code> comes before
# <code> so that a code block's code does not get the inline span's tint.
_MAIL_STYLES = [
    ("<pre><code>", f'<pre style="background:{_TINT};border:1px solid {_LINE};border-radius:4px;'
                    f'padding:10px 12px;margin:0 0 12px;white-space:pre-wrap;font-size:10pt;line-height:1.4;{_MONO}">'
                    f'<code style="{_MONO}">'),
    ("<code>", f'<code style="background:{_TINT};border:1px solid {_LINE};border-radius:3px;'
               f'padding:0 4px;font-size:90%;{_MONO}">'),
    ("<p>", '<p style="margin:0 0 12px">'),
    ("<h1>", '<h1 style="font-size:16pt;margin:18px 0 8px">'),
    ("<h2>", '<h2 style="font-size:14pt;margin:16px 0 8px">'),
    ("<h3>", '<h3 style="font-size:12pt;margin:14px 0 6px">'),
    ("<h4>", '<h4 style="font-size:11pt;margin:12px 0 6px">'),
    ("<h5>", '<h5 style="font-size:11pt;margin:12px 0 6px">'),
    ("<h6>", '<h6 style="font-size:11pt;margin:12px 0 6px">'),
    ("<ul>", '<ul style="margin:0 0 12px;padding-left:24px">'),
    # "<ol " (a list that starts at another number) before "<ol>", or the
    # styled "<ol style=" would be styled twice.
    ("<ol ", '<ol style="margin:0 0 12px;padding-left:24px" '),
    ("<ol>", '<ol style="margin:0 0 12px;padding-left:24px">'),
    ("<li>", '<li style="margin:0 0 4px">'),
    ("<blockquote>", f'<blockquote style="margin:0 0 12px;padding:0 0 0 12px;border-left:3px solid {_LINE};color:#57606a">'),
    ("<table>", '<table style="border-collapse:collapse;margin:0 0 12px">'),
    ("<th>", f'<th style="border:1px solid {_LINE};background:{_TINT};padding:4px 8px;text-align:left">'),
    ("<td>", f'<td style="border:1px solid {_LINE};padding:4px 8px;vertical-align:top">'),
    ("<hr>", f'<hr style="border:none;border-top:1px solid {_LINE};margin:16px 0">'),
]

MAIL_FONT = "font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt;line-height:1.4"


def to_mail_html(text):
    """Markdown to mail HTML: to_html with the look on every tag, in a font div.

    Only tags that to_html made are touched: the text itself is escaped, so a
    "<p>" a person wrote is "&lt;p&gt;" by now and stays text.
    """
    out = to_html(text)
    for tag, styled in _MAIL_STYLES:
        out = out.replace(tag, styled)
    return f'<div style="{MAIL_FONT}">{out}</div>'
