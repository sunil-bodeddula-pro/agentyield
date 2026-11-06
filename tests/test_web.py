"""The dashboard server, sign-in, access rules, the static build, and GitHub App authentication."""

from __future__ import annotations

import base64
import http.client
import json
import os
import secrets
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from typing import Any

from helpers import CONFIG, OrgCase  # first: puts src on the path

# isort: split
from agentyield import appauth, web
from agentyield.cli import _applied_config
from agentyield.config import load
from agentyield.github import AppToken, GitHub
from agentyield.ops import applied_repos
from agentyield.store import Store


# ---------- a throwaway RSA key, generated here so no key material lives in the repository ----------
def _probable_prime(bits: int) -> int:
    while True:
        n = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        d, r = n - 1, 0
        while d % 2 == 0:
            d, r = d // 2, r + 1
        for _ in range(20):
            x = pow(secrets.randbelow(n - 3) + 2, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                break
        else:
            return n


def _der_int(v: int) -> bytes:
    raw = v.to_bytes((v.bit_length() + 8) // 8 or 1, "big")
    return b"\x02" + _der_len(len(raw)) + raw


def _der_len(n: int) -> bytes:
    if n < 128:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der_seq(*parts: bytes) -> bytes:
    body = b"".join(parts)
    return b"\x30" + _der_len(len(body)) + body


def test_key(bits: int = 1024) -> tuple[str, str, int, int]:
    """(PKCS#1 PEM, PKCS#8 PEM, n, e)."""
    e = 65537
    while True:
        p, q = _probable_prime(bits // 2), _probable_prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            break
    n, d = p * q, pow(e, -1, phi)
    pkcs1 = _der_seq(*[_der_int(v) for v in (0, n, e, d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p))])
    algo = _der_seq(b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01", b"\x05\x00")
    pkcs8 = _der_seq(_der_int(0), algo, b"\x04" + _der_len(len(pkcs1)) + pkcs1)

    def pem(kind: str, der: bytes) -> str:
        b64 = base64.b64encode(der).decode()
        lines = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
        return f"-----BEGIN {kind}-----\n{lines}\n-----END {kind}-----\n"

    return pem("RSA " + "PRIVATE KEY", pkcs1), pem("PRIVATE " + "KEY", pkcs8), n, e


class AppAuthTest(OrgCase):
    def test_rs256_and_installation_token(self) -> None:
        pkcs1, pkcs8, n, e = test_key()
        for pem in (pkcs1, pkcs8):
            key = appauth.load_private_key(pem)
            token = appauth.app_jwt("12345", key, now=1_700_000_000)
            header, claims, sig = token.split(".")
            pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
            self.assertEqual(
                json.loads(base64.urlsafe_b64decode(pad(claims))),
                {"iat": 1_699_999_940, "exp": 1_700_000_540, "iss": "12345"},
            )
            self.assertTrue(
                appauth.verify_rs256(n, e, f"{header}.{claims}".encode(), base64.urlsafe_b64decode(pad(sig)))
            )
            self.assertFalse(appauth.verify_rs256(n, e, b"tampered", base64.urlsafe_b64decode(pad(sig))))
        gh = GitHub(AppToken("12345", pkcs1, "99", self.gh.url), self.gh.url)
        self.assertEqual(gh.repo("acme/checkout-service")["full_name"], "acme/checkout-service")
        self.assertEqual(len(self.gh.app_jwts), 1)  # the token is reused until it nears expiry
        gh.repo("acme/web-dashboard")
        self.assertEqual(len(self.gh.app_jwts), 1)

    def test_sync_as_a_github_app(self) -> None:
        pkcs1, _, _, _ = test_key()
        os.environ.pop("GITHUB_TOKEN")
        key_path = os.path.join(self.dir, "app.pem")
        with open(key_path, "w") as fh:
            fh.write(pkcs1)
        os.environ.update(AY_APP_ID="12345", AY_APP_KEY=key_path, AY_APP_INST="99")
        try:
            self.write_config(
                CONFIG.replace(
                    "github:\n  api_url: {api}\n",
                    "github:\n  api_url: {api}\n  auth:\n"
                    "    app_id_env: AY_APP_ID\n    private_key_env: AY_APP_KEY\n"
                    "    installation_id_env: AY_APP_INST\n",
                )
            )
            code, out = self.run_cli("config", "apply", "--yes")
            self.assertEqual(code, 0, out)
            self.assertTrue(self.gh.app_jwts)
        finally:
            for k in ("AY_APP_ID", "AY_APP_KEY", "AY_APP_INST"):
                os.environ.pop(k, None)


class Client:
    def __init__(self, port: int):
        self.port, self.cookies = port, {}

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())} if self.cookies else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        for raw in resp.headers.get_all("Set-Cookie") or []:
            name, _, rest = raw.partition("=")
            value = rest.split(";")[0]
            if "Max-Age=0" in raw:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return resp.status, dict(resp.headers), body

    def json(self, path: str) -> Any:
        status, _, body = self.get(path)
        return status, json.loads(body)


class WebCase(OrgCase):
    def start(self, extra_config: str = "") -> Client:
        self.write_config(CONFIG + extra_config)
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        current = load(self.config)
        store = Store(current.store_uri)
        cfg = _applied_config(store, current)
        self.dash = web.Dashboard.from_config(store, cfg, applied_repos(store.manifest()))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.dash))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return Client(httpd.server_address[1])


class LocalDashboardTest(WebCase):
    def test_pages_and_api_without_sign_in(self) -> None:
        c = self.start()
        status, headers, body = c.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>AgentYield</title>", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        status, s = c.json("/api/summary?scope=team:payments")
        self.assertEqual((status, s["scope"], s["viewer"]["admin"]), (200, "team:payments", True))
        self.assertIn("repo:acme/security-patches", s["scopes"])
        status, prs = c.json("/api/prs?per_page=3&page=1")
        self.assertEqual((prs["total"], len(prs["rows"])), (7, 3))
        _, all_prs = c.json("/api/prs?per_page=100")
        by = {(r["repo"], r["number"]): r for r in all_prs["rows"]}
        self.assertEqual(by[("acme/security-patches", 1)]["title"], "")
        self.assertTrue(by[("acme/security-patches", 1)]["restricted"])
        self.assertEqual(by[("acme/web-dashboard", 1)]["title"], "feat: dark mode")
        self.assert_no_planted(json.dumps(all_prs) + json.dumps(s))
        status, admin = c.json("/api/admin")
        self.assertEqual((status, len(admin["repos"])), (200, 3))

    def test_refuses_the_network_without_sign_in(self) -> None:
        self.start()
        with self.assertRaises(SystemExit) as ctx:
            web.serve(self.dash, "0.0.0.0", 0)
        self.assertIn("without sign-in", str(ctx.exception))

    def test_static_build_has_totals_only(self) -> None:
        self.start()
        out = os.path.join(self.dir, "site")
        code, msg = self.run_cli("build", "--out", out)
        self.assertEqual(code, 0, msg)
        with open(os.path.join(out, "data", "summary.json")) as fh:
            raw = fh.read()
        data = json.loads(raw)
        self.assertEqual(data["mode"], "static")
        self.assertIn("org", data["scopes"])
        self.assertNotIn('"title"', raw)
        self.assertNotIn("dark mode", raw)  # an ordinary PR title: not in a static copy either
        self.assert_no_planted(raw)
        self.assertTrue(os.path.exists(os.path.join(out, "index.html")))


SIGN_IN = """outputs:
  dashboard:
    base_url: http://127.0.0.1
    admins: [bob]
    auth:
      mode: github
"""


class SignInTest(WebCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ.update(
            AGENTYIELD_CLIENT_ID="client-id",
            AGENTYIELD_CLIENT_SECRET="client-secret",
            AGENTYIELD_SESSION_SECRET="s" * 40,
        )
        self.addCleanup(
            lambda: [
                os.environ.pop(k, None)
                for k in ("AGENTYIELD_CLIENT_ID", "AGENTYIELD_CLIENT_SECRET", "AGENTYIELD_SESSION_SECRET")
            ]
        )
        self.gh.users = {
            "u-alice": {"login": "alice", "repos": {"acme/checkout-service"}},
            "u-bob": {
                "login": "bob",
                "repos": {"acme/checkout-service", "acme/web-dashboard", "acme/security-patches"},
            },
        }

    def sign_in(self, c: Client, code: str, token: str) -> None:
        self.gh.codes[code] = token
        status, headers, _ = c.get("/auth/login")
        self.assertEqual(status, 302)
        loc = urllib.parse.urlparse(headers["Location"])
        q = dict(urllib.parse.parse_qsl(loc.query))
        self.assertEqual((loc.path, q["client_id"]), ("/login/oauth/authorize", "client-id"))
        status, headers, _ = c.get(f"/auth/callback?code={code}&state={q['state']}")
        self.assertEqual((status, headers["Location"]), (302, "/"))
        self.assertIn("ay_session", c.cookies)

    def test_people_see_only_repositories_they_can_read(self) -> None:
        c = self.start(SIGN_IN.replace("127.0.0.1", self.gh.url.split("//")[1]))
        self.dash.web_url = self.gh.url
        self.assertEqual(c.get("/")[0], 302)
        self.assertEqual(c.json("/api/summary")[0], 401)
        self.sign_in(c, "code-a", "u-alice")
        _, s = c.json("/api/summary")
        self.assertEqual(s["viewer"], {"login": "alice", "admin": False})
        self.assertIn("org", s["scopes"])
        self.assertEqual([x for x in s["scopes"] if x.startswith("repo:")], ["repo:acme/checkout-service"])
        _, prs = c.json("/api/prs?per_page=100")
        self.assertEqual({r["repo"] for r in prs["rows"]}, {"acme/checkout-service"})
        _, s2 = c.json("/api/summary?scope=repo:acme/web-dashboard")  # not readable: falls back to org
        self.assertEqual(s2["scope"], "org")
        self.assertEqual(c.json("/api/admin")[0], 403)
        bob = Client(c.port)
        self.sign_in(bob, "code-b", "u-bob")
        self.assertEqual(bob.json("/api/admin")[0], 200)

    def test_tampering_is_refused(self) -> None:
        c = self.start(SIGN_IN)
        self.dash.web_url = self.gh.url
        status, _, _ = c.get("/auth/callback?code=x&state=forged")
        self.assertEqual(status, 400)
        c.cookies["ay_session"] = self.dash.sign({"sid": "made-up", "exp": 9e9})
        self.assertEqual(c.json("/api/summary")[0], 401)
        body, _, mac = self.dash.sign({"sid": "x", "exp": 9e9}).partition(".")
        c.cookies["ay_session"] = f"{body}.{'0' * len(mac)}"
        self.assertEqual(c.json("/api/summary")[0], 401)

    def test_sign_in_needs_its_secrets(self) -> None:
        os.environ.pop("AGENTYIELD_SESSION_SECRET")
        with self.assertRaises(SystemExit) as ctx:
            self.start(SIGN_IN)
        self.assertIn("AGENTYIELD_SESSION_SECRET", str(ctx.exception))


class NoKeyInRepoTest(unittest.TestCase):
    def test_tests_generate_their_keys(self) -> None:
        here = os.path.dirname(__file__)
        for name in os.listdir(here):
            if name.endswith(".py"):
                with open(os.path.join(here, name), encoding="utf-8") as fh:
                    self.assertNotIn("BEGIN RSA PRIVATE KEY-----\nMII", fh.read())
