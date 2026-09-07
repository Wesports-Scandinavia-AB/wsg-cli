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
wsg arena incidents  open Site Watch incidents and warnings
wsg arena logout     deletes the Arena session
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

`wsg arena incidents` lists Site Watch incidents (severity `acute`) and
warnings (`watch`). Open ones by default; `--status resolved --days 7` for
last week's, `--severity acute` for incidents only, `--id X` for one incident
with its timeline, `--json` for scripts. From Python: `arena.incidents()`.

Send a real `User-Agent`: the zone's bot rules answer 403 to `Python-urllib`.
Override the app with `WSG_ARENA_URL`, the binary with `WSG_CLOUDFLARED`.
