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

wsg intune apps        managed apps with their assignments
wsg intune app-status  per-user install status for one app
wsg intune intent      what the service intends for a user on a device
wsg intune store-app   add a Microsoft Store app (new) by Store id
wsg intune win32       publish an .intunewin package
wsg intune sync        ask a device to check in now
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
