"""Mock GitHub for tests: a fixture builder for an organisation's pull requests, and a fake REST server that
answers from it. No test touches the network or real repositories.

The fixtures plant sensitive values (PLANTED) in every text field GitHub returns: titles, branch names, labels,
PR bodies, commit messages, author emails, review text, and a restricted repository. Tests check that none of
them ever reaches the store or the command output.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "test-token-not-a-secret"

PLANTED = {
    "github_token": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "aws_key": "AKIA" + "IOSFODNN7EXAMPLE",
    "email": "jane.doe@acme-corp.com",
    "phone": "+44 20 7946 0958",
    "deny_term": "Project Bluebird",
    "host": "db1.acme.internal",
    "home_path": "/home/jdoe",
    "body": "BODY-TEXT-MUST-NOT-BE-STORED",
    "review": "REVIEW-TEXT-MUST-NOT-BE-STORED",
    "commit": "COMMIT-MESSAGE-MUST-NOT-BE-STORED",
    "restricted_title": "Patch auth bypass CVE-2026-1234 before disclosure",
    "security_label_title": "Rotate leaked signing key for payments",
}


def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Org:
    """An organisation's repositories, pull requests and timelines, built deterministically around `now`."""

    def __init__(self, now: datetime | None = None):
        self.now = now or datetime.now(UTC).replace(microsecond=0)
        self.pulls: dict[str, dict[int, dict[str, Any]]] = {}
        self.timelines: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.properties: dict[str, dict[str, Any]] = {}
        self.files: dict[tuple[str, str], str] = {}
        for repo in ("acme/checkout-service", "acme/web-dashboard", "acme/security-patches", "acme/untracked"):
            self.pulls[repo] = {}
        self._build()

    # ---------- building blocks ----------
    def add_pr(
        self,
        repo: str,
        *,
        title: str,
        author: str = "alice",
        bot: bool = False,
        branch: str = "feature/x",
        labels: tuple[str, ...] = (),
        body: str = "",
        created: datetime,
        merged: datetime | None = None,
        closed: datetime | None = None,
        events: list[dict[str, Any]] | None = None,
        additions: int = 40,
        deletions: int = 10,
    ) -> int:
        n = len(self.pulls[repo]) + 1
        events = events or []
        last = max([created, merged or created, closed or created] + [self._when(e) for e in events])
        self.pulls[repo][n] = {
            "number": n,
            "title": title,
            "state": "closed" if (merged or closed) else "open",
            "draft": False,
            "user": {"login": author, "type": "Bot" if bot else "User"},
            "head": {"ref": branch},
            "base": {"ref": "main"},
            "labels": [{"name": lb} for lb in labels],
            "body": body,
            "created_at": ts(created),
            "updated_at": ts(last),
            "merged_at": ts(merged) if merged else None,
            "closed_at": ts(merged or closed) if (merged or closed) else None,
            "merged_by": {"login": "lead"} if merged else None,
            "additions": additions,
            "deletions": deletions,
            "changed_files": 3,
        }
        self.timelines[(repo, n)] = events
        return n

    @staticmethod
    def _when(e: dict[str, Any]) -> datetime:
        raw = e.get("submitted_at") or e.get("created_at") or (e.get("committer") or {}).get("date")
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))

    @staticmethod
    def commit(at: datetime, message: str, sha: str) -> dict[str, Any]:
        return {
            "event": "committed",
            "sha": sha,
            "message": message,
            "author": {"name": "Jane Doe", "email": PLANTED["email"], "date": ts(at)},
            "committer": {"name": "Jane Doe", "email": PLANTED["email"], "date": ts(at)},
        }

    @staticmethod
    def review(at: datetime, state: str, login: str = "bob", bot: bool = False) -> dict[str, Any]:
        return {
            "event": "reviewed",
            "state": state,
            "submitted_at": ts(at),
            "body": PLANTED["review"],
            "user": {"login": login, "type": "Bot" if bot else "User"},
        }

    def touch(self, repo: str, n: int, minutes: int = 1) -> None:
        """Make an existing PR look updated (a new comment, a label), as GitHub would."""
        self.pulls[repo][n]["updated_at"] = ts(self.now + timedelta(minutes=minutes))

    # ---------- the scenario ----------
    def _build(self) -> None:
        d = self.now - timedelta(days=10)
        h = timedelta(hours=1)
        cs = "acme/checkout-service"
        # PR 1: the back-and-forth example. 3 commits, 1 after the first review, 1 round trip, 1 force-push.
        self.add_pr(
            cs,
            title=f"feat: idempotency keys, see {PLANTED['email']}",
            author="alice",
            branch="claude/idem",
            body=f"Generated with Claude Code\n{PLANTED['body']} {PLANTED['github_token']}",
            created=d,
            merged=d + 6 * h,
            events=[
                self.commit(
                    d + h,
                    f"feat: add keys\n\n{PLANTED['commit']}\nCo-Authored-By: Claude <noreply@anthropic.com>",
                    "a" * 40,
                ),
                self.commit(
                    d + 2 * h, "test: cover retries\n\nCo-Authored-By: Claude <noreply@anthropic.com>", "b" * 40
                ),
                self.review(d + 3 * h, "CHANGES_REQUESTED"),
                self.commit(d + 4 * h, "fix: handle replay", "c" * 40),
                {"event": "head_ref_force_pushed", "created_at": ts(d + 4.5 * h)},
                self.review(d + 5 * h, "APPROVED"),
            ],
        )
        # PR 2: human, secrets in title and branch, deny term.
        self.add_pr(
            cs,
            title=f"chore: rotate {PLANTED['aws_key']} for {PLANTED['deny_term']}",
            author="carol",
            branch=f"fix/{PLANTED['github_token']}",
            created=d + 24 * h,
            merged=d + 30 * h,
            events=[
                self.commit(d + 25 * h, f"chore: config {PLANTED['host']}", "d" * 40),
                self.review(d + 26 * h, "APPROVED"),
            ],
        )
        # PR 3: security label in a tracked repository: restricted by label.
        self.add_pr(
            cs,
            title=PLANTED["security_label_title"],
            author="dave",
            labels=("security",),
            created=d + 48 * h,
            merged=d + 50 * h,
            events=[self.commit(d + 49 * h, "fix: key", "e" * 40)],
        )
        # PR 4: open, call me at a phone number, home path in branch.
        self.add_pr(
            cs,
            title=f"docs: call {PLANTED['phone']} for access",
            author="erin",
            branch=f"docs{PLANTED['home_path']}",
            created=d + 60 * h,
        )
        # PR 5: older than the 180-day backfill cut-off: must not be fetched by default.
        old = self.now - timedelta(days=400)
        self.add_pr(cs, title="feat: ancient history", created=old, merged=old + h)

        wd = "acme/web-dashboard"
        self.add_pr(
            wd,
            title="feat: dark mode",
            author="copilot-swe",
            bot=True,
            branch="copilot/dark-mode",
            created=d,
            merged=d + 3 * h,
            events=[self.commit(d + h, "feat: dark mode", "f" * 40), self.review(d + 2 * h, "APPROVED", "frank")],
        )
        self.add_pr(wd, title="fix: chart overflow", author="frank", created=d + 5 * h, closed=d + 9 * h)

        sp = "acme/security-patches"
        self.add_pr(
            sp,
            title=PLANTED["restricted_title"],
            author="grace",
            branch="fix/cve-2026-1234",
            labels=("embargo",),
            created=d,
            merged=d + 2 * h,
            events=[self.commit(d + h, "fix: bypass", "9" * 40)],
        )

        self.add_pr("acme/untracked", title="never read", created=d)

        self.properties = {
            "acme/checkout-service": {"agentyield-team": "payments"},
            "acme/web-dashboard": {"agentyield-team": "web"},
            "acme/security-patches": {},
            "acme/untracked": {},
        }


class FakeGitHub:
    """A local HTTP server that answers the REST calls AgentYield makes, from an Org."""

    def __init__(self, org: Org):
        self.org = org
        self.requests: list[str] = []
        self.rate_remaining = 5000
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake.requests.append(self.path)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"message": "Bad credentials"})
                parsed = urllib.parse.urlparse(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                parts = parsed.path.strip("/").split("/")
                try:
                    status, body, extra = fake.route(parts, q, self.headers.get("If-None-Match", ""), self.path)
                except KeyError:
                    status, body, extra = 404, {"message": "Not Found"}, {}
                self._send(status, body, extra)

            def _send(self, status: int, body: Any, extra: dict[str, str] | None = None) -> None:
                if status != 304:
                    fake.rate_remaining -= 1
                data = b"" if status == 304 else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Remaining", str(fake.rate_remaining))
                self.send_header("X-RateLimit-Reset", "0")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H

    def _page(self, items: list[Any], q: dict[str, str], path: str) -> tuple[list[Any], dict[str, str]]:
        per = int(q.get("per_page", 30))
        page = int(q.get("page", 1))
        chunk = items[(page - 1) * per : page * per]
        extra = {}
        if page * per < len(items):
            base, _, query = path.partition("?")
            params = dict(urllib.parse.parse_qsl(query))
            params["page"] = str(page + 1)
            extra["Link"] = f'<{self.url}{base}?{urllib.parse.urlencode(params)}>; rel="next"'
        return chunk, extra

    def route(self, parts: list[str], q: dict[str, str], etag: str, path: str) -> tuple[int, Any, dict[str, str]]:
        org = self.org
        if parts[0] == "orgs" and parts[2:] == ["properties", "values"]:
            rows = [
                {"repository_full_name": r, "properties": [{"property_name": k, "value": v} for k, v in p.items()]}
                for r, p in org.properties.items()
                if r.startswith(parts[1] + "/")
            ]
            chunk, extra = self._page(rows, q, path)
            return 200, chunk, extra
        if parts[0] != "repos":
            raise KeyError(parts)
        repo = f"{parts[1]}/{parts[2]}"
        rest = parts[3:]
        if not rest:
            if repo not in org.pulls:
                raise KeyError(repo)
            return 200, {"full_name": repo, "default_branch": "main", "private": True}, {}
        if rest[0] == "contents":
            text = org.files[(repo, "/".join(rest[1:]))]
            return 200, {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}, {}
        if rest == ["pulls"]:
            items = sorted(org.pulls[repo].values(), key=lambda p: p["updated_at"], reverse=True)
            listing = [{k: p[k] for k in ("number", "title", "updated_at", "state")} for p in items]
            chunk, extra = self._page(listing, q, path)
            tag = '"' + hashlib.sha1(json.dumps(chunk).encode()).hexdigest() + '"'
            if etag and etag == tag:
                return 304, None, {"ETag": tag}
            return 200, chunk, {"ETag": tag, **extra}
        if rest[0] == "pulls" and len(rest) == 2:
            return 200, org.pulls[repo][int(rest[1])], {}
        if rest[0] == "issues" and rest[2:] == ["timeline"]:
            chunk, extra = self._page(org.timelines[(repo, int(rest[1]))], q, path)
            return 200, chunk, extra
        raise KeyError(parts)
