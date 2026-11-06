"""The dashboard: a live server (`agentyield serve`) and a static build (`agentyield build`), from one HTML page.

Who sees what:
  - Organisation and team views are totals, so everyone signed in may see them.
  - PR-level data for a repository (the PR list, its titles, the repository's own view) appears only to people
    GitHub says can read that repository, checked with their own GitHub token and re-checked every 15 minutes.
  - The admin view (sync state, attribution coverage, corrections) is for logins in `outputs.dashboard.admins`.
  - A static build can't check who is looking, so it carries totals only: no PR list and no titles.

Sign-in is "Sign in with GitHub" through a GitHub App. With `auth: none` the server only listens on 127.0.0.1,
for one person on their own machine; listening on another address without sign-in is refused.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Config, RepoSpec
from .derive import SUMMARY, attribution_path, corrections, metrics_path
from .github import GitHub, GitHubError, post_json
from .store import Store

STATIC = os.path.join(os.path.dirname(__file__), "static")
ACCESS_TTL = 15 * 60
SESSION_TTL = 12 * 3600
LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass
class Viewer:
    login: str
    admin: bool
    repos: set[str] | None  # lower-case names this person can read; None = every tracked repository
    token: str = ""
    checked: float = 0.0


@dataclass
class Dashboard:
    store: Store
    cfg: Config
    repos: dict[str, RepoSpec]
    auth_mode: str = "none"
    api_url: str = "https://api.github.com"
    web_url: str = "https://github.com"
    base_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    session_secret: bytes = b""
    admins: set[str] = field(default_factory=set)
    sessions: dict[str, Viewer] = field(default_factory=dict)

    @classmethod
    def from_config(cls, store: Store, cfg: Config, repos: dict[str, RepoSpec]) -> Dashboard:
        dash = dict(cfg.section("outputs").get("dashboard") or {})
        auth = dict(dash.get("auth") or {})
        mode = str(auth.get("mode") or "none")
        d = cls(
            store,
            cfg,
            repos,
            mode,
            cfg.api_url,
            str(cfg.section("github").get("web_url") or "https://github.com").rstrip("/"),
            str(dash.get("base_url") or "").rstrip("/"),
            admins={str(a).lower() for a in dash.get("admins") or []},
        )
        if mode == "github":
            names = {
                k: str(auth.get(k) or default)
                for k, default in (
                    ("client_id_env", "AGENTYIELD_CLIENT_ID"),
                    ("client_secret_env", "AGENTYIELD_CLIENT_SECRET"),
                    ("session_secret_env", "AGENTYIELD_SESSION_SECRET"),
                )
            }
            env = {k: os.environ.get(name, "") for k, name in names.items()}
            missing = [names[k] for k, v in env.items() if not v]
            if missing:
                raise SystemExit(f"GitHub sign-in needs these environment variables set: {', '.join(missing)}")
            if len(env["session_secret_env"]) < 32:
                raise SystemExit("the session secret must be at least 32 characters")
            d.client_id, d.client_secret = env["client_id_env"], env["client_secret_env"]
            d.session_secret = env["session_secret_env"].encode()
        return d

    # ---------------------------------------------------------------- sessions
    def sign(self, payload: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        mac = hmac.new(self.session_secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{mac}"

    def unsign(self, value: str) -> dict[str, Any] | None:
        body, _, mac = value.partition(".")
        good = hmac.new(self.session_secret, body.encode(), hashlib.sha256).hexdigest()
        if not mac or not hmac.compare_digest(mac, good):
            return None
        try:
            data = dict(json.loads(base64.urlsafe_b64decode(body)))
        except ValueError:
            return None
        return data if data.get("exp", 0) > time.time() else None

    def viewer(self, cookie_header: str) -> Viewer | None:
        if self.auth_mode == "none":
            return Viewer("local", True, None)
        jar = SimpleCookie(cookie_header or "")
        session = self.unsign(jar["ay_session"].value) if "ay_session" in jar else None
        if not session:
            return None
        v = self.sessions.get(session["sid"])
        if v and time.time() - v.checked > ACCESS_TTL:
            v.repos, v.checked = self.readable(v.token), time.time()
        return v

    def readable(self, token: str) -> set[str]:
        """Tracked repositories this person can read, asked of GitHub with their own token."""
        gh = GitHub(token, self.api_url)
        out = set()
        for key, spec in self.repos.items():
            try:
                gh.repo(spec.name)
                out.add(key)
            except GitHubError:
                pass
        return out

    def login_url(self, state: str) -> str:
        q = {"client_id": self.client_id, "state": state, "redirect_uri": f"{self.base_url}/auth/callback"}
        return f"{self.web_url}/login/oauth/authorize?{urllib.parse.urlencode(q)}"

    def finish_login(self, code: str) -> str:
        data = post_json(
            f"{self.web_url}/login/oauth/access_token",
            {"client_id": self.client_id, "client_secret": self.client_secret, "code": code},
            form=True,
        )
        token = str(data.get("access_token") or "")
        if not token:
            raise GitHubError(401, str(data.get("error_description") or "sign-in was not completed"))
        login = str(GitHub(token, self.api_url).get("/user").data.get("login", ""))
        sid = secrets.token_urlsafe(24)
        self.sessions[sid] = Viewer(login, login.lower() in self.admins, self.readable(token), token, time.time())
        return self.sign({"sid": sid, "exp": time.time() + SESSION_TTL})

    # ---------------------------------------------------------------- data
    def allowed(self, v: Viewer, repo: str) -> bool:
        return v.repos is None or repo.lower() in v.repos

    def summary(self, v: Viewer, scope: str) -> dict[str, Any]:
        data = self.store.read(SUMMARY) or {}
        scopes = data.get("scopes") or {}
        names = [s for s in scopes if not s.startswith("repo:") or self.allowed(v, s[5:])]
        if scope not in names:
            scope = "org"
        return {
            "built_at": data.get("built_at"),
            "min_sample": data.get("min_sample"),
            "scopes": names,
            "scope": scope,
            "data": scopes.get(scope),
            "viewer": {"login": v.login, "admin": v.admin},
            "mode": "live",
        }

    def prs(self, v: Viewer, scope: str, page: int, per_page: int) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for key, spec in sorted(self.repos.items()):
            if not self.allowed(v, key):
                continue
            if scope.startswith("repo:") and scope[5:].lower() != key:
                continue
            for row in (self.store.read(metrics_path(spec.name), {}) or {}).get("prs") or []:
                if scope.startswith("team:") and row.get("team") != scope[5:]:
                    continue
                fact = self.store.read(self.store.pr_path(spec.name, row["number"]), {}) or {}
                rows.append(
                    {
                        **row,
                        "title": fact.get("title", ""),
                        "restricted": fact.get("restricted", False),
                        "created_at": fact.get("created_at"),
                        "merged_at": fact.get("merged_at"),
                    }
                )
        rows.sort(key=lambda r: r.get("merged_at") or r.get("created_at") or "", reverse=True)
        start = (page - 1) * per_page
        return {"total": len(rows), "page": page, "per_page": per_page, "rows": rows[start : start + per_page]}

    def admin(self) -> dict[str, Any]:
        manifest = self.store.manifest()
        repos = []
        for spec in sorted(self.repos.values(), key=lambda s: s.name.lower()):
            cursor = self.store.read(f"{self.store.repo_dir(spec.name)}/cursor.json", {}) or {}
            attr = (self.store.read(attribution_path(spec.name), {}) or {}).get("summary") or {}
            repos.append(
                {
                    "repo": spec.name,
                    "team": spec.team,
                    "restricted": spec.restricted,
                    "synced_at": cursor.get("synced_at"),
                    "history_from": cursor.get("backfilled_to"),
                    "prs": attr.get("prs", 0),
                    "coverage": attr.get("coverage"),
                    "labels": attr.get("labels"),
                    "low_confidence": attr.get("low_confidence", 0),
                }
            )
        ev = self.store.read("derived/classifier_eval.json") or {}
        classifier = {
            "name": str(self.cfg.section("review").get("classifier") or "rules"),
            "accuracy": ev.get("accuracy"),
            "sample": ev.get("sample"),
            "sample_source": ev.get("sample_source"),
            "measured_at": ev.get("measured_at"),
            "measured_with": ev.get("classifier"),
        }
        return {
            "classifier": classifier,
            "last_sync": manifest.get("last_sync"),
            "config": (manifest.get("applied") or {}).get("hash"),
            "hidden": sorted(manifest.get("hidden", {})),
            "corrections": len(corrections(self.store)),
            "repos": repos,
        }


def make_handler(d: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            gz = "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 2048
            if gz:
                body = gzip.compress(body, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
            )
            if gz:
                self.send_header("Content-Encoding", "gzip")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _redirect(self, to: str, cookies: list[str] | None = None) -> None:
            self.send_response(302)
            self.send_header("Location", to)
            for c in cookies or []:
                self.send_header("Set-Cookie", c)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _cookie(self, name: str, value: str, max_age: int) -> str:
            secure = "; Secure" if d.base_url.startswith("https://") else ""
            return f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

        def do_GET(self) -> None:
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            path = url.path
            if path == "/auth/login" and d.auth_mode == "github":
                state = secrets.token_urlsafe(16)
                return self._redirect(d.login_url(state), [self._cookie("ay_state", state, 600)])
            if path == "/auth/callback" and d.auth_mode == "github":
                jar = SimpleCookie(self.headers.get("Cookie", ""))
                expected = jar["ay_state"].value if "ay_state" in jar else ""
                if not expected or not hmac.compare_digest(expected, q.get("state", "")):
                    return self._send(400, b"Sign-in expired or was tampered with; start again.", "text/plain")
                try:
                    session = d.finish_login(q.get("code", ""))
                except GitHubError as exc:
                    return self._send(401, str(exc).encode(), "text/plain")
                return self._redirect(
                    "/", [self._cookie("ay_session", session, SESSION_TTL), self._cookie("ay_state", "", 0)]
                )
            if path == "/auth/logout":
                return self._redirect("/", [self._cookie("ay_session", "", 0)])
            viewer = d.viewer(self.headers.get("Cookie", ""))
            if viewer is None:
                if path.startswith("/api/"):
                    return self._json({"error": "sign in"}, 401)
                return self._redirect("/auth/login")
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "dashboard.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if path == "/api/summary":
                return self._json(d.summary(viewer, q.get("scope", "org")))
            if path == "/api/prs":
                page = max(1, int(q.get("page", "1") or 1))
                per = min(100, max(1, int(q.get("per_page", "25") or 25)))
                return self._json(d.prs(viewer, q.get("scope", "org"), page, per))
            if path == "/api/admin":
                return self._json(d.admin()) if viewer.admin else self._json({"error": "admins only"}, 403)
            if path == "/api/health":
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

    return Handler


def serve(
    d: Dashboard, host: str, port: int, sync: Any = None, every: int = 900, allow_unauthenticated: bool = False
) -> None:
    if d.auth_mode == "none" and host not in LOOPBACK and not allow_unauthenticated:
        raise SystemExit(
            f"Refusing to listen on {host} without sign-in: anyone on the network could see the data. "
            "Set outputs.dashboard.auth.mode: github, or listen on 127.0.0.1."
        )
    if sync is not None and every > 0:

        def loop() -> None:
            while True:
                time.sleep(every)
                try:
                    sync()
                except (Exception, SystemExit) as exc:  # one failed sync must not stop the dashboard
                    print(f"sync failed: {exc}", flush=True)

        threading.Thread(target=loop, daemon=True, name="agentyield-sync").start()
    httpd = ThreadingHTTPServer((host, port), make_handler(d))
    print(f"Dashboard: http://{host}:{port}  (sign-in: {d.auth_mode}; Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def build_site(store: Store, out_dir: str) -> list[str]:
    """The static dashboard: the page plus totals for every scope. No PR list, no titles: a static file can't
    check who is reading it."""
    data = store.read(SUMMARY) or {}
    shared = {
        "built_at": data.get("built_at"),
        "min_sample": data.get("min_sample"),
        "scopes": data.get("scopes") or {},
        "mode": "static",
    }
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    with open(os.path.join(STATIC, "dashboard.html"), "rb") as fh:
        page = fh.read()
    written = []
    for rel, body in (("index.html", page), ("data/summary.json", json.dumps(shared).encode())):
        path = os.path.join(out_dir, rel)
        with open(path, "wb") as fh:
            fh.write(body)
        written.append(path)
    return written
