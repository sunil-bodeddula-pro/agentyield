"""Config changes are applied on purpose: `config diff` shows what a new config would do, `config apply` does it.

The last applied config is recorded in the store's manifest, so `sync` keeps using it until a valid new one is
applied. Repositories chosen by GitHub custom property are the one exception: `sync` re-reads the property
values each run, so opting a repository in or out in GitHub needs no config change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yaml

from .config import Config, RepoSpec
from .derive import attribution_path, recompute
from .store import Store
from .sync import DEFAULT_BACKFILL_DAYS, RepoResult, Syncer, days_ago, iso


def stored_copy(cfg: Config) -> str:
    """The applied config as kept in the store. Deny terms are left out: they are the very names the filter
    removes, and the store may be a shared bucket. `sync` takes them from the live config file instead."""
    data = json.loads(json.dumps(cfg.data, default=str))
    g = data.get("guardrails") or {}
    if g.get("deny_terms"):
        g["deny_terms"] = []
    header = f"# Applied config {cfg.hash} from {safe_source(cfg)}. Deny terms are not stored here.\n"
    return header + yaml.safe_dump(data, sort_keys=False)


def safe_source(cfg: Config) -> str:
    """Where the config came from, without a home folder (a username) in a store that may be shared."""
    return cfg.filter().clean(cfg.source)[0] or "(local file)"


def rules_hash(cfg: Config) -> str:
    parts = {k: cfg.data.get(k) for k in ("teams", "agents", "functionality", "metrics")}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Change:
    kind: str
    repo: str
    detail: str
    github_calls: bool = False

    def line(self) -> str:
        calls = "fetches from GitHub" if self.github_calls else "no GitHub calls"
        target = f"{self.repo}: " if self.repo else ""
        return f"  {self.kind:<12} {target}{self.detail} ({calls})"


def snapshot(resolved: dict[str, RepoSpec]) -> dict[str, dict[str, Any]]:
    return {k: {"name": s.name, "team": s.team, "restricted": s.restricted, "via": s.via} for k, s in resolved.items()}


def diff(
    cfg: Config, resolved: dict[str, RepoSpec], manifest: dict[str, Any], since: str | None = None
) -> list[Change]:
    applied = manifest.get("applied") or {}
    before: dict[str, dict[str, Any]] = applied.get("repos") or {}
    back = since or f"the last {DEFAULT_BACKFILL_DAYS} days"
    out: list[Change] = []
    if not applied:
        out.append(Change("first apply", "", f"{len(resolved)} repositories to track", bool(resolved)))
    for key, spec in sorted(resolved.items()):
        old = before.get(key)
        if old is None:
            out.append(Change("add", spec.name, f"backfill {back}", True))
            continue
        if spec.restricted and not old.get("restricted"):
            out.append(Change("restrict", spec.name, "blank titles, branches and labels already stored"))
        if old.get("restricted") and not spec.restricted:
            out.append(Change("unrestrict", spec.name, "new data only; older text was never kept"))
        if spec.team != old.get("team", ""):
            out.append(Change("team", spec.name, f"{old.get('team') or '(none)'} -> {spec.team or '(none)'}"))
    for key, old in sorted(before.items()):
        if key not in resolved:
            out.append(Change("remove", old["name"], "stop syncing and hide; data kept until `prune`"))
    if applied and applied.get("guardrails") != cfg.guardrail_fingerprint():
        out.append(Change("guardrails", "", "filter stored PRs again with the new rules"))
    if applied and applied.get("rules") != rules_hash(cfg):
        out.append(Change("rules", "", "team, agent, functionality or metric rules changed; derived numbers rebuilt"))
    return out


def apply(
    cfg: Config, store: Store, syncer: Syncer, resolved: dict[str, RepoSpec], since: str | None = None
) -> tuple[list[Change], list[RepoResult]]:
    manifest = store.manifest()
    changes = diff(cfg, resolved, manifest, since)
    store.acquire()
    try:
        store.put_bytes(f"config/{cfg.hash}.yaml", stored_copy(cfg).encode())
        results: list[RepoResult] = []
        before = (manifest.get("applied") or {}).get("repos") or {}
        hidden = manifest.setdefault("hidden", {})
        for key, spec in sorted(resolved.items()):
            hidden.pop(key, None)
            if key not in before:
                results.append(syncer.sync_repo(spec, since or days_ago(DEFAULT_BACKFILL_DAYS)))
            elif spec.team != before[key].get("team", ""):
                retag_team(store, spec)
        for key, old in before.items():
            if key not in resolved:
                hidden[key] = {"name": old["name"], "since": iso(datetime.now(UTC))}
        if any(c.kind in ("restrict", "guardrails") for c in changes):
            syncer.refilter(resolved)
        manifest["applied"] = {
            "hash": cfg.hash,
            "source": safe_source(cfg),
            "at": iso(datetime.now(UTC)),
            "guardrails": cfg.guardrail_fingerprint(),
            "rules": rules_hash(cfg),
            "repos": snapshot(resolved),
        }
        if results:
            manifest["last_sync"] = {
                "at": iso(datetime.now(UTC)),
                "fetched": sum(r.fetched for r in results),
                "errors": [f"{r.repo}: {r.error}" for r in results if r.error],
            }
        store.save_manifest(manifest)
        recompute(store, cfg, resolved)
        return changes, results
    finally:
        store.release()


def retag_team(store: Store, spec: RepoSpec) -> None:
    for rel in store.pr_files(spec.name):
        rec = store.read(rel)
        if rec and rec.get("team") != spec.team:
            rec["team"] = spec.team
            store.write(rel, rec)


def applied_repos(manifest: dict[str, Any]) -> dict[str, RepoSpec]:
    repos = (manifest.get("applied") or {}).get("repos") or {}
    return {
        k: RepoSpec(
            name=v["name"], team=v.get("team", ""), restricted=bool(v.get("restricted")), via=v.get("via", "include")
        )
        for k, v in repos.items()
    }


def status(store: Store) -> dict[str, Any]:
    manifest = store.manifest()
    applied = manifest.get("applied") or {}
    repos = []
    for key, r in sorted((applied.get("repos") or {}).items()):
        cursor = store.read(f"{store.repo_dir(r['name'])}/cursor.json", {}) or {}
        attr = (store.read(attribution_path(r["name"]), {}) or {}).get("summary") or {}
        repos.append(
            {
                "attribution": attr,
                "repo": r["name"],
                "team": r.get("team", ""),
                "via": r.get("via", ""),
                "restricted": r.get("restricted", False),
                "prs": len(store.pr_files(r["name"])),
                "newest_update": cursor.get("newest", ""),
                "synced_at": cursor.get("synced_at", ""),
                "history_from": cursor.get("backfilled_to", ""),
                "key": key,
            }
        )
    return {
        "store": store.uri,
        "config": applied.get("source", ""),
        "config_hash": applied.get("hash", ""),
        "applied_at": applied.get("at", ""),
        "repos": repos,
        "hidden": sorted(manifest.get("hidden", {})),
        "last_sync": manifest.get("last_sync", {}),
        "lease": store.read(".lease"),
    }
