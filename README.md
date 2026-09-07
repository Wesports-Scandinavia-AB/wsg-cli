# wsg

Sign in to Microsoft Graph once, reuse the token from any script. No app
registration, no secrets: you sign in as yourself and the scripts get what you
are allowed to do.

## Install

```
pipx install git+https://github.com/tomeo/wsg-cli
```

or, for development:

```
git clone https://github.com/tomeo/wsg-cli
pip install -e wsg-cli
```

Requires Python 3.9+. No third party dependencies.

## Use

```
wsg login            prints a URL, open it in your browser, sign in
wsg status           who is signed in, token expiry, granted scopes
wsg token            prints an access token, refreshed silently
wsg whoami           calls Graph /me
wsg logout           deletes the cache
```

`wsg login` does not open a browser by itself. Pass `--open` if you want that.
On a machine with no browser (a server), use `wsg login --device`.

The refresh token lives 90 days and is renewed each time it is used, so after
one login the scripts stay silent until you change password or go 90 days
without running anything.

### From PowerShell

```powershell
$H = @{ Authorization = (wsg token --header) }
if ($LASTEXITCODE) { throw "Not signed in, run: wsg login" }
Invoke-RestMethod -Headers $H -Uri https://graph.microsoft.com/v1.0/me
```

### From Python

```python
from wsg import get_token
from wsg.auth import graph

token = get_token(["Files.ReadWrite"])   # raises AuthError if login is needed
me = graph("GET", "/me")
```

## Scopes

`wsg login` asks for the full list in `src/wsg/scopes.py`. Some of them need an
admin role in the tenant. If you do not have that, sign in with a narrower set:

```
wsg login --scopes User.Read,Files.ReadWrite,Mail.Send
```

`wsg token --scopes X` fails with exit code 2 if X was not granted, so scripts
can tell the user exactly what to do.

## How it works

Authorization code flow with PKCE against the Microsoft Graph Command Line
Tools public client (the one Connect-MgGraph uses). The CLI listens on a random
localhost port, you sign in in the browser, the browser redirects back to
localhost with the code. That is a normal interactive sign-in, so Conditional
Access sees your device and your MFA. Device code flow is often blocked by
policy, which is why it is not the default.

Cache: `%LOCALAPPDATA%\wsg\token.json` on Windows, `~/.config/wsg/token.json`
elsewhere. Override with `WSG_TOKEN_CACHE`. Tenant defaults to `organizations`,
override with `WSG_TENANT`.
