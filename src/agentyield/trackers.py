"""Jira and Linear: which features (epics, projects) the pull requests delivered.

A PR is linked to tracker issues by their keys (ABC-123) in its title, branch name or description, for the project
or team prefixes you list, so a word like UTF-8 is never mistaken for an issue. Each linked issue is fetched once
and refreshed until it is done. The feature is the issue's epic (Jira) or project (Linear); with `feature: parent`
it is the parent issue instead.

Stored per issue: key, type, whether it's done and when, its parent or project, and its title and labels after the
guardrail filter (blank for restricted labels). Descriptions and comments are never fetched.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from .guardrails import Filter

KEY = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9})-(\d{1,7})(?![0-9])")
REFRESH_OPEN = 6 * 3600  # open issues are re-read at most every 6 hours; done ones are kept


class TrackerError(RuntimeError):
    pass


def extract_keys(texts: list[str], prefixes: set[str]) -> list[str]:
    """Tracker keys with one of the configured prefixes, e.g. ['ABC-12'] from 'feature/ABC-12-checkout'."""
    if not prefixes:
        return []
    found = {f"{p}-{n}" for text in texts for p, n in KEY.findall(text or "") if p in prefixes}
    return sorted(found, key=lambda k: (k.split("-")[0], int(k.split("-")[1])))


def _request(url: str, headers: dict[str, str], body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if body is not None else "GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agentyield",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        raise TrackerError(f"{urllib.parse.urlparse(url).netloc} answered {err.code}") from None
    except urllib.error.URLError as err:
        raise TrackerError(f"{urllib.parse.urlparse(url).netloc} unreachable: {err.reason}") from None


class Tracker(Protocol):
    system: str
    prefixes: set[str]
    feature_mode: str

    def issue(self, key: str) -> dict[str, Any]: ...


class Jira:
    """Jira Cloud REST API v3, basic auth with an account email and API token."""

    system = "jira"
    FIELDS = "summary,issuetype,status,parent,labels,created,resolutiondate"

    def __init__(self, base_url: str, email: str, token: str, projects: list[str], feature: str = "epic"):
        self.base_url = base_url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        self.prefixes = {p.upper() for p in projects}
        self.feature_mode = feature

    def issue(self, key: str) -> dict[str, Any]:
        data = _request(
            f"{self.base_url}/rest/api/3/issue/{urllib.parse.quote(key)}?fields={self.FIELDS}",
            {"Authorization": self.auth},
        )
        f = data.get("fields") or {}
        parent = f.get("parent") or {}
        parent_type = ((parent.get("fields") or {}).get("issuetype") or {}).get("name", "")
        issue_type = (f.get("issuetype") or {}).get("name", "")
        if issue_type.lower() == "epic":
            feature = data.get("key", key)
        elif parent and (self.feature_mode == "parent" or parent_type.lower() == "epic"):
            feature = parent.get("key", "")
        else:
            feature = ""
        return {
            "key": data.get("key", key),
            "system": "jira",
            "type": issue_type,
            "done": ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done",
            "done_at": f.get("resolutiondate"),
            "created_at": f.get("created"),
            "parent": parent.get("key", ""),
            "feature": feature,
            "title": f.get("summary", ""),
            "labels": list(f.get("labels") or []),
        }


class Linear:
    """Linear's GraphQL API with a personal API key (sent as-is, without a Bearer prefix)."""

    system = "linear"
    QUERY = (
        "query($id: String!) { issue(id: $id) { identifier title createdAt completedAt state { type } "
        "labels { nodes { name } } parent { identifier } project { id name } } }"
    )

    def __init__(
        self, api_key: str, teams: list[str], feature: str = "project", api_url: str = "https://api.linear.app/graphql"
    ):
        self.api_key, self.api_url = api_key, api_url
        self.prefixes = {t.upper() for t in teams}
        self.feature_mode = feature

    def issue(self, key: str) -> dict[str, Any]:
        data = _request(self.api_url, {"Authorization": self.api_key}, {"query": self.QUERY, "variables": {"id": key}})
        if data.get("errors") or not (data.get("data") or {}).get("issue"):
            raise TrackerError(f"Linear has no issue {key}")
        i = data["data"]["issue"]
        project = i.get("project") or {}
        parent = (i.get("parent") or {}).get("identifier", "")
        if self.feature_mode == "parent":
            feature = parent
        else:
            feature = f"project:{project['id']}" if project.get("id") else parent
        return {
            "key": i.get("identifier", key),
            "system": "linear",
            "type": "issue",
            "done": (i.get("state") or {}).get("type") == "completed",
            "done_at": i.get("completedAt"),
            "created_at": i.get("createdAt"),
            "parent": parent,
            "feature": feature,
            "feature_title": project.get("name", "") if feature.startswith("project:") else "",
            "title": i.get("title", ""),
            "labels": [n.get("name", "") for n in (i.get("labels") or {}).get("nodes") or []],
        }


def trackers_from(functionality: dict[str, Any]) -> list[Tracker]:
    out: list[Tracker] = []
    jira = functionality.get("jira") or {}
    if jira:
        email, token = (
            os.environ.get(str(jira.get("email_env") or "JIRA_EMAIL"), ""),
            os.environ.get(str(jira.get("token_env") or "JIRA_API_TOKEN"), ""),
        )
        if not (email and token):
            raise SystemExit("functionality.jira needs its email and API token environment variables set")
        out.append(
            Jira(
                str(jira["base_url"]),
                email,
                token,
                list(jira.get("projects") or []),
                str(jira.get("feature") or "epic"),
            )
        )
    linear = functionality.get("linear") or {}
    if linear:
        key = os.environ.get(str(linear.get("api_key_env") or "LINEAR_API_KEY"), "")
        if not key:
            raise SystemExit("functionality.linear needs its API key environment variable set")
        out.append(
            Linear(
                key,
                list(linear.get("teams") or []),
                str(linear.get("feature") or "project"),
                str(linear.get("api_url") or "https://api.linear.app/graphql"),
            )
        )
    return out


def fetch(tracker: Tracker, key: str, old: dict[str, Any] | None, flt: Filter) -> dict[str, Any] | None:
    """The filtered issue, from the cache when it's done or fresh enough."""
    if old and (old.get("done") or time.time() - float(old.get("fetched", 0)) < REFRESH_OPEN):
        return old
    try:
        issue = tracker.issue(key)
    except TrackerError:
        return old
    restricted = flt.restricted_label(issue["labels"])
    issue["restricted"] = restricted
    issue["title"] = "" if restricted else flt.clean(issue["title"])[0]
    if issue.get("feature_title"):
        issue["feature_title"] = "" if restricted else flt.clean(issue["feature_title"])[0]
    issue["labels"] = [] if restricted else flt.clean_all(issue["labels"])[0]
    issue["fetched"] = time.time()
    return issue
