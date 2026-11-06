"""Fetch what changed on GitHub since the last sync, filter it, and store one JSON file per pull request.

PRs are listed newest-updated first, so a sync stops at the first PR it has already seen. The first page is
requested with the previous ETag; `304 Not Modified` means nothing changed and costs no rate limit. A repository
seen for the first time is backfilled back to a cut-off date (180 days unless told otherwise).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import survival
from .attribution import DEFAULT_EXCLUDED_FILES, excluded_lines, needs_line_counts
from .config import Config, RepoSpec
from .facts import AgentRule, build_record
from .github import GitHub, GitHubError
from .guardrails import Filter
from .quality import add_ci, add_scanning, sync_deployments
from .store import Store

DEFAULT_BACKFILL_DAYS = 180


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(days: int) -> str:
    return iso(datetime.now(UTC) - timedelta(days=days))


@dataclass
class RepoResult:
    repo: str
    fetched: int = 0
    unchanged: bool = False
    newest: str = ""
    error: str = ""
    redactions: dict[str, int] = field(default_factory=dict)


class Syncer:
    def __init__(self, store: Store, gh: GitHub, cfg: Config):
        self.store, self.gh, self.cfg = store, gh, cfg
        self.flt: Filter = cfg.filter()
        self.agents: dict[str, AgentRule] = cfg.agents()
        self._cursor: dict[str, Any] = {}

    def cursor_path(self, repo: str) -> str:
        return f"{self.store.repo_dir(repo)}/cursor.json"

    def sync_repo(self, spec: RepoSpec, since: str | None = None) -> RepoResult:
        """New and updated PRs since the cursor; with `since`, everything updated after that date (a backfill)."""
        res = RepoResult(spec.name)
        cursor = self.store.read(self.cursor_path(spec.name), {}) or {}
        self._cursor = cursor
        backfill = since is not None or not cursor
        cutoff = since or cursor.get("newest") or days_ago(DEFAULT_BACKFILL_DAYS)
        newest = cursor.get("newest", "")
        url: str | None = None
        first = True
        try:
            while True:
                page = self.gh.pulls_page(spec.name, url, "" if backfill or not first else cursor.get("etag", ""))
                if page.status == 304:
                    res.unchanged = True
                    break
                if first:
                    cursor["etag"] = page.etag
                    first = False
                done = False
                for pr in page.data or []:
                    updated = pr.get("updated_at", "")
                    if updated <= cutoff:  # already seen, or older than the backfill cut-off
                        done = True
                        break
                    self._store_pr(spec, pr["number"], res)
                    newest = max(newest, updated)
                url = page.next_url
                if done or not url:
                    break
        except GitHubError as exc:
            res.error = str(exc)
        if not res.error:
            self.sync_releases(spec)
            self.sync_deployments(spec, cutoff if backfill else days_ago(DEFAULT_BACKFILL_DAYS))
            if self.cfg.section("metrics").get("code_survival"):
                self.measure_survival(spec)
            cursor["newest"] = newest or cursor.get("newest", "")
            cursor["synced_at"] = iso(datetime.now(UTC))
            if since and (not cursor.get("backfilled_to") or since < cursor["backfilled_to"]):
                cursor["backfilled_to"] = since
            cursor.setdefault("backfilled_to", cutoff)
            self.store.write(self.cursor_path(spec.name), cursor)
        res.newest = newest
        return res

    def _store_pr(self, spec: RepoSpec, number: int, res: RepoResult) -> None:
        detail = self.gh.pull(spec.name, number)
        timeline = self.gh.timeline(spec.name, number)
        record = build_record(spec.name, detail, timeline, self.flt, self.agents, spec.restricted)
        old = self.store.read(self.store.pr_path(spec.name, number), {}) or {}
        self._files(spec, record)
        self._line_counts(spec, record)
        add_ci(self.gh, spec.name, record, old, dict(self.cfg.section("metrics").get("coverage") or {}))
        add_scanning(self.gh, spec.name, record, self._cursor)
        if old.get("survival") and old.get("merge_commit_sha") == record.get("merge_commit_sha"):
            record["survival"] = old["survival"]
        for n in record["closes"]:
            self._store_issue(spec, n)
        record["team"] = spec.team
        record["fetched_at"] = iso(datetime.now(UTC))
        record["config"] = self.cfg.hash
        for kind in record["redacted"]:
            res.redactions[kind] = res.redactions.get(kind, 0) + 1
        self.store.write(self.store.pr_path(spec.name, number), record)
        res.fetched += 1

    def _files(self, spec: RepoSpec, record: dict[str, Any]) -> None:
        """Changed lines without generated, vendored and lock files, and the changed files as short hashes:
        enough to spot a later PR touching the same files, without storing paths (they can name projects)."""
        patterns = DEFAULT_EXCLUDED_FILES + spec.exclude_paths
        files = self.gh.pull_files(spec.name, record["number"])
        kept = [f for f in files if not excluded_lines(str(f.get("filename", "")), patterns)]
        record["lines"] = sum(int(f.get("additions", 0)) + int(f.get("deletions", 0)) for f in kept)
        record["files"] = sorted({hashlib.sha256(str(f.get("filename", "")).encode()).hexdigest()[:12] for f in kept})

    def _store_issue(self, spec: RepoSpec, number: int) -> None:
        try:
            issue = self.gh.issue(spec.name, number)
        except GitHubError:
            return  # deleted, transferred, or not visible: the PR still counts, the issue doesn't
        labels = [lb.get("name", "") for lb in issue.get("labels") or []]
        restricted = spec.restricted or self.flt.restricted_label(labels)
        self.store.write(
            f"{self.store.repo_dir(spec.name)}/issues/{number}.json",
            {
                "number": number,
                "state": issue.get("state", ""),
                "closed_at": issue.get("closed_at"),
                "created_at": issue.get("created_at"),
                "restricted": restricted,
                "title": "" if restricted else self.flt.clean(issue.get("title"))[0],
                "labels": [] if restricted else self.flt.clean_all(labels)[0],
                "type": ((issue.get("type") or {}).get("name") or "") if isinstance(issue.get("type"), dict) else "",
            },
        )

    def sync_deployments(self, spec: RepoSpec, since: str) -> None:
        envs = list(self.cfg.section("metrics").get("production_environments") or ["production", "prod"])
        path = f"{self.store.repo_dir(spec.name)}/deployments.json"
        old = self.store.read(path, {}) or {}
        self.store.write(path, sync_deployments(self.gh, spec.name, envs, old, min(since, old.get("since", since))))

    def measure_survival(self, spec: RepoSpec) -> int:
        """git blame on a local mirror for merged PRs at least 30 days old, re-measured weekly."""
        if not survival.available():
            return 0
        now = datetime.now(UTC)
        due = [rel for rel in self.store.pr_files(spec.name) if survival.due(self.store.read(rel) or {}, now)]
        if not due:
            return 0
        base = os.environ.get("AGENTYIELD_GIT_BASE") or str(
            self.cfg.section("github").get("web_url") or "https://github.com"
        )
        cache = str(self.cfg.data.get("cache_dir") or "~/.agentyield/cache")
        token = "" if base.startswith(("file:", "/")) else self.gh.token
        try:
            path = survival.mirror(cache, spec.name, f"{base.rstrip('/')}/{spec.name}.git", token)
        except (OSError, subprocess.SubprocessError):
            return 0
        patterns = DEFAULT_EXCLUDED_FILES + spec.exclude_paths
        done = 0
        for rel in due:
            rec = self.store.read(rel)
            if rec and survival.update(rec, path, patterns, now):
                done += 1
            if rec:
                self.store.write(rel, rec)
        return done

    def sync_releases(self, spec: RepoSpec) -> int:
        path = f"{self.store.repo_dir(spec.name)}/releases.json"
        old = self.store.read(path, {}) or {}
        try:
            resp = self.gh.releases_page(spec.name, old.get("etag", ""))
        except GitHubError:
            return 0
        if resp.status == 304:
            return 0
        releases = [
            {
                "tag": "" if spec.restricted else self.flt.clean(r.get("tag_name"))[0],
                "published_at": r.get("published_at"),
                "draft": bool(r.get("draft")),
                "prerelease": bool(r.get("prerelease")),
            }
            for r in resp.data or []
            if r.get("published_at")
        ]
        self.store.write(path, {"etag": resp.etag, "releases": releases})
        return len(releases)

    def _line_counts(self, spec: RepoSpec, record: dict[str, Any]) -> None:
        """Changed lines per commit, for PRs mixing agent and human commits. Counts already stored for a commit
        are reused, so a PR is never counted twice. Generated, vendored and lock files don't count."""
        if not needs_line_counts(record, self.agents):
            return
        old = self.store.read(self.store.pr_path(spec.name, record["number"]), {}) or {}
        known = {c["sha"]: c["lines"] for c in old.get("commits") or [] if isinstance(c.get("lines"), int)}
        patterns = DEFAULT_EXCLUDED_FILES + spec.exclude_paths
        for c in record["commits"]:
            if c["sha"] in known:
                c["lines"] = known[c["sha"]]
                continue
            files = self.gh.commit_files(spec.name, c["sha"])
            c["lines"] = sum(
                int(f.get("additions", 0)) + int(f.get("deletions", 0))
                for f in files
                if not excluded_lines(str(f.get("filename", "")), patterns)
            )

    def refilter(self, repos: dict[str, RepoSpec]) -> int:
        """Apply the current guardrails to stored PRs: blank newly restricted ones, re-clean the rest.
        Text removed earlier can't come back without a refetch."""
        changed = 0
        for spec in repos.values():
            for rel in self.store.pr_files(spec.name):
                rec = self.store.read(rel)
                if not rec:
                    continue
                before = (rec.get("title"), rec.get("branch"), rec.get("labels"), rec.get("restricted"))
                # once restricted, always restricted: the blanked text is gone
                restricted = (
                    spec.restricted or rec.get("restricted") or self.flt.restricted_label(rec.get("labels") or [])
                )
                if restricted:
                    rec.update(restricted=True, title="", branch="", labels=[])
                else:
                    rec["title"] = self.flt.clean(rec.get("title"))[0]
                    rec["branch"] = self.flt.clean(rec.get("branch"))[0]
                    rec["labels"] = self.flt.clean_all(rec.get("labels") or [])[0]
                if (rec.get("title"), rec.get("branch"), rec.get("labels"), rec.get("restricted")) != before:
                    self.store.write(rel, rec)
                    changed += 1
        return changed


def run_sync(
    syncer: Syncer, repos: dict[str, RepoSpec], since: str | None = None, only: list[str] | None = None
) -> dict[str, Any]:
    t0 = time.perf_counter()
    results = []
    for key, spec in sorted(repos.items()):
        if only and key not in {o.lower() for o in only}:
            continue
        results.append(syncer.sync_repo(spec, since))
    return {
        "repos": [r.__dict__ for r in results],
        "fetched": sum(r.fetched for r in results),
        "errors": [f"{r.repo}: {r.error}" for r in results if r.error],
        "requests": syncer.gh.requests,
        "rate_remaining": syncer.gh.rate_remaining,
        "seconds": round(time.perf_counter() - t0, 2),
    }
