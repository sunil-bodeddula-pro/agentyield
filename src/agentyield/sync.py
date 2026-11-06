"""Fetch what changed on GitHub since the last sync, filter it, and store one JSON file per pull request.

PRs are listed newest-updated first, so a sync stops at the first PR it has already seen. The first page is
requested with the previous ETag; `304 Not Modified` means nothing changed and costs no rate limit. A repository
seen for the first time is backfilled back to a cut-off date (180 days unless told otherwise).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config, RepoSpec
from .facts import AgentRule, build_record
from .github import GitHub, GitHubError
from .guardrails import Filter
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

    def cursor_path(self, repo: str) -> str:
        return f"{self.store.repo_dir(repo)}/cursor.json"

    def sync_repo(self, spec: RepoSpec, since: str | None = None) -> RepoResult:
        """New and updated PRs since the cursor; with `since`, everything updated after that date (a backfill)."""
        res = RepoResult(spec.name)
        cursor = self.store.read(self.cursor_path(spec.name), {}) or {}
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
        record["team"] = spec.team
        record["fetched_at"] = iso(datetime.now(UTC))
        record["config"] = self.cfg.hash
        for kind in record["redacted"]:
            res.redactions[kind] = res.redactions.get(kind, 0) + 1
        self.store.write(self.store.pr_path(spec.name, number), record)
        res.fetched += 1

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
