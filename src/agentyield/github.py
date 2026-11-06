"""A small read-only GitHub REST client on the standard library.

It sends conditional requests (an authorised request answered `304 Not Modified` doesn't count against the rate
limit), follows `Link: rel="next"` pagination, and reports the rate limit left. It never writes to GitHub.
"""

from __future__ import annotations

import base64
import calendar
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

API_VERSION = "2022-11-28"
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


@dataclass
class Response:
    status: int
    data: Any
    etag: str
    next_url: str | None


def token_from(env_name: str = "GITHUB_TOKEN") -> str:
    """The token in `env_name`, or the GitHub CLI's login when that is unset (handy on a laptop)."""
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    gh = shutil.which("gh")
    if gh:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    raise SystemExit(f"No GitHub credentials: set {env_name}, or sign in with `gh auth login`.")


class AppToken:
    """Installation tokens for a GitHub App, renewed five minutes before they expire (they last an hour)."""

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str, api_url: str):
        from .appauth import load_private_key  # noqa: PLC0415

        self.app_id, self.installation_id, self.api_url = app_id, installation_id, api_url.rstrip("/")
        self.key = load_private_key(private_key_pem)
        self._token, self._expires = "", 0.0

    def __call__(self) -> str:
        if not self._token or time.time() > self._expires - 300:
            from .appauth import app_jwt  # noqa: PLC0415

            data = post_json(
                f"{self.api_url}/app/installations/{self.installation_id}/access_tokens",
                {},
                {"Authorization": f"Bearer {app_jwt(self.app_id, self.key)}"},
            )
            self._token = str(data["token"])
            expires = str(data.get("expires_at") or "")
            self._expires = (
                calendar.timegm(time.strptime(expires, "%Y-%m-%dT%H:%M:%SZ")) if expires else time.time() + 3600
            )
        return self._token


def post_json(
    url: str, body: dict[str, Any], headers: dict[str, str] | None = None, form: bool = False, timeout: float = 30
) -> Any:
    """The only POSTs AgentYield makes: trading an App JWT for an installation token, and an OAuth code for a
    user token. Neither changes anything on GitHub."""
    data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
    req.add_header("User-Agent", "agentyield")
    req.add_header("X-GitHub-Api-Version", API_VERSION)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            message = json.loads(err.read()).get("message", err.reason)
        except ValueError:
            message = str(err.reason)
        raise GitHubError(err.code, str(message)) from None


class GitHub:
    def __init__(self, token: str | Any, api_url: str = "https://api.github.com", timeout: float = 30):
        self._token = token  # a string, or a callable that returns a fresh token (GitHub App)
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None
        self.requests = 0

    @property
    def token(self) -> str:
        return self._token() if callable(self._token) else str(self._token)

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def get(self, path: str, params: dict[str, Any] | None = None, etag: str = "") -> Response:
        req = urllib.request.Request(self._url(path, params))
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", API_VERSION)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("User-Agent", "agentyield")
        if etag:
            req.add_header("If-None-Match", etag)
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._note_limits(resp.headers)
                body = resp.read()
                links = NEXT_LINK.search(resp.headers.get("Link", ""))
                return Response(
                    resp.status,
                    json.loads(body) if body else None,
                    resp.headers.get("ETag", ""),
                    links.group(1) if links else None,
                )
        except urllib.error.HTTPError as err:
            self._note_limits(err.headers)
            if err.code == 304:
                return Response(304, None, etag, None)
            try:
                message = json.loads(err.read()).get("message", err.reason)
            except ValueError:
                message = str(err.reason)
            if err.code in (403, 429) and self.rate_remaining == 0 and self.rate_reset:
                wait = max(0, self.rate_reset - int(time.time()))
                message += f" (rate limit reached; resets in {wait // 60} min)"
            raise GitHubError(err.code, str(message)) from None

    def _note_limits(self, headers: Any) -> None:
        if headers and headers.get("X-RateLimit-Remaining") is not None:
            self.rate_remaining = int(headers["X-RateLimit-Remaining"])
            self.rate_reset = int(headers.get("X-RateLimit-Reset", "0") or 0)

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
        url: str | None = self._url(path, params)
        while url:
            resp = self.get(url)
            yield from resp.data or []
            url = resp.next_url

    # ---------- the calls AgentYield makes ----------
    def repo(self, full_name: str) -> dict[str, Any]:
        return dict(self.get(f"/repos/{full_name}").data)

    def file_text(self, owner_repo: str, path: str, ref: str = "") -> str:
        resp = self.get(f"/repos/{owner_repo}/contents/{urllib.parse.quote(path)}", {"ref": ref} if ref else None)
        return base64.b64decode(resp.data["content"]).decode()

    def org_property_values(self, org: str) -> Iterator[dict[str, Any]]:
        return self.paginate(f"/orgs/{org}/properties/values", {"per_page": 100})

    def pulls_page(self, repo: str, url: str | None, etag: str = "") -> Response:
        params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 50}
        return self.get(url or f"/repos/{repo}/pulls", None if url else params, etag)

    def pull(self, repo: str, number: int) -> dict[str, Any]:
        return dict(self.get(f"/repos/{repo}/pulls/{number}").data)

    def review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Inline review comments on a PR's diff."""
        return list(self.paginate(f"/repos/{repo}/pulls/{number}/comments", {"per_page": 100}))

    def pull_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self.paginate(f"/repos/{repo}/pulls/{number}/files", {"per_page": 100}))

    def issue(self, repo: str, number: int) -> dict[str, Any]:
        return dict(self.get(f"/repos/{repo}/issues/{number}").data)

    def releases_page(self, repo: str, etag: str = "") -> Response:
        return self.get(f"/repos/{repo}/releases", {"per_page": 100}, etag)

    def commit_files(self, repo: str, sha: str) -> list[dict[str, Any]]:
        """Changed files of one commit, with additions and deletions. Only used for PRs that mix agent and
        human commits, to measure the agent's share in lines."""
        files: list[dict[str, Any]] = []
        url: str | None = f"/repos/{repo}/commits/{sha}"
        while url:
            resp = self.get(url)
            files.extend((resp.data or {}).get("files") or [])
            url = resp.next_url
        return files

    def timeline(self, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self.paginate(f"/repos/{repo}/issues/{number}/timeline", {"per_page": 100}))
