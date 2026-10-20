"""
One-time secret links on secrets.wesports.io: hand someone a password, an API key
or a credentials file as a link that burns after one read.

The service is zero-knowledge, and that only holds if the encryption happens here:

1. **The key never goes to the server.** A random 256-bit key encrypts the value
   with AES-GCM; the server gets the ciphertext and the key goes in the link's
   fragment (`…/s/<id>#<key>`), which browsers never send. The envelope format is
   the service's own `public/assets/crypto.js`, byte for byte, so the reveal page
   in the browser opens what this creates.
2. **A passphrase is woven in, not checked.** With one, the AES key is PBKDF2 over
   (URL key || passphrase), so the link alone opens nothing. The server cannot tell
   a wrong passphrase from a right one; a wrong one still burns the secret.
3. **The title is readable without opening.** It is encrypted with the URL key only
   — never the passphrase — so anyone holding the link can read it. Never put the
   secret in it.

AES-GCM is implemented below in plain Python, because the package has no
third-party dependencies and the standard library has no AES. Only encryption is
needed (GCM decrypts by encrypting the counter stream), and a secret is at most a
few kilobytes, so speed does not matter. Checked on 2026-10-07 against the
`cryptography` package and by opening a created link through the service's own
reveal endpoint. Decision, stated plainly: a home-grown cipher is the shortcut
here; if the package ever takes a dependency, swap this for `cryptography`'s
AESGCM and keep the envelope format.
"""

import base64
import hashlib
import json
import os
import re
import secrets as _secrets
from urllib import request
from urllib.error import HTTPError, URLError

BASE = os.environ.get("WSG_SECRETS_URL", "https://secrets.wesports.io").rstrip("/")
# Speglar tjänstens egen tolkning: ett tal och en enhet, från en minut till ett
# år. Kontrollen här finns bara för att svara direkt i stället för att gå ut på
# nätet med något som ändå avvisas; servern validerar om.
TTL_RE = re.compile(r"^\d{1,6}[mhdw]$")
TTL_EXEMPEL = "30m, 12h, 21d, 4w"
MAX_VIEWS = 10
PBKDF2_ITERS = 310000   # same as crypto.js; the reveal page reads it from the envelope


class OnetimeError(Exception):
    pass


# ── AES-256 (encrypt only) ────────────────────────────────────────────────────

def _sbox():
    # Built rather than pasted: a 256-entry table typed by hand is where a typo
    # hides, and this is the textbook construction (inverse in GF(2^8) + affine).
    box, p, q = [0] * 256, 1, 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1 | q >> 7) & 0xFF) ^ ((q << 2 | q >> 6) & 0xFF) \
              ^ ((q << 3 | q >> 5) & 0xFF) ^ ((q << 4 | q >> 4) & 0xFF)
        box[p] = x ^ 0x63
        if p == 1:
            break
    box[0] = 0x63
    return box


SBOX = _sbox()


def _xtime(b):
    return ((b << 1) ^ 0x1B) & 0xFF if b & 0x80 else b << 1


def _expand_key(key):
    assert len(key) == 32
    w = [list(key[i:i + 4]) for i in range(0, 32, 4)]
    rcon = 1
    for i in range(8, 60):
        t = list(w[i - 1])
        if i % 8 == 0:
            t = [SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % 8 == 4:
            t = [SBOX[b] for b in t]
        w.append([a ^ b for a, b in zip(w[i - 8], t)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(15)]


def _encrypt_block(rk, block):
    s = [a ^ b for a, b in zip(block, rk[0])]
    for r in range(1, 15):
        s = [SBOX[b] for b in s]
        # ShiftRows on a column-major state: row i moves left by i.
        s = [s[(c * 4 + i * 5) % 16] for c in range(4) for i in range(4)]
        if r != 14:
            m = []
            for c in range(4):
                a = s[c * 4:c * 4 + 4]
                t = a[0] ^ a[1] ^ a[2] ^ a[3]
                m += [a[i] ^ t ^ _xtime(a[i] ^ a[(i + 1) % 4]) for i in range(4)]
            s = m
        s = [a ^ b for a, b in zip(s, rk[r])]
    return bytes(s)


# ── GCM ───────────────────────────────────────────────────────────────────────

def _gmul(x, y):
    r, R = 0, 0xE1 << 120
    for i in range(127, -1, -1):
        if (y >> i) & 1:
            r ^= x
        x = (x >> 1) ^ R if x & 1 else x >> 1
    return r


def _ghash(h, data):
    y = 0
    for i in range(0, len(data), 16):
        y = _gmul(y ^ int.from_bytes(data[i:i + 16], "big"), h)
    return y


def _pad16(b):
    return b + b"\0" * (-len(b) % 16)


def aes_gcm_encrypt(key, iv, plaintext):
    """AES-256-GCM, 96-bit IV, 128-bit tag, no AAD — what WebCrypto does with
    {name: 'AES-GCM', iv} and nothing else. Returns ciphertext || tag."""
    assert len(iv) == 12
    rk = _expand_key(key)
    h = int.from_bytes(_encrypt_block(rk, bytes(16)), "big")
    out = bytearray()
    for n, i in enumerate(range(0, len(plaintext), 16)):
        ks = _encrypt_block(rk, iv + (n + 2).to_bytes(4, "big"))
        out += bytes(a ^ b for a, b in zip(plaintext[i:i + 16], ks))
    lens = (0).to_bytes(8, "big") + (len(out) * 8).to_bytes(8, "big")
    s = _ghash(h, _pad16(bytes(out)) + lens)
    j0 = int.from_bytes(_encrypt_block(rk, iv + (1).to_bytes(4, "big")), "big")
    return bytes(out) + (s ^ j0).to_bytes(16, "big")


# ── The service ───────────────────────────────────────────────────────────────

def _b64(b):
    return base64.b64encode(b).decode()


def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _aes_key(raw, passphrase, salt):
    if not passphrase:
        return raw
    return hashlib.pbkdf2_hmac("sha256", raw + passphrase.encode(), salt, PBKDF2_ITERS, 32)


def create(value, *, ttl="1d", views=1, title=None, passphrase=None):
    """Encrypt `value` here and store the envelope. Returns the service's answer
    plus `link`, the only place the key exists."""
    ttl = ttl.strip().lower()
    if not TTL_RE.match(ttl):
        raise OnetimeError(f"--ttl är ett tal och en enhet: m, h, d eller w. Till exempel {TTL_EXEMPEL}")
    if not 1 <= views <= MAX_VIEWS:
        raise OnetimeError(f"--views måste vara 1–{MAX_VIEWS}")
    raw = _secrets.token_bytes(32)
    salt, iv = _secrets.token_bytes(16), _secrets.token_bytes(12)
    ct = aes_gcm_encrypt(_aes_key(raw, passphrase, salt), iv, value.encode("utf-8"))
    envelope = {"v": 1, "kdf": "pbkdf2" if passphrase else "raw"}
    if passphrase:
        envelope["i"] = PBKDF2_ITERS
    envelope.update({"s": _b64(salt), "n": _b64(iv), "c": _b64(ct)})
    body = {"envelope": json.dumps(envelope, separators=(",", ":")),
            "hasPassphrase": bool(passphrase), "ttl": ttl, "views": views}
    if title:
        tiv = _secrets.token_bytes(12)
        body["title"] = json.dumps({"v": 1, "n": _b64(tiv),
                                    "c": _b64(aes_gcm_encrypt(raw, tiv, title.encode("utf-8")))},
                                   separators=(",", ":"))
    data = _call("POST", "/api/create", body)
    data["link"] = f"{BASE}{data['revealPath']}#{_b64url(raw)}"
    return data


def delete(secret_id, burn_token):
    """Destroy a secret before anyone reads it, with the burn token `create` gave."""
    _call("DELETE", f"/api/s/{secret_id}", {"burnToken": burn_token})


def _call(method, path, body):
    req = request.Request(BASE + path, data=json.dumps(body).encode(), method=method,
                          headers={"content-type": "application/json",
                                   "user-agent": "wsg-cli"})
    try:
        with request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or e.reason
        except ValueError:
            msg = e.reason
        raise OnetimeError(f"{method} {path}: HTTP {e.code}: {msg}") from None
    except URLError as e:
        raise OnetimeError(f"{BASE} svarar inte: {e.reason}") from None
