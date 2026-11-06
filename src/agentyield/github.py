"""A small read-only GitHub REST client on the standard library.

It sends conditional requests (an authorised request answered `304 Not Modified` doesn't count against the rate
limit), follows `Link: rel="next"` pagination, and reports the rate limit left. It never writes to GitHub.
"""

from __future__ import annotations

import base64
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


class GitHub:
    def __init__(self, token: str, api_url: str = "https://api.github.com", timeout: float = 30):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None
        self.requests = 0

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

    def timeline(self, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self.paginate(f"/repos/{repo}/issues/{number}/timeline", {"per_page": 100}))
