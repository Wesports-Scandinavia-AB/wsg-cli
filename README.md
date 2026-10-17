# wsg

Sign in once per service, reuse the token from any script. No app registration,
no secrets: you sign in as yourself and the scripts get what you are allowed to
do. Three services today:

- `wsg graph` — Microsoft Graph, signed in with your Entra account
- `wsg arena` — WeSports Arena, signed in with Arena's own OAuth (no Cloudflare Access since 2026-09-20)
- `wsg secret` — one-time links on secrets.wesports.io, no sign-in
- `wsg link` — short links on go.wesports.io, with a service token

## Install

Through Claude: give it the link to [INSTALL-FOR-CLAUDE.md](INSTALL-FOR-CLAUDE.md)
and it installs wsg, writes the skill and walks you through the sign-in.

By hand:

```
python -m pip install --user git+https://github.com/Wesports-Scandinavia-AB/wsg-cli
python -m wsg skill install
python -m wsg graph login
```

or, for development:

```
git clone https://github.com/Wesports-Scandinavia-AB/wsg-cli
pip install -e wsg-cli
```

Requires Python 3.9+. No third party dependencies. `wsg update` reinstalls from
GitHub and rewrites the skill; on a clone, `git pull` does it instead.

The IT commands (`wsg user`, `device`, `intune`, `admin`, ...) live in a separate,
private package, wsg-admin-cli, which adds them to this `wsg` when it is installed
beside it.

## Use

```
wsg graph login      prints a URL, open it in your browser, sign in
wsg graph status     who is signed in, token expiry, granted scopes
wsg graph token      prints an access token, refreshed silently
wsg graph whoami     calls Graph /me
wsg graph sendmail   send mail as yourself, markdown from stdin, --dry-run first
wsg graph logout     deletes the cache

wsg arena login      sign in to WeSports Arena (prints the URL, loopback redirect)
wsg arena status     the Arena session on this machine, token expiry
wsg arena token      prints a live Arena access token
wsg arena whoami     calls Arena /api/me
wsg arena get PATH   GET any Arena API path, prints JSON
wsg arena companies      the registry: id, legal name, org no
wsg arena integrations   integration rows for one company
wsg arena integration-add    create one (--key, --instance, --config JSON)
wsg arena secrets        secret names and metadata — never a value
wsg arena secret-set     store or rotate a secret, value read from stdin
wsg arena consumers      machine consumers and what they may read
wsg arena grant-add      let a consumer read secrets (grant-remove withdraws)
wsg arena incidents  open Site Watch incidents and warnings
wsg arena ack X      acknowledge an incident (id or domain)
wsg arena resolve X  close an incident, --false-positive, --cause, --learning
wsg arena reopen X   undo a resolve
wsg arena logout     deletes the Arena session

wsg planner plans      the Planner boards you can see, grouped by owner
wsg planner tasks X    one board, in board order, by bucket — --all for completed
wsg planner mine       tasks assigned to you, nearest due first
wsg planner task ID    one task with its description and checklist
wsg planner link X     the address of one board, --bare for the URL alone

wsg teams send         post in a Teams chat as yourself, markdown in, --dry-run first
wsg teams chats [X]    your chats, most recent first, filtered by topic or member
wsg teams read         one chat, oldest first: --chat ID, --to ADDRESS or --name TEXT
wsg teams find TEXT    search your chats and channels for TEXT, the whole history
wsg teams md           print the Teams HTML for a markdown text, send nothing
wsg mail reply ID      answer a mail in its thread, markdown from stdin, --dry-run first
wsg mail md            print the mail HTML for a markdown text (-o FILE to view it), send nothing
wsg mail archive ID    move every message in a mail's thread from the inbox to Archive

wsg secret create      encrypt a value or --file here, print a one-time link
wsg secret delete      destroy a link before it is opened

wsg link create        shorten a URL: for ever, or gone after one use
wsg link list          your short links
wsg link revoke        withdraw one

wsg skill install      write the Claude skill to ~/.claude/skills/wsg/
wsg update             get the latest wsg-cli from GitHub and refresh the skill

```

The 0.1 spellings `wsg login`, `wsg token`, `wsg status`, `wsg whoami` and
`wsg logout` still work as aliases for the `graph` commands, so old scripts
keep running. New scripts should say `graph`.

## Graph

`wsg graph login` does not open a browser by itself. Pass `--open` for the default
browser, or `--browser edge` to pick one. Use the browser that is signed in to
your work account on a joined device: Conditional Access policies that require
a compliant device only see the device through that browser session.
On a machine with no browser (a server), use `wsg graph login --device`.

The refresh token lives 90 days and is renewed each time it is used, so after
one login the scripts stay silent until you change password or go 90 days
without running anything.

### From PowerShell

```powershell
$H = @{ Authorization = (wsg graph token --header) }
if ($LASTEXITCODE) { throw "Not signed in, run: wsg graph login" }
Invoke-RestMethod -Headers $H -Uri https://graph.microsoft.com/v1.0/me
```

### From Python

```python
from wsg import graph

token = graph.get_token(["Files.ReadWrite"])   # raises AuthError if login is needed
me = graph.graph("GET", "/me")
```

### Scopes

`wsg graph login` asks for the base set in `src/wsg/scopes.py`: your own mail,
calendar, files and chats, files others have shared with you, Planner, and reading the directory for names. That is
also exactly what the app is consented for, so there is no consent prompt and no
admin role involved.

`wsg graph token --scopes X` fails with exit code 2 if X was not granted, so scripts
can tell the user exactly what to do.

### How it works

Authorization code flow with PKCE against "WeSports CLI", our own public client
app in the Vartex tenant (no secret). The CLI listens on a random localhost port,
you sign in in the browser, the browser redirects back to localhost with the code.
That is a normal interactive sign-in, so Conditional Access sees your device and
your MFA. Device code flow is often blocked by policy, which is why it is not the
default.

Until 0.5.0 (2026-10-09) the client was Microsoft Graph Command Line Tools, the one
Connect-MgGraph uses. Entra puts every scope consented for a client into its
tokens, whatever the login asks for, so a shared first-party client hands out
whatever any tool ever got consented for it. Our own app is consented for the base
set and nothing else, so the token holds what this list says. A token cached by an older version
cannot be refreshed by the new client and reads as signed out: run
`wsg graph login` once.

Cache: `%LOCALAPPDATA%\wsg\token.json` on Windows, `~/.config/wsg/token.json`
elsewhere. Override with `WSG_TOKEN_CACHE`. The tenant is Vartex's, override with
`WSG_TENANT`; the client with `WSG_CLIENT_ID`.

## Arena

Arena (`https://arena.wesports.io`) has its own sign-in (Tribune) since
Cloudflare Access came off it on 2026-09-20, and a terminal cannot carry the
browser's session cookie. `wsg arena login` therefore uses Arena's OAuth server:
authorization code + PKCE with a loopback redirect, the page that asks being
Arena's own. It prints the URL and never opens a browser.

Nothing to set up first. wsg signs in as `cl_wsg_cli`, a public client
registered in Arena with audience `api` (since 2026-10-09): no secret, since a
secret on your own disk protects nothing, while PKCE and the loopback redirect
protect the flow. Its tokens open `/api` with your rights and never `/mcp`.

A client of your own still works and wins when configured — register it in
wesports-arena with `node scripts/mcp-client.mjs create --name "wsg — <name>" --api`,
and put the id and secret in `%LOCALAPPDATA%\wsg\arena.json`
(`~/.config/wsg/arena.json` elsewhere), or in `WSG_ARENA_CLIENT_ID` /
`WSG_ARENA_CLIENT_SECRET`:

```json
{"client_id": "cl_...", "client_secret": "amcs_..."}
```

```
wsg arena login                            prints a URL; sign in and allow
wsg arena get /api/me
```

The access token lives an hour and renews itself with the refresh token, which
lasts 30 days and rotates on every use. Scripts should treat exit code 2 as
"run `wsg arena login`". The token is a normal bearer token:

```powershell
$H = @{ Authorization = "Bearer " + (wsg arena token); "User-Agent" = "my-script" }
Invoke-RestMethod -Headers $H -Uri https://arena.wesports.io/api/me
```

```python
from wsg import arena
me = arena.get("/api/me")             # raises AuthError if login is needed
arena.api("POST", "/api/...", body={...})
```

## Sending mail

`sendmail` posts to `/me/sendMail` as the signed-in user. It delivers — it does not
create a draft — and keeps a copy in Sent Items unless you pass `--no-save`.

```
wsg graph sendmail --to someone@example.com --subject "Subject" --dry-run <<'EOF'
Body here, line breaks intact.
EOF
```

The body is read from stdin so a here-doc keeps its formatting, which a shell
argument would mangle. It is markdown, rendered the way `teams send` renders it
(bold, italic, `code`, code blocks, links, headings, quotes, tables, lists), with
the look written inline on every tag: Outlook on Windows renders mail with Word
and ignores a `<style>` block. `--plain` sends the text as it is, `--html` sends
HTML you wrote yourself, and `wsg mail md -o page.html` shows what a text will
look like without sending anything. `--dry-run` prints the exact request and sends nothing; use
it first, because there is no unsend.

`--send-at 2026-10-15T08:00` sends later. A time without an offset is local time.
The message goes to Exchange right away with the deferred-send property set
(`PidTagDeferredSendTime`), so it waits in Outbox and leaves on time even if this
machine is off. Delete it from Outbox to cancel.

Attachments go inline as `#microsoft.graph.fileAttachment` with base64 in
`contentBytes`, so Graph caps the whole request at roughly 3 MB. `--attach` is
repeatable and the limit is checked against the *encoded* size before sending —
base64 costs a third on top, and Graph rejects the entire request rather than the
attachment. Above that ceiling the file needs `createUploadSession` against a draft,
or OneDrive and a link; this command refuses up front rather than failing halfway.

Needs `Mail.Send`, which is in the default scope set.

### Replying in the thread, archiving the thread

```
wsg mail reply <message-id> --dry-run <<'EOF'
Thursday works, see you then.
EOF
wsg mail reply <message-id> --all --attach notes.pdf --archive <<'EOF'
...
EOF
wsg mail archive <message-id> --dry-run
```

A reply sent with `sendmail` and "Re:" in the subject is a new conversation: the
recipient sees it apart from the thread, without the quote. `mail reply` goes through
`createReply` (`createReplyAll` with `--all`), so the answer carries the thread's
headers and the quoted original. The text is markdown, as with `sendmail`
(`--plain` for text as it is), and is written into the draft's HTML above the
quote rather than passed as `comment`, which joins its lines into one. Attachments are
added to the draft before it is sent, under the same ~3 MB ceiling as `sendmail`.
`--dry-run` shows who it would go to, worked out as Outlook does: the sender (or
Reply-To), plus To and Cc with `--all`, minus yourself.

`mail archive` moves every message in the inbox that shares the message's
`conversationId` to the Archive folder, so the whole thread leaves the inbox, not only
the message that was answered. `reply --archive` does both in one go. A moved message
gets a new id in Archive.

The message id is Graph's, or the M365 connector's `mail:///messages/<id>`, which
carries the same id. Needs `Mail.ReadWrite` beside `Mail.Send`; both are in the
default scope set.

## Teams messages

`teams send` posts in a chat as the signed-in user. `--to ADDRESS` finds the 1:1
chat with that person — `POST /chats` returns the one you already share, or makes
it — and `--chat ID` posts in a chat you know the id of, such as a group chat.

```
wsg teams send --to someone@example.com --dry-run <<'EOF'
**Done!** Here's how:
1. Open settings
2. Paste the `key`
EOF
```

Graph does not render markdown: sent as text, `**bold**` keeps its asterisks.
The text is therefore markdown, converted to the HTML subset Teams shows (bold,
italic, strikethrough, inline code, code blocks, links, headings, quotes, tables
and lists) and sent as `contentType: html`. It reads like chat rather than strict
markdown: a line break stays a line break, and a list directly under a line of
text is a list. `--html` sends HTML as it is; `wsg teams md` prints the HTML
without sending.

For scripts and loops: `--json` prints `chatId`, `id` and `createdDateTime`, and
`--sent-log FILE` appends the message id to a file, so a loop can tell its own
messages from the ones a person typed.

Reading:

```
wsg teams chats "anna"                         chats whose topic or member matches
wsg teams read --name "Anna Berg" -n 50        one chat, oldest first, local times
wsg teams read --to someone@example.com --since 2026-10-12 --images ./img
wsg teams read --name "Anna Berg" -n 5 --files ./files   also save the files shared there
wsg teams find "invoice"                       search chats and channels, whole history
wsg teams find "from:anna invoice" --json      KQL works; JSON has chatId and webLink
```

`read --to` looks the 1:1 chat up in your chat list rather than through
`POST /chats`, so reading never makes a chat. `--name` takes the one chat whose
topic or member matches; a whole person's name picks the 1:1 chat with them, and
anything ambiguous lists the candidates instead of guessing. `--images DIR`
downloads pasted images, which sit behind a Graph URL that wants the token.
`--files DIR` downloads the files shared in the chat. Such a file lives in the
sender's OneDrive, not in the chat, so it needs `Files.Read.All` (from 0.9.0):
delegated, it opens only what you could already open in Teams.
`--json` gives raw `createdDateTime`, sender id, text, images and files.

`find` goes through Graph's `/search/query`, which covers your whole history in
chats and the channels you can see. For chat messages it wants
`ChannelMessage.Read.All` beside `Chat.Read` — even when every hit is in a chat —
which is why that scope is in the base set from 0.8.0. A login without it falls
back to reading your most recent chats and matching locally, and says so;
`--recent` (with `--chats` and `--days`) does that on purpose.

Needs `ChatMessage.Send`, `Chat.Create` for `send --to`, `Chat.Read`,
`ChannelMessage.Read.All` for `find` and `Files.Read.All` for `read --files`; all
five are in the default scope set.

## One-time secret links

A password, an API key or a credentials file goes to someone else as a link on
[secrets.wesports.io](https://secrets.wesports.io) that burns on first read — never
as text in a mail or a Teams thread, which are archived, forwarded and indexed. Put
the link in the mail instead.

```
wsg secret create --file client_secret.json --ttl 7d --title "GA4 OAuth client"
printf '%s' "$API_KEY" | wsg secret create
wsg secret create --passphrase          # prompts for the value, then a passphrase
wsg secret delete <id> <burn-token>     # before it is opened
```

No sign-in: the service is public. The value is encrypted here, with the same
envelope the service's own create page builds, and the key exists only in the
link's `#fragment`, which never reaches the server. So the link alone is the
secret — whoever holds it can open it, once.

- **The value comes from stdin or `--file`, never an argument**, for the same reason
  as `secret-set`. A file is sent byte for byte; from a pipe, one trailing line
  ending is dropped.
- **stdout is the link alone**, so it pipes into a mail. Expiry and the delete
  command go to stderr. `--json` gives link, id, burn token and expiry together.
- `--ttl` is `1h`, `1d` (default) or `7d`; `--views` 1–10, default 1.
- **`--title` is readable by anyone with the link** before it is opened. Say what
  it is, never what it says.
- `--passphrase` weaves a passphrase into the key: the link alone then opens
  nothing. Send it another way. A wrong passphrase still burns the secret — the
  server cannot tell it from a right one.

AES-GCM is implemented in plain Python in `onetime.py`, since the package has no
dependencies and the standard library has no AES. It was checked against the
`cryptography` package and against the service's reveal endpoint.

## Short links

A long URL as a short one on go.wesports.io. The lifetime is the interesting
part: a link can last for ever, or be gone the moment somebody uses it.

```
wsg link create "https://long.example/..."                 unlimited, no expiry
wsg link create "https://..." --once                       spent on first use
wsg link create "https://..." --times 5                    works five times
wsg link create "https://..." --until 2026-10-23           through the end of that day, UTC
wsg link create "https://..." --name offsite-2026          choose the short name
wsg link list [--all]                                      yours; --all includes spent ones
wsg link revoke SLUG                                       it stops working for everyone
```

- **This is not how a password travels.** A short link forwards, it hides
  nothing. Secrets go through `secret create` above.
- `--name` only works on a link with no use limit. One that can run out gets a
  random name, because a name that can be guessed is a one-use link a stranger
  can spend before the recipient opens their mail.
- Pasting a one-use link into Teams or Outlook does not spend it. The service
  tells a person clicking from a preview fetching, and only the first spends a
  use.
- stdout is the link alone so it pipes; the lifetime and the revoke command go
  to stderr.

Unlike `secret`, this needs a credential: creating a link goes through the
service's machine door, behind its own Cloudflare Access application. Put the
service token in `go.json` in the config directory (`%LOCALAPPDATA%\wsg` on
Windows, `~/.config/wsg` otherwise), or in `WSG_GO_CLIENT_ID` and
`WSG_GO_CLIENT_SECRET`:

```json
{"client_id": "....access", "client_secret": "..."}
```

secrets.wesports.io is public and needs nothing; go.wesports.io is not, because
an open redirector on a wesports.io hostname would be a phishing tool wearing
our own domain.

## The work calendar

`calendar mirror` blocks time on your work calendar from a booking that lives in a
private one. It reads the Google event as JSON on stdin, the same way `sendmail`
reads a body, because the booking already exists over there and retyping it into
arguments is where two calendars start to drift apart.

```
pkj calendar show <id> --json | wsg calendar mirror
pkj calendar add -s Tandläkare --start "imorgon 09" --work      # the same thing in one go
```

**The title never comes along.** The mirrored booking is called `🔒 Upptagen`
and carries no description, no location and no guests, with `showAs: busy` and
`sensitivity: private`. What is on a private calendar stays there; the work
calendar learns only that the time is taken.

**Travel time is blocked too, by default.** Half an hour either side, as its own
block called `Restid`, so the trip there and back is not bookable either:

```
09:15 Blodtryckskontroll  (Google)   ->   08:45-09:15  🚗 Restid    (Outlook)
                                          09:15-09:45  🔒 Upptagen
                                          09:45-10:15  🚗 Restid
```

The emoji is the only decoration either title carries, and it is chosen to say
as little as the words do: a closed lock and a car.

`--restid 45m` moves it, `--no-restid` books the appointment alone. The travel
blocks carry the same link as the booking, so `show` lists all three and
`unmirror` removes all three — travel is part of the mirror, not something
separate to remember. An all-day event gets none: there is no journey to
midnight.

Running it twice does not book it twice. Graph has `transactionId` for this: the
same id posted again hands back the event it already made, so a mirror is safe to
re-run from a script that cannot remember what it did last time.

Times go over as the instant they are, in UTC. Wall-clock plus a timezone is how a
booking ends up an hour out on the far side of a clock change. All-day events are
the exception, since a day starts at midnight where you live rather than in UTC,
so those carry the mailbox's own timezone — the string Graph itself reports, and
therefore one Graph accepts.

Recurring series are refused. Google's `RRULE` and Graph's `patternedRecurrence`
are different models, and a half-right translation of an every-other-week rule is
worse than an honest no: mirror the individual occurrences you want blocked.

`calendar show <google-id>` says whether a booking is mirrored and where.
`calendar unmirror <google-id> --yes` removes the block and leaves the Google
event alone.

Needs `Calendars.ReadWrite`.

## Planner

`wsg planner` reads the Planner boards, and only reads them — nothing here
writes a task, moves one, or ticks a box.

```
wsg planner plans                    every board you can see, grouped by owner
wsg planner tasks "Vartex Service"   one board, in board order, by bucket
wsg planner tasks <plan-id> --all    completed tasks included
wsg planner mine                     what is assigned to you, nearest due first
wsg planner task <task-id>           one task with description and checklist
wsg planner link "Vartex Service"    the board's address, to paste somewhere
wsg planner plans --link             the whole list with addresses, not ids
wsg planner task <task-id> --link    a link to that one card
```

`tasks` takes a plan id or a fragment of the title. A fragment that matches more
than one board lists them with their ids and stops, rather than printing the
wrong board — "Vartex" alone matches four.

Open tasks only, unless `--all`. A task is `○` not started, `◐` in progress or
`●` done, which is the whole of what Planner tracks: `percentComplete` is 0,
1–99 or 100 and there is no separate status field. Buckets and tasks print in
board order, taken from the `orderHint` strings rather than sorted by title —
a board sorted alphabetically is one nobody recognises.

Due dates print as the date Planner itself shows. They are stored as midnight
UTC with no time of day, so converting them to local time is a conversion of
something that was never a moment; west of Greenwich it lands a day early and
the task reads as overdue before it is.

`plans` groups by what owns the board. That is usually a group or a team, but a
personal board is owned by the account that made it and a shared list by a
roster — a roster is not a directory object and has no name to look up, so it
prints as "Delad lista".

What you see is what you are a member of. A board someone only sent a link to is
not in `plans`, and Graph answers that with an empty list rather than an error,
so a missing board looks exactly like an empty one. Reading it by its plan id
still works.

### Links

Graph hands out no `webUrl` for a plan or a task the way it does for a file or a
calendar event, so `link` assembles the address instead of reading it:

```
https://planner.cloud.microsoft/webui/plan/<plan-id>/view/board?tid=<tenant>
```

The tenant comes from the `tid` claim in your own cached token. It matters when
you are signed into more than one: without it the web app picks whichever
session it finds first and says you have no access to a board you can read
perfectly well.

`--bare` prints the URL and nothing else, for piping into a mail or `clip`.
`--legacy` builds the old `tasks.office.com` form instead, which still redirects
— and which is the only one that opens a task without knowing its board, should
the new host ever move again.

Needs `Tasks.Read`, which was added to the default scopes later than most of
them. An account that signed in before that gets HTTP 403 from every command
here until `wsg graph login` runs once more, and the error says so.

## The secrets vault

`secret-set` stores or rotates one credential in a company's vault. It needs
`company.secrets.write` at group scope, and Arena audits every write.

**The value is read from stdin and there is no `--value` flag.** An argument is
visible in shell history and, on a shared machine, in the process list — so the
safe path is the only path. Piped in, exactly one trailing line ending is stripped
(the shell's, not the secret's); on a terminal you are prompted without echo.

```
wsg arena integration-add --company co_vartex --key garp --instance nps_zidcore     --config '{"db_host":"…","db_port":"1433","db_name":"…"}'

printf '%s' "$PASSWORD" | wsg arena secret-set --company co_vartex     --name GARP_DB_PASSWORD --integration 01M2… --dry-run
```

`--dry-run` says whether it would create or rotate, and how many characters it
read, without sending anything. Use it first: a value that arrived truncated or
with a stray newline authenticates nowhere, and the vault cannot be read back to
check.

A stored value never comes back — the API returns metadata only (name, a short
hint, timestamps). Rotation is the same command as creation. `--integration` is a
partial patch server-side: omitting it leaves an existing link alone, so rotating
a credential cannot silently orphan it.

`--company` takes an id (`co_vartex`) or a unique fragment of a legal name. An
ambiguous fragment lists the candidates and refuses, because these commands write
to a named company's vault and the wrong one is not a mistake you notice later.

## Machine grants

`grant-add` decides what a machine consumer (a Dagster pipeline, say) may read from
the vault. Three shapes, narrowest first:

```
--secret NAME --company REF     one secret
--key KEY --company REF         every secret linked to that company's integration
--key KEY --all-companies       the same, for every company that has that platform
```

The last one is wide and stays wide: it covers companies onboarded **after** the
grant, with no edit and no notification. That is the point for a pipeline that runs
group-wide, and the reason `--all-companies` has to be typed rather than inferred
from an omitted `--company`. `--dry-run` prints what would change and sends nothing.

Both wider shapes resolve when the secret is read, so a disabled integration
delivers nothing and revoking is immediate.

`wsg arena incidents` lists Site Watch incidents (severity `acute`) and
warnings (`watch`). Open ones by default; `--status resolved --days 7` for
last week's, `--severity acute` for incidents only, `--id X` for one incident
with its timeline, `--json` for scripts. From Python: `arena.incidents()`.

`ack`, `resolve` and `reopen` take an incident id or a domain, as long as the
domain has exactly one incident in the relevant state. `ack` only needs
`sitewatch.read`, the other two need `sitewatch.write`. `resolve --cause "..."
--learning "..."` writes the after-the-fact note in the same go.

Send a real `User-Agent`: the zone's bot rules answer 403 to `Python-urllib`.
Override the app with `WSG_ARENA_URL`, the token cache with `WSG_ARENA_TOKEN_CACHE`.

