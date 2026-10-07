# wsg

Sign in once per service, reuse the token from any script. No app registration,
no secrets: you sign in as yourself and the scripts get what you are allowed to
do. Three services today:

- `wsg graph` — Microsoft Graph, signed in with your Entra account
- `wsg arena` — WeSports Arena, signed in with Arena's own OAuth (no Cloudflare Access since 2026-09-20)
- `wsg secret` — one-time links on secrets.wesports.io, no sign-in

## Install

```
pipx install git+https://github.com/Wesports-Scandinavia-AB/wsg-cli
```

or, for development:

```
git clone https://github.com/Wesports-Scandinavia-AB/wsg-cli
pip install -e wsg-cli
```

Requires Python 3.9+. No third party dependencies.

## Use

```
wsg graph login      prints a URL, open it in your browser, sign in
wsg graph status     who is signed in, token expiry, granted scopes
wsg graph token      prints an access token, refreshed silently
wsg graph whoami     calls Graph /me
wsg graph sendmail   send mail as yourself, body from stdin, --dry-run first
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

wsg user find X        search the directory by name, UPN or mail
wsg user stale         tenant-wide: accounts nobody uses, and what they cost
wsg user create UPN    create the account in on-prem AD, where it has to start
wsg user show X        account state, licences, sign-in history, last changes
wsg user audit X       who changed the account, when, and what
wsg user signins X     sign-in log, interactive and token refreshes
wsg user groups X      group and directory role memberships
wsg user password X    set a new password — in AD if the account is synced
wsg user alias X ADDR  add (or --remove) a mail alias, in AD for a synced account
wsg user onprem-find X who in the AD forest holds this name, deleted ones too
wsg user onprem-check  can we reach the DC, and does Entra Connect run there

wsg device find X      search by serial, device name or user
wsg device show X      who has it, last check-in, Autopilot record, sign-in log
wsg device user X      hand it over: primary user in Intune and in Autopilot
wsg device reset X     Autopilot reset — wipe the profiles, keep the enrolment
wsg device retire X    remove company data, leave the rest
wsg device sync X      ask this device to check in now

wsg intune apps        managed apps with their assignments
wsg intune app-status  per-user install status for one app
wsg intune intent      what the service intends for a user on a device
wsg intune store-app   add a Microsoft Store app (new) by Store id
wsg intune win32       publish an .intunewin package
wsg intune sync        ask a device to check in now

wsg ongoing accounts   the configured Ongoing accounts (never the key)
wsg ongoing last       how long since the warehouse last got an order, per channel

wsg garp last          latest order per channel in Garp, and what has no picking list

wsg garpdb check       who the login is on the SQL instance, and what it may do
wsg garpdb databases   every database on the instance, and which ones you may open
wsg garpdb tables      tables in one database with row counts and size
wsg garpdb query SQL   run one SELECT, --db to pick the database, --json for JSON

wsg planner plans      the Planner boards you can see, grouped by owner
wsg planner tasks X    one board, in board order, by bucket — --all for completed
wsg planner mine       tasks assigned to you, nearest due first
wsg planner task ID    one task with its description and checklist
wsg planner link X     the address of one board, --bare for the URL alone

wsg secret create      encrypt a value or --file here, print a one-time link
wsg secret delete      destroy a link before it is opened

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

`wsg graph login` asks for the full list in `src/wsg/scopes.py`. Some of them need an
admin role in the tenant. If you do not have that, sign in with a narrower set:

```
wsg graph login --scopes User.Read,Files.ReadWrite,Mail.Send
```

`wsg graph token --scopes X` fails with exit code 2 if X was not granted, so scripts
can tell the user exactly what to do.

### How it works

Authorization code flow with PKCE against the Microsoft Graph Command Line
Tools public client (the one Connect-MgGraph uses). The CLI listens on a random
localhost port, you sign in in the browser, the browser redirects back to
localhost with the code. That is a normal interactive sign-in, so Conditional
Access sees your device and your MFA. Device code flow is often blocked by
policy, which is why it is not the default.

Cache: `%LOCALAPPDATA%\wsg\token.json` on Windows, `~/.config/wsg/token.json`
elsewhere. Override with `WSG_TOKEN_CACHE`. Tenant defaults to `organizations`,
override with `WSG_TENANT`.

## Arena

Arena (`https://arena.wesports.io`) has its own sign-in (Tribune) since
Cloudflare Access came off it on 2026-09-20, and a terminal cannot carry the
browser's session cookie. `wsg arena login` therefore uses Arena's OAuth server:
authorization code + PKCE with a loopback redirect, the page that asks being
Arena's own. It prints the URL and never opens a browser.

It needs a client registered in Arena with audience `api` — one per person,
whose tokens open `/api` with that person's rights and never `/mcp`:

```
node scripts/mcp-client.mjs create --name "wsg — Tommy" --api   in wesports-arena, then apply the SQL
```

Put the id and secret it prints in `%LOCALAPPDATA%\wsg\arena.json`
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
argument would mangle. `--dry-run` prints the exact request and sends nothing; use
it first, because there is no unsend.

Attachments go inline as `#microsoft.graph.fileAttachment` with base64 in
`contentBytes`, so Graph caps the whole request at roughly 3 MB. `--attach` is
repeatable and the limit is checked against the *encoded* size before sending —
base64 costs a third on top, and Graph rejects the entire request rather than the
attachment. Above that ceiling the file needs `createUploadSession` against a draft,
or OneDrive and a link; this command refuses up front rather than failing halfway.

Needs `Mail.Send`, which is in the default scope set.

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

## Devices

`wsg device` answers the two questions a laptop on a desk actually raises: who
has it, and can somebody else take it over.

```
wsg device find PF464                  partial serial, name or user
wsg device show PF46487Q               the whole picture
wsg device user PF46487Q --set person@example.com
wsg device reset PF46487Q              Autopilot reset, then hand it over
```

One laptop is three objects with three keys, and none of them links to the
others by name. The **Autopilot identity** is the hardware hash, keyed by serial
number; it survives every wipe and carries the group tag and the assigned user.
The **managed device** is the Intune enrolment, with its own GUID, a primary
user and the check-in times. The **Entra device** is what Conditional Access
evaluates, keyed by a third id. `show` fetches all three and prints them as one
machine.

`show` leads with the check-in date and the sign-in log because that is what
says whether the machine is free. `usersLoggedOn` is the last logon the client
reported *at its last check-in*, so a laptop that has been in a drawer since May
still names whoever used it in May. The sign-in log is the second opinion: it
names everyone, not just the last one, and it is where a machine that quietly
changed hands shows up. It keeps 30 days, so an empty log means "nothing in the
window", never "nobody used it".

A partial serial is the one search that needs care. On `managedDevices`,
`$filter=contains(serialNumber,'PF46487Q')` returns the device but
`contains(serialNumber,'PF464')` returns an empty list rather than an error —
the serial is indexed for equality only. `find` therefore falls back to a
client-side sweep when the server side comes back empty, so a half-typed serial
still finds the machine instead of silently looking like no such machine exists.

Handing a machine over is two writes, and `wsg device user` does both: the
primary user in Intune, which decides who it is listed under and which
user-targeted assignments it gets, and the assigned user on the Autopilot
record, which is the name at the sign-in screen after a reset. Where a profile
is assigned to a specific user, leaving the old name there means the new person
cannot complete the setup.

Then the reset, and the three are not interchangeable:

| | enrolment | Entra join | user profiles | personal data |
|---|---|---|---|---|
| `reset` | kept | kept | gone | gone |
| `reset --factory` | gone | gone | gone | gone |
| `retire` | gone | gone | kept | kept |

`reset` is the handover: the machine comes back at the Autopilot sign-in screen,
still ours, ready for the next person. `--factory` also drops the Intune and
Entra objects; the Autopilot registration survives either way because it lives
on the hardware hash. `retire` is for a machine somebody keeps.

All three need `DeviceManagementManagedDevices.PrivilegedOperations.All`, all
three take effect at the device's next check-in, and none of them can be called
back after that. They ask for the serial number typed back rather than a y/n,
and `--yes` is the only way past the prompt — a piped stdin is refused, so a
script cannot wipe a laptop by accident.

## Intune

App management over Graph beta, using the same cached token as everything else.
Needs `DeviceManagementApps.ReadWrite.All` for the write commands and
`DeviceManagementManagedDevices.ReadWrite.All` for `sync`.

```
wsg intune apps ChatGPT
wsg intune store-app 9PLM9XGG6VKS --name ChatGPT --publisher OpenAI --assign all
wsg intune win32 pkg.intunewin --name "Claude Desktop" --publisher "Anthropic, PBC" \
    --install "powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1" \
    --uninstall "..." --detect-path "%LOCALAPPDATA%\AnthropicClaude" \
    --detect-file claude.exe --assign <group-id> --intent required
```

`--run-as` defaults to `user`, which is what per-user installers need. Squirrel
and Electron apps unpack into `%LOCALAPPDATA%`; published as `system` they land
in the SYSTEM profile and the user gets nothing, with no error anywhere.

Detection is always file existence, never a version comparison, because
self-updating apps drift away from a pinned version on first launch.

`--intent available` only reaches devices that have Company Portal. `required`
installs through the management extension without it. An empty `app-status`
means no client has reported yet, which is not the same as "not assigned" —
`wsg intune intent <user> <device>` is the authoritative check, and it updates
long before Company Portal's own catalogue does.

Graph beta retires endpoints without notice: `deviceStatuses`, `installSummary`
and `getDeviceInstallStatusReport` are all gone, which is why `app-status` goes
through `reports/getUserInstallStatusReport` and why every failure prints the
Graph error body rather than a bare status code.

## Ongoing WMS

`wsg ongoing` goes straight at the warehouse over the Goods Owner REST API, with
no backend in between. It answers one thing well: has the warehouse actually got
an order, and when was the last one.

```
wsg ongoing accounts
wsg ongoing last --days 30 --top 5000
wsg ongoing last --account rawbike
```

```
LAGRET   senaste order per kanal, ur senaste 30 dygnen
         täcker tillbaka till 2026-09-01 11:18 (12 d 9 h sedan, svagast: vartex)

  vartex     63                     6 d 22 h  500020943        Skickad
  vartex     89                       16 min  3752764          Frisläppt

31 kanaler. Hämtningen slog i taket på 5000 ordrar, så en kanal som saknas säger ingenting — höj --top.
```

**Read the coverage line before the list.** There is no "latest per channel" query
in Ongoing, so this sweeps recent orders and groups them client-side, which means
an absent channel may be silent or may simply not have been fetched. Two numbers
keep that honest: how far back the sweep reached, and whether `--top` cut it
short. When it did, absence carries no information at all.

The reach is the **weakest account's**, not the oldest order seen. Accounts are
swept separately and a quiet one covers the whole window while a busy one is cut
off after days — reporting the oldest row anywhere would advertise the quiet
account's coverage as everyone's. Measured against a live 30-day sweep the two
differ by more than half the window: 12 days of real coverage, 30 days if you
take the flattering number.

What going direct costs: Ongoing knows nothing about the webshop or the ERP, so a
silent channel here is a fact, not a diagnosis — it cannot tell "Garp never
released it" from "nobody ordered".

### Channel names and groups

Ongoing has no marketplace register. `marketPlace` is a bare code on the order and
nothing enumerates it: `/orders/classes` and `/orders/types` both come back empty
on this instance. The names come from pulse, which maps every code to a storefront
in `MARKETPLACE_MAP` and groups the storefronts in its storefront-groups
migration. `scripts/channels-from-pulse.py` reads both into
`%LOCALAPPDATA%\wsg\channels.json`; run it again when pulse gains a storefront.

A code the map has not heard of prints as a bare code and is named at the bottom
of the output. That is deliberate: a channel pulse was never told about should
look new, not quietly borrow a neighbour's name.

The map is stamped with the goods owner whose numbering it is, and is not applied
to the others. The empty code is why — on Vartex it means the vartex.se dealer
portal, but a Rawbike order with no `marketPlace` is simply Rawbike's, and
labelling it vartex.se would be a plain lie.

Groups cover only the domains that migration named, so most channels land under
"Ogrupperade i pulse". Matching is on the exact domain and nothing else: pulse
calls two of the channels "traningsmaskiner DK" and "traningsmaskiner NO", which
are labels rather than domains, and guessing that they belong with
traningsmaskiner.com would be inventing the answer instead of reporting it.

### Credentials

Ongoing issues API users, not user sessions, so there is no signing in as
yourself: it is a stored credential per goods owner, and the separate legal
entities have their own. `WSG_ONGOING_URL/_USER/_KEY/_GOODS_OWNER` for one
account, otherwise `%LOCALAPPDATA%\wsg\ongoing.json` with an `accounts` list
(override the path with `WSG_ONGOING_CONFIG`).

`scripts/ongoing-config-from-azure.ps1` builds that file from the `Ongoing__*`
app settings on `fa-example`, printing only the account names. Run it again
after a key rotation. Everyone sharing a key is the same identity in Ongoing's
log, which is Ongoing's model rather than a shortcut taken here — and the reason
this module reads and never writes.

Timestamps are warehouse local time. One carrying an offset is taken as given; a
bare one is read as this machine's local clock. Windows ships no tz database for
`zoneinfo` and this package has no dependencies, so that assumption is the limit
rather than a lookup — and outgoing time filters are sent in local time for the
same reason, since a UTC boundary would shift the window by the offset every
time, always in the same direction.

## Garp SQL

`wsg garp` asks the Business Server on 8443. `wsg garpdb` reads the SQL Server on
the same host, `192.0.2.7\SQLEXPRESS` on 1433, where the ERP mirror lands next to
the databases the integrations own — price engines, feeds, Quartz schedules, Svea
reconciliation, e37 staging. Twelve business databases in all; `databases` lists
them and says which ones your login may actually open.

```
wsg garpdb check
wsg garpdb databases
wsg garpdb tables --db PriceEngine
wsg garpdb query "SELECT TOP 5 Ordernummer FROM OrderHuvud ORDER BY Ordernummer DESC"
wsg garpdb query --db Quartz --json "SELECT * FROM QueuedJob" | jq .
```

A long statement is easier to pipe in than to quote:

```powershell
@'
SELECT TOP 20 Artikelnr, [Benämning]
FROM Artikel
WHERE [Benämning] LIKE '%cykel%'
'@ | wsg garpdb query
```

Garp's column names are Swedish and often contain spaces or a hyphen — `Benämning`,
`Kod förs-rabatt`, `Lev-fl` — so bracket anything that is not a single plain word.
Åäö survive the whole round trip, argument or stdin.

### Credentials

The login is `arena_dagster_read`: `db_datareader` and `db_denydatawriter` in
every business database, so it reads everything and writes nothing. Put it in
`%LOCALAPPDATA%\wsg\garpdb.json`:

```json
{
  "user": "arena_dagster_read",
  "password": "..."
}
```

`server` and `database` may also go in that file; without them the defaults are
`192.0.2.7\SQLEXPRESS,1433` and `NPS_Zidcore`. Every field can be overridden per
call with `WSG_GARPDB_SERVER`, `_DATABASE`, `_USER` and `_PASSWORD`, which is
what CI should use rather than writing the file.

Only statements starting with `SELECT` or `WITH` leave the machine. That is a
typo guard, not a permission model — the real protection is `db_denydatawriter`
on the server, which no client can talk its way past. `wsg garpdb check` warns
loudly if the configured login turns out to be `sysadmin`, because for such a
login the deny does not apply and nothing here would stop a mistake.

The queries run through `powershell.exe` and `System.Data.SqlClient` rather than
pyodbc, which needs an ODBC driver this machine does not have. The instance sits
on a private address behind a self-signed certificate, so certificate validation
is off for that host and the server is unreachable from outside the VPN.

## Scripts

Standalone tools that do not need a Graph token. They sit in `scripts/` and are
run directly, not through `wsg`.

### az-vm-diskreport.sh

Where the disk on an Azure Windows VM went. The scan runs as SYSTEM on the guest
through `az vm run-command`, so it needs no agent, no RDP session and no inbound
port — only Reader plus
`Microsoft.Compute/virtualMachines/runCommand/action`.

```
scripts/az-vm-diskreport.sh -g RG-EXAMPLE -n vm-example
scripts/az-vm-diskreport.sh -g RG -n VM -s users --top 20
scripts/az-vm-diskreport.sh -g RG -n VM -s all > report.txt
```

Four sections. `volumes` and `profiles` answer in seconds; `tree` and `users`
walk every file and take upwards of ten minutes on a full disk, so redirect them
to a file rather than watching the terminal.

`tree` buckets by the first two path segments, `users` groups
`AppData\<Local|Roaming>\<vendor>\<product>` and sums each app over every
profile — which is what turns "lots of big profiles" into a named cause. On the
session host this was written against, it named Teams at 83 GB spread over the
classic and the new client.

Three things the script exists to get right:

`Get-ChildItem -Recurse` follows junctions on Windows PowerShell 5.1, and every
user profile carries legacy ones such as `Application Data`. The walk re-enters
the same tree until it hits `MAX_PATH`, counting profiles many times over — a
first pass on a 511 GB disk reported a single folder at 1 013 GB. The walker
here skips `ReparsePoint` entries instead. Check the result: `tree`'s total
should land within a gigabyte of size minus free from `volumes`.

`run-command` truncates its output at roughly 4 KB. Everything is aggregated on
the guest and only a ranked head comes back; never stream a file list through
it.

`Win32_UserProfile.LastUseTime` is touched by background servicing, so on a busy
session host every profile looks used within the last few days and the property
cannot find stale ones. `profiles` compares ProfileList registrations against
the directories that actually exist instead. A large gap means profiles were
deleted without their directories, and a pile of `TEMP.<name>.NNN` directories
means profile loads have been failing for a long time — usually an FSLogix agent
whose services run without `HKLM:\SOFTWARE\FSLogix\Profiles`, leaving every
session on a local profile that nothing ever prunes.

On Git Bash, `az` rewrites anything shaped like a POSIX path: resource IDs need
`MSYS_NO_PATHCONV=1` and `--scripts @file` needs a native Windows path from
`cygpath -w`. The script does both.

## Users

`wsg user show` answers "what state is this account in, and who touched it" in
one call. `X` is a UPN, an object id, or enough of a display name to be unique -
an ambiguous name lists the candidates rather than guessing.

```
wsg user show person@example.com
wsg user audit person@example.com --days 30 -v
wsg user signins person@example.com --failed
```

Four things the raw Graph output gets wrong if you read it too quickly:

- **`lastSignInDateTime` counts failed attempts.** A blocked account keeps
  showing recent sign-ins because a client somewhere is still retrying.
  `senast lyckad inloggning` (`lastSuccessfulSignInDateTime`) is the one that
  means someone got in.
- **Non-interactive sign-ins are hidden by default.** Graph returns interactive
  sign-ins only unless the filter asks for more, so `wsg user signins` runs both
  queries and merges them. `--kinds interactive` gets the old behaviour.
- **The audit log is not history.** Entra keeps directory audits 30 days with
  P1/P2, 7 without, and refuses a query that reaches further back instead of
  returning what it has - so the commands clamp to the real cut-off and print
  the window they actually covered. For an older offboarding, `wsg user show`
  falls back to `licenser borttagna` and `sessioner spärrade`, which are stored
  on the user object and do not expire.
- **A synced account was probably changed in AD, not in Entra.** When
  `typ` says `synkat från lokalt AD`, the Entra audit record names AD Connect,
  and the person who made the change is only in the on-prem AD security log.

### Accounts nobody is using

```
wsg user stale                       # the whole tenant, silent 90+ days
wsg user stale --days 180 --limit 40
wsg user stale --sku SPB             # who is holding the licence you need
wsg user stale --domain cykloteket.se
wsg user stale --domain vartex.se --domain wesports.se --days 365
wsg user stale --json
```

`--domain` matches any address the account answers to, not just the UPN: the
primary address and every `smtp:` alias in `proxyAddresses` count, so a mailbox
whose UPN is `hej@wesports.se` turns up under `--domain cykloteket.se` when it
carries that alias. A B2B guest is matched on their own address too - the UPN
of a guest is the invited address with the `@` rewritten to an underscore, so
matching the UPN alone would never find them by their own domain.

One sweep, two different questions. **A licensed account nobody uses is money**:
the report puts each SKU next to how many of its licences sit on dormant
accounts, and marks the ones that are fully consumed, because that is where the
next hire gets stuck. **An unlicensed account that is still enabled is reach**:
it appears in no licence review precisely because it is free, and a synced one
goes on authenticating on-prem long after the cloud has forgotten it.

Three things the raw numbers get wrong on their own:

- **`lastSignInDateTime` counts failed attempts, and interactive only.** A
  mailbox a client keeps polling has an old interactive timestamp and a fresh
  non-interactive one, so reading either alone calls a working account dead.
  Dormancy here is the most recent of `lastSuccessfulSignInDateTime` and
  `lastNonInteractiveSignInDateTime`.
- **"Never used" also describes a shared mailbox.** Nobody signs in as one, so
  it looks identical to an abandoned account - and the answer for it is to
  convert it to a shared mailbox, which needs no licence at all, rather than to
  close it. The report prints how old a never-used account is for the same
  reason: a new hire who has not started yet reads exactly like a five-year-old
  ghost without it.
- **An account in an OU that is not synced is not in this report at all.** It
  has no Entra object to be dormant, while AD keeps it enabled and keeps
  guarding its UPN. `wsg user onprem-find` is the one that sees those.

`signInActivity` needs AuditLog.Read.All and Entra ID P1; without the licence
the property is simply absent, which from here is indistinguishable from an
account that has never been used.

### Creating an account

```
wsg user create person@example.com --generate --like bo.exempel --dry-run
wsg user create person@example.com --generate --like bo.exempel --sync
wsg user create person@example.com --generate --ou "OU=Konsulter,OU=Vartex,DC=ad,DC=vartex,DC=se" --disabled
```

**The account is born in AD, not in Entra.** In a hybrid tenant that is not a
preference: a cloud account cannot be adopted by AD afterwards without a hard
match on `onPremisesImmutableId`, so an account created in the wrong half has
to be deleted and made again. The command opens a WinRM session to a domain
controller, runs `New-ADUser` there, adds the groups, and optionally starts a
delta sync and waits for the account to turn up in Entra.

Only the UPN is required. `person@example.com` gives the display name
`Ada Exempel`, the samAccountName `ada.exempel` and the same address in
the `mail` attribute, and every part of that can be overridden - `--given-name`,
`--surname`, `--display-name`, `--cn`, `--sam`, `--mail`, plus `--title`,
`--department`, `--company`, `--office`, `--mobile`, `--employee-id`,
`--manager` and `--description`. Deriving a name from an address is guesswork,
so the plan prints every field it worked out before anything is written, and
`--dry-run` stops there.

**`--like` copies the OU and the groups from a colleague**, which is how a new
person in an existing role should be set up: `--ou` overrides the OU it found,
`--group` adds more groups, and `--no-copy-groups` takes the OU alone.

Four things are checked before the DC is asked to write anything, because each
one is silent afterwards:

- **The UPN is not already in Entra.** A cloud account with the same name does
  not merge with a new AD object; the sync collides with it, and someone has to
  clean up two accounts by hand.
- **The OU exists, and there is one at all.** `New-ADUser` without `-Path` drops
  the account in the domain default container, which the OU filter in Entra
  Connect usually does not include - the account then exists in AD and nowhere
  else, and nothing reports an error. There is no sane default, so the command
  refuses rather than guess: `--ou`, `"ou"` in `onprem.json`, `WSG_AD_OU`, or
  `--like`.
- **The samAccountName and the UPN are free in the whole forest**, and the
  samAccountName fits in AD's 20 characters. Forest-wide is the point: AD
  enforces UPN uniqueness across every domain, and counts objects a plain
  `Get-ADUser` never returns - other domains, and deleted ones the Recycle Bin
  still holds. Missing those is how a create passes every check and then dies
  inside `New-ADUser` with *not unique forest-wide*, an error that names neither
  the object nor where it is. `wsg user onprem-find X` asks the same question on
  its own and prints what it found. (A long firstname.lastname is the other
  half: it quietly becomes an invalid samAccountName, and AD's answer does not
  mention the length.)
- **The UPN suffix is a verified domain in the tenant.** AD writes any suffix it
  is given; Entra Connect then replaces an unverified one with
  `...@<tenant>.onmicrosoft.com`. The account syncs, so it looks right, and the
  address handed to the new person does not work. `--force` creates it anyway.

**When AD already has the account, `--adopt` puts that object back into service**
instead of adding a second one. An offboarded account here is moved to
`OU=!Disabled Users` rather than deleted, which puts it outside Entra Connect's
OU filter: Entra cannot see it, so `wsg user show` says the name is free, while
AD goes on guarding its UPN forest-wide. The same person coming back should get
the same object - the SID goes with it, and with the SID every on-prem
permission ever granted to them. `--adopt` moves it into the target OU, writes
the attributes, resets the password, enables it and adds the groups it does not
already have. The ones it does have are left alone and printed, because old
access follows the object and is worth a look. What it cannot bring back is the
cloud side: the Entra object was deleted when the account fell out of scope, so
the licence gives a new and empty mailbox.

The first password takes the same route as every other secret here: `--generate`
prints it on stdout and the report on stderr, or it is read from stdin. The
account must change it at first sign-in unless `--no-force-change` says
otherwise, and `--disabled` creates it blocked, for a start date that has not
arrived yet.

**What is left afterwards is in the cloud**: the licence - and with it the
mailbox in Exchange Online - Entra groups, and MFA registration. None of that
can happen before the account has synced, which is what `--sync` and the wait
are for. Where Exchange is still on-prem, the mailbox is created there with
`Enable-RemoteMailbox`, not from here.

### Resetting a password

```
wsg user password person@example.com --generate
wsg user password person@example.com --generate --revoke-sessions
pass show tempwd | wsg user password person@example.com -y
```

The password is generated here or read from stdin, and prompted for with echo
off on a terminal. There is no `--value`: an argument lands in the shell history
and in the process list, and this is a credential. `--generate` prints the new
password on stdout and everything else on stderr, so it pipes straight into a
password manager. `--dry-run` shows the account and stops.

By default the user must change the password at next sign-in; `--no-force-change`
leaves it standing, and `--mfa` makes them pass MFA before the change.

**Which half owns the password decides the route, and the command works it out.**
A synced account goes to AD, a cloud account to Entra, and neither needs a flag.
`--entra` forces the cloud route for the rare case that wants it. The environment is hybrid
with password hash sync on and *password writeback off*, so for the ~480 synced
accounts the password lives in on-prem AD: Entra refuses the write, and the next
hash sync would overwrite it if it did not. The command reads
`passwordWritebackEnabled` from the tenant and stops rather than sending a
request that fails, and points at `--on-prem`, which resets in AD instead. The
257 cloud accounts go straight to Entra. Guest accounts are refused either way -
a B2B guest authenticates in its own tenant and has no password here.

Three things worth knowing before the reset:

- **The scope is not the permission.** `User.ReadWrite.All` gets the request
  through, but Entra still requires a directory role that may reset passwords -
  Helpdesk, User or Password Administrator. Without one, Graph answers 403
  *Insufficient privileges*, which reads exactly like missing consent. Resetting
  an account that itself holds an admin role needs an equal or stronger role.
- **A synced account belongs to AD.** With `typ: synkat från lokalt AD` the
  password lives on-prem and the reset only goes through if password writeback
  is on in Entra Connect - which it is not here, see above. Note that
  `passwordSyncEnabled` is the other direction and says nothing about this.
- **A new password does not sign anyone out.** Refresh tokens issued before the
  reset keep working, which for a compromised account means the attacker stays
  in. `--revoke-sessions` invalidates them; clients are out within the hour, and
  access tokens already issued live until they expire.

### Mail aliases on a synced account

```
wsg user alias person@example.com person@example.com --dry-run
wsg user alias person@example.com person@example.com --sync
wsg user alias person@example.com person@example.com --remove
```

A synced account's addresses belong to AD the same way its password does:
Exchange Online refuses to edit them and Graph cannot write `proxyAddresses` at
all. The alias is added in AD as one `smtp:` value - `-Add`, never `-Replace`,
which would drop the primary with it - and then waits for Entra to show it.

Before anything is written the address is checked three ways: its domain must
be verified in the tenant (Exchange silently drops one that is not), no user or
group in Entra may hold it, and no object in AD may hold it either - an account
outside the sync scope can, where Entra never sees it. `--dry-run` runs all
three and stops. A cloud account is refused with a pointer to Exchange, where
its addresses live.

**An empty list in AD is normal and invisible from Entra.** Many accounts
have no `proxyAddresses` on-prem at all; Entra then shows a primary derived from
`mail`, and Exchange adds the `onmicrosoft.com` routing address itself. The first
value written makes AD's list the one that counts, so a lone `smtp:` alias would
leave the account without an `SMTP:` primary. In that case the command writes the
primary Entra shows today in the same change, and `--dry-run` says so.

An alias only receives mail. It is not a sign-in name: that is the UPN, and it
is also what a SAML app gets as NameID unless the claim says otherwise.

### Synced accounts: resetting in AD

```
wsg user onprem-check
wsg user password person@example.com --generate
wsg user password person@example.com --generate --sync
```

This is the default for a synced account; `--on-prem` only says it out loud. It
opens a WinRM session to a domain controller and runs
`Set-ADAccountPassword -Reset` there, then waits for Entra to show the new
password date. The account is addressed by the `onPremisesDistinguishedName`
Entra already holds, so the DC writes exactly the object the report was about
rather than matching a name. A locked-out account is unlocked at the same time
(`--no-unlock` leaves it), and the password takes the same route as everywhere
else here: generator or stdin, into powershell.exe over its stdin, to the DC
inside the encrypted WinRM channel. Never argv, and never the environment.

**A delta sync does not carry the password.** `--sync` starts
`Start-ADSyncSyncCycle -PolicyType Delta`, which moves directory attributes;
password hash sync is a separate channel that runs every 2 minutes on its own.
That is why the command verifies by polling `lastPasswordChangeDateTime` in
Entra instead of trusting the sync cycle. Not seeing it inside `--wait` seconds
is reported as "not yet", not as a failure - the AD write already happened.

**Your own password is asked for at each run**, and is not stored. It is the
credential that opens the session to the DC - a different secret from the new
password being set, which still comes from `--generate` or stdin. Nothing is kept
on disk because this machine is Entra joined but not a domain member, and a
domain credential at rest there is worth more to an attacker than the
convenience is worth to us. Where the command has to run unattended,
`Get-Credential | Export-Clixml $env:LOCALAPPDATA\wsgd-cred.xml` writes a
DPAPI-encrypted file, and its existence switches the prompt off.

Setup, once per machine:

```
# the DC, the account that signs in to it, and where new accounts go,
# in %LOCALAPPDATA%\wsg\onprem.json
{"host": "192.0.2.4", "user": "you@ad.example.com",
 "ou": "OU=Personal,OU=Vartex,DC=ad,DC=vartex,DC=se"}

# only on a machine outside the domain: WinRM will not NTLM to an unknown host
# without this. Elevated shell, and scoped to the one address.
Set-Item WSMan:\localhost\Client\TrustedHosts -Value 192.0.2.4 -Concatenate -Force

wsg user onprem-check                  # says which of these is still missing
```
