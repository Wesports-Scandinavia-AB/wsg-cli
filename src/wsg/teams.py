"""
Teams chat over Graph: post a message as the signed-in user.

Six things here that are easy to get wrong by hand:

1. **Graph does not render markdown.** A message sent as `contentType: text`
   shows `**bold**` with the asterisks, and a numbered list is one long line.
   `to_html` (markdown.py, shared with mail) turns the markdown people (and
   assistants) write into the small HTML subset Teams shows, and `send` posts
   that as `contentType: html`. The converter is our own, standard library only: bold, italic, strikethrough,
   `code`, fenced code blocks, links, headings, quotes, tables and lists.
2. **A chat line is not a markdown line.** In a chat, a line break is a line
   break, and a list right under a line of text ("do this:\\n1. open") is a
   list. Markdown wants a blank line before the list and joins lines into one
   paragraph. Here a line break becomes `<br>` and a list needs no blank line.
3. **A 1:1 chat has an id nobody knows.** `POST /chats` with the two members
   returns the chat that already exists rather than making a second one, so
   `chat_with(address)` finds the chat to post in from a mail address alone.
   Reading must not make a chat, so `one_on_one(address)` looks through the
   chat list instead. A group chat is found by its topic or a member's name
   (`find_chats`) and used by its id (`19:...@thread.v2`).
4. **A mail address is not a user principal name.** `users('{address}')`
   resolves the object id and the UPN, nothing else, so binding a chat member
   to the address people write fails with `NotFound` for every mailbox whose
   UPN sits on another domain. That is the normal state after a merge or a
   domain change, and the address still works in mail, which makes the error
   look like a missing person. `resolve_user` asks the directory instead and
   matches the mail, the UPN and the proxy addresses, so any address the person
   answers on reaches them.
5. **Search wants a channel scope even for chats.** Graph's `/search/query`
   over chat messages requires `ChannelMessage.Read.All` beside `Chat.Read`,
   even when every hit is in a chat. With it (added 2026-10-12), `search` finds
   a word in the whole history, channels included. `find` is the fallback for
   a token without it: it reads the most recent chats and matches here, so it
   sees what is recent, not everything.

6. **A shared file is not in the chat.** It is a link into the sender's OneDrive,
   which `Files.ReadWrite` does not reach. `save_files` opens it through
   `/shares/{encoded url}` with `Files.Read.All` (added 2026-10-13) and fetches
   the bytes from the item's pre-signed download URL, which wants no token.

Sending needs `ChatMessage.Send`, finding a 1:1 chat to post in `Chat.Create`,
reading `Chat.Read`, searching `ChannelMessage.Read.All`, fetching a shared file
`Files.Read.All`. All of them are in the default scope set.
"""

import base64
import html
import os
import re
from urllib import parse, request

from .auth import get_token, graph
from .markdown import to_html  # noqa: F401  (cli.py calls teams.to_html)

GRAPH = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------- Graph

def resolve_user(address):
    """The directory user behind a mail address, in whatever form it is written.

    Matches the mailbox address, the user principal name and the proxy
    addresses (the aliases), case-insensitively, which covers every form a
    person could reasonably type for a colleague. Needs User.Read.All, which is
    in the default scopes; no advanced query header is required for the lambda.
    Raises LookupError when the directory has no one, or more than one.
    """
    quoted = address.replace("'", "''")
    terms = (f"mail eq '{quoted}' or userPrincipalName eq '{quoted}'"
             f" or proxyAddresses/any(p:p eq 'smtp:{quoted}')")
    hits = graph("GET", "/users?$select=id,displayName,userPrincipalName,mail"
                        f"&$filter={parse.quote(terms)}").get("value", [])
    if not hits:
        raise LookupError(f"Ingen användare i katalogen har adressen {address}.")
    if len(hits) > 1:
        who = ", ".join(f"{u.get('displayName')} <{u.get('mail') or u.get('userPrincipalName')}>"
                        for u in hits)
        raise LookupError(f"Flera användare matchar {address}: {who}. "
                          "Välj chatten med --chat, se wsg teams chats.")
    return hits[0]


def chat_with(address):
    """The id of the 1:1 chat with `address`, made if it does not exist yet.

    POST /chats answers with the chat the two already share, so this is a
    lookup as much as a create. It needs Chat.Create. The member is bound by
    object id, because the address alone resolves only when it doubles as the
    UPN (see 4 in the module docstring).
    """
    me = graph("GET", "/me?$select=id")["id"]
    try:
        them = resolve_user(address)["id"]
    except Exception:
        # Outside the directory (a federated chat partner), or a token without
        # User.Read.All: an existing chat still carries the address.
        existing = one_on_one(address)
        if existing:
            return existing
        raise
    member = "#microsoft.graph.aadUserConversationMember"
    body = {
        "chatType": "oneOnOne",
        "members": [
            {"@odata.type": member, "roles": ["owner"],
             "user@odata.bind": f"{GRAPH}/users('{me}')"},
            {"@odata.type": member, "roles": ["owner"],
             "user@odata.bind": f"{GRAPH}/users('{them}')"},
        ],
    }
    return graph("POST", "/chats", body=body)["id"]


def send(chat_id, content_html):
    """Post one message in a chat as the signed-in user; returns Graph's message."""
    return graph("POST", f"/chats/{parse.quote(chat_id)}/messages",
                 body={"body": {"contentType": "html", "content": content_html}})


def me_id():
    return graph("GET", "/me?$select=id")["id"]


def chats(limit=50):
    """The chats the signed-in user is in, most recent message first, with members."""
    out = []
    url = ("/me/chats?$expand=members,lastMessagePreview"
           "&$orderby=lastMessagePreview/createdDateTime%20desc&$top=50")
    while url and len(out) < limit:
        r = graph("GET", url)
        out.extend(r.get("value", []))
        url = r.get("@odata.nextLink")
    return out[:limit]


def chat_name(chat, me=None):
    """The topic, or the other members' names, which is what Teams shows."""
    if chat.get("topic"):
        return chat["topic"]
    names = [m.get("displayName") or m.get("email") or "?" for m in chat.get("members", [])
             if not me or m.get("userId") != me]
    return ", ".join(names) or "(ingen annan)"


def _matches(chat, query):
    q = query.casefold()
    hay = [chat.get("topic") or ""]
    for m in chat.get("members", []):
        hay += [m.get("displayName") or "", m.get("email") or ""]
    return any(q in h.casefold() for h in hay)


def find_chats(query, limit=200):
    """Recent chats whose topic, member name or member address holds `query`."""
    return [c for c in chats(limit) if _matches(c, query)]


def one_on_one(address, limit=500):
    """The id of the 1:1 chat with `address`, or None. Reads only.

    A chat member carries one address, which need not be the one the person
    was asked for, so the directory lookup decides who is meant and the member
    is recognised by object id. Without it (no directory read, or someone
    outside it) the written address is still compared to the member's own.
    """
    wanted = {address.casefold()}
    user_id = None
    try:
        user = resolve_user(address)
        user_id = user["id"]
        wanted |= {(user.get("mail") or "").casefold(),
                   (user.get("userPrincipalName") or "").casefold()} - {""}
    except Exception:
        pass
    for c in chats(limit):
        if c.get("chatType") != "oneOnOne":
            continue
        for m in c.get("members", []):
            if user_id and m.get("userId") == user_id:
                return c["id"]
            if (m.get("email") or "").casefold() in wanted:
                return c["id"]
    return None


def messages(chat_id, n=30, since=None):
    """The last `n` real messages in a chat, oldest first.

    System events (members added, calls) and deleted messages are left out.
    `since` is an ISO timestamp; reading stops at the first message older.
    """
    out = []
    url = f"/chats/{parse.quote(chat_id)}/messages?$top=50&$orderby=createdDateTime%20desc"
    while url and len(out) < n:
        r = graph("GET", url)
        page = r.get("value", [])
        for m in page:
            if since and m["createdDateTime"] < since:
                url = None
                break
            if m.get("messageType") == "message" and not m.get("deletedDateTime"):
                out.append(m)
        else:
            url = r.get("@odata.nextLink")
    out = out[:n]
    out.sort(key=lambda m: m["createdDateTime"])
    return out


def sender(m):
    who = (m.get("from") or {}).get("user") or (m.get("from") or {}).get("application") or {}
    return who.get("displayName") or "?", who.get("id")


def text_of(m):
    """A message's body as plain text: lists stay lists, images become [bild]."""
    raw = (m.get("body") or {}).get("content") or ""
    if (m.get("body") or {}).get("contentType") != "html":
        return raw.strip()
    t = re.sub(r"<img[^>]*>", "[bild]", raw)
    t = re.sub(r"<attachment[^>]*>\s*</attachment>", "", t)
    t = re.sub(r"<li>", "- ", t)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>|</pre>|</blockquote>|</tr>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def files_of(m):
    """Shared files (reference attachments) as (name, url)."""
    return [(a.get("name"), a.get("contentUrl")) for a in m.get("attachments", [])
            if a.get("contentType") == "reference"]


def save_images(m, folder):
    """Download the images pasted into a message; returns the paths, or why not.

    Pasted images are hostedContents behind a Graph URL that wants the token, which
    is why a browser link to them does not work for anyone else.
    """
    paths = []
    srcs = re.findall(r'<img[^>]+src="([^"]+)"', (m.get("body") or {}).get("content") or "")
    if not srcs:
        return paths
    os.makedirs(folder, exist_ok=True)
    token = get_token()
    for i, src in enumerate(srcs):
        p = os.path.join(folder, f"{m['id']}_{i}.png")
        if not os.path.exists(p):
            try:
                req = request.Request(html.unescape(src), headers={"Authorization": f"Bearer {token}"})
                with request.urlopen(req) as r, open(p, "wb") as f:
                    f.write(r.read())
            except Exception as e:
                p = f"(bilden gick inte att hämta: {e})"
        paths.append(p)
    return paths


def save_files(m, folder):
    """Download the files shared in a message: one path, or why not, per files_of entry."""
    paths = []
    for name, url in files_of(m):
        if not url:
            paths.append("(filen har ingen adress)")
            continue
        os.makedirs(folder, exist_ok=True)
        p = os.path.join(folder, os.path.basename(name or "") or f"{m['id']}.bin")
        if not os.path.exists(p):
            try:
                share = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
                item = graph("GET", f"/shares/{share}/driveItem")
                with request.urlopen(item["@microsoft.graph.downloadUrl"]) as r, open(p, "wb") as f:
                    f.write(r.read())
            except Exception as e:
                p = f"(filen gick inte att hämta: {e})"
        paths.append(p)
    return paths


def find(query, n_chats=25, since=None, per_chat=200):
    """Messages holding `query` in the `n_chats` most recent chats, newest first.

    Read here and matched here (see the module notes on why not /search/query).
    Returns (chat, message) pairs.
    """
    q = query.casefold()
    hits = []
    for c in chats(n_chats):
        for m in messages(c["id"], per_chat, since):
            if q in text_of(m).casefold():
                hits.append((c, m))
    hits.sort(key=lambda h: h[1]["createdDateTime"], reverse=True)
    return hits


def search(query, n=25):
    """Messages matching `query` (KQL, e.g. `from:anna faktura`), best first.

    Pages through /search/query 25 at a time. Each hit is a dict with id,
    createdDateTime, from, summary, chatId (or teamId/channelId for a channel)
    and webLink.
    """
    out = []
    while len(out) < n:
        size = min(25, n - len(out))
        r = graph("POST", "/search/query", body={"requests": [{
            "entityTypes": ["chatMessage"], "query": {"queryString": query},
            "from": len(out), "size": size}]})
        box = ((r.get("value") or [{}])[0].get("hitsContainers") or [{}])[0]
        hits = box.get("hits") or []
        for h in hits:
            res = h.get("resource") or {}
            ident = res.get("channelIdentity") or {}
            summary = re.sub(r"</?c0>|<ddd/>", "", h.get("summary") or "")
            out.append({
                "id": res.get("id"),
                "createdDateTime": res.get("createdDateTime"),
                "from": ((res.get("from") or {}).get("emailAddress") or {}).get("name") or "?",
                "summary": html.unescape(re.sub(r"\s+", " ", summary)).strip(),
                "chatId": res.get("chatId"),
                "teamId": ident.get("teamId"),
                "channelId": None if res.get("chatId") else ident.get("channelId"),
                "webLink": res.get("webLink"),
            })
        if not hits or not box.get("moreResultsAvailable"):
            break
    return out[:n]
