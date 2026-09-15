# wsg

Sign in once per service, reuse the token from any script. No app registration,
no secrets: you sign in as yourself and the scripts get what you are allowed to
do. Two services today:

- `wsg graph` — Microsoft Graph, signed in with your Entra account
- `wsg arena` — WeSports Arena, signed in through Cloudflare Access

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

wsg arena login      sign in to WeSports Arena through Cloudflare Access
wsg arena status     who is signed in to Arena, session expiry
wsg arena token      prints the Access JWT for Arena
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
wsg user show X        account state, licences, sign-in history, last changes
wsg user audit X       who changed the account, when, and what
wsg user signins X     sign-in log, interactive and token refreshes
wsg user groups X      group and directory role memberships
wsg user password X    set a new password, generated or read from stdin
wsg user onprem-check  can we reach the DC, and does Entra Connect run there

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

Arena (`https://arena.wesports.io`) sits behind Cloudflare Access, which
federates to Entra. The app only trusts the JWT Access issues, so the Graph
token from `wsg login` does not work there. `wsg arena` drives `cloudflared`
for the browser login and the token cache in `~/.cloudflared`:

```
winget install Cloudflare.cloudflared      once
wsg arena login                            cloudflared prints a URL and opens the default browser
wsg arena get /api/me
```

The session lasts as long as the Access app allows (hours, not 90 days), so
scripts should treat exit code 2 as "run `wsg arena login`". The `get` command
sends the token as `cf-access-token`; do the same from your own code:

```powershell
$H = @{ "cf-access-token" = (wsg arena token); "User-Agent" = "my-script" }
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

## The work calendar

`calendar mirror` blocks time on your work calendar from a booking that lives in a
private one. It reads the Google event as JSON on stdin, the same way `sendmail`
reads a body, because the booking already exists over there and retyping it into
arguments is where two calendars start to drift apart.

```
pkj calendar show <id> --json | wsg calendar mirror
pkj calendar add -s Tandläkare --start "imorgon 09" --work      # the same thing in one go
```

**The title never comes along.** Every mirrored event is called `Upptagen` and
carries no description, no location and no guests, with `showAs: busy` and
`sensitivity: private`. What is on a private calendar stays there; the work
calendar learns only that the time is taken.

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
Override the app with `WSG_ARENA_URL`, the binary with `WSG_CLOUDFLARED`.

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

**Which half owns the password decides the command.** The environment is hybrid
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

### Synced accounts: resetting in AD

```
wsg user onprem-check
wsg user password person@example.com --on-prem --generate
wsg user password person@example.com --on-prem --generate --sync
```

`--on-prem` opens a WinRM session to a domain controller and runs
`Set-ADAccountPassword -Reset` there, then waits for Entra to show the new
password date. The account is addressed by the `onPremisesDistinguishedName`
Entra already holds, so the DC writes exactly the object the report was about
rather than matching a name. A locked-out account is unlocked at the same time
(`--no-unlock` leaves it), and the password takes the same route as everywhere
else here: generator or stdin, into powershell.exe through an environment
variable, to the DC inside the encrypted WinRM channel. Never argv.

**A delta sync does not carry the password.** `--sync` starts
`Start-ADSyncSyncCycle -PolicyType Delta`, which moves directory attributes;
password hash sync is a separate channel that runs every 2 minutes on its own.
That is why the command verifies by polling `lastPasswordChangeDateTime` in
Entra instead of trusting the sync cycle. Not seeing it inside `--wait` seconds
is reported as "not yet", not as a failure - the AD write already happened.

Setup, once per machine:

```
wsg user onprem-check                  # says which of these is missing

# the DC, into %LOCALAPPDATA%\wsg\onprem.json (or set WSG_AD_HOST)
'{"host": "192.0.2.4"}' | Set-Content $env:LOCALAPPDATA\wsg\onprem.json

# an AD account that may reset passwords, DPAPI-encrypted to you on this machine
Get-Credential | Export-Clixml $env:LOCALAPPDATA\wsgd-cred.xml

# only on a machine outside the domain: WinRM will not NTLM to an unknown host
# without this. Elevated shell.
Set-Item WSMan:\localhost\Client\TrustedHosts -Value 192.0.2.4 -Concatenate -Force
```
