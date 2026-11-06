"""AgentYield command line.

agentyield init                      write a starter agentyield.yaml (pick repositories from `gh repo list`)
agentyield config validate           check the file; line numbers for every problem
agentyield config show               where the config came from, its hash, the repositories it resolves to
agentyield config diff               what `apply` would change; changes nothing
agentyield config apply              apply it: backfill added repositories, hide removed ones, re-filter
agentyield config schema             print the JSON Schema for editors
agentyield sync                      fetch what changed since the last sync
agentyield backfill <owner/repo>     fetch older history (--since 2025-01-01 or 365d); --all for every repository
agentyield status                    what is tracked, how far back, when it last synced
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from . import __version__
from . import config as cfgmod
from .github import GitHub, GitHubError, token_from
from .ops import applied_repos, apply, diff, snapshot, status
from .schema import json_schema
from .store import LeaseHeld, Store
from .sync import Syncer, days_ago, iso, run_sync


def parse_since(value: str | None) -> str | None:
    if not value:
        return None
    m = re.fullmatch(r"(\d+)d", value)
    if m:
        return days_ago(int(m.group(1)))
    try:
        return iso(datetime.fromisoformat(value).replace(tzinfo=datetime.fromisoformat(value).tzinfo or UTC))
    except ValueError:
        raise SystemExit(f"--since takes a date (2025-01-31) or a number of days (365d), not {value!r}") from None


def github_for(cfg: cfgmod.Config | None = None) -> GitHub:
    api = cfg.api_url if cfg else os.environ.get("AGENTYIELD_GITHUB_API", "https://api.github.com")
    return GitHub(token_from(cfg.token_env if cfg else "GITHUB_TOKEN"), api)


def load_config(flag: str | None) -> cfgmod.Config:
    source = cfgmod.locate(flag)
    gh: GitHub | None = None

    def fetch(repo: str, path: str, ref: str) -> str:
        nonlocal gh
        gh = gh or github_for()
        return gh.file_text(repo, path, ref)

    return cfgmod.load(source, fetch)


def resolve(cfg: cfgmod.Config, gh: GitHub) -> dict[str, cfgmod.RepoSpec]:
    return cfg.resolve_repos(gh.org_property_values)


# ---------------------------------------------------------------- commands
def cmd_validate(args: argparse.Namespace) -> int:
    source = cfgmod.locate(args.config)
    try:
        cfg = load_config(args.config)
    except cfgmod.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{source}: valid (hash {cfg.hash})")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    gh = github_for(cfg)
    repos = resolve(cfg, gh)
    print(f"Config     {cfg.source}")
    print(f"Hash       {cfg.hash}")
    print(f"Store      {cfg.store_uri}")
    print(f"GitHub     {cfg.api_url} (token from {cfg.token_env} or the gh CLI)")
    print(f"Tracks     {len(repos)} repositories")
    for spec in sorted(repos.values(), key=lambda s: s.name.lower()):
        flags = ", ".join(
            x
            for x in (
                spec.team and f"team {spec.team}",
                spec.restricted and "restricted",
                spec.via == "select" and "by custom property",
            )
            if x
        )
        print(f"  {spec.name}{f'  ({flags})' if flags else ''}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.store_uri)
    changes = diff(cfg, resolve(cfg, github_for(cfg)), store.manifest(), parse_since(args.since))
    print("\n".join(c.line() for c in changes) if changes else "No changes: the store already uses this config.")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    gh = github_for(cfg)
    store = Store(cfg.store_uri)
    resolved = resolve(cfg, gh)
    since = parse_since(args.since)
    changes = diff(cfg, resolved, store.manifest(), since)
    if not changes:
        print("No changes: the store already uses this config.")
        return 0
    print("This will:\n" + "\n".join(c.line() for c in changes))
    if not args.yes and (not sys.stdin.isatty() or input("Apply? [y/N] ").strip().lower() not in ("y", "yes")):
        print("Nothing applied.")
        return 1
    _, results = apply(cfg, store, Syncer(store, gh, cfg), resolved, since)
    for r in results:
        print(f"  {r.repo}: {r.fetched} PRs stored" + (f" (error: {r.error})" if r.error else ""))
    print(
        f"Applied config {cfg.hash} to {store.uri}. GitHub requests: {gh.requests}, rate limit left: "
        f"{gh.rate_remaining}"
    )
    return 1 if any(r.error for r in results) else 0


def _applied_config(store: Store, current: cfgmod.Config) -> cfgmod.Config:
    """The config last applied to this store. Its deny terms come from the live config, since the stored copy
    deliberately leaves them out; the filter is never weaker than what the live file says."""
    applied = store.manifest().get("applied") or {}
    raw = store.get_bytes(f"config/{applied.get('hash')}.yaml") if applied else None
    if raw is None:
        raise SystemExit("Nothing applied to this store yet: run `agentyield config apply` first.")
    cfg = cfgmod.Config(source=applied["source"], text=raw.decode(), data=cfgmod.check(raw.decode())[0])
    cfg.hash_override = applied["hash"]
    cfg.deny_terms = list(current.section("guardrails").get("deny_terms") or []) + current.deny_terms
    return cfg


def cmd_sync(args: argparse.Namespace, since: str | None = None, only: list[str] | None = None) -> int:
    current = load_config(args.config)
    store = Store(current.store_uri)
    cfg = _applied_config(store, current)  # sync runs the applied config
    if current.hash != cfg.hash:
        print(f"Note: {current.source} has changes that aren't applied yet; run `agentyield config diff`.")
    gh = github_for(cfg)
    syncer = Syncer(store, gh, cfg)
    manifest = store.manifest()
    repos = applied_repos(manifest)
    if cfg.section("repositories").get("select"):  # custom-property changes need no config apply
        now = resolve(cfg, gh)
        added = {k: v for k, v in now.items() if k not in repos}
        gone = [k for k, v in repos.items() if v.via == "select" and k not in now]
        if added or gone:
            for k in gone:
                manifest.setdefault("hidden", {})[k] = {"name": repos.pop(k).name, "since": iso(datetime.now(UTC))}
            for k, spec in added.items():
                print(f"  {spec.name}: opted in by custom property, backfilling")
                syncer.sync_repo(spec, days_ago(180))
                repos[k] = spec
            manifest["applied"]["repos"] = snapshot(repos)
            store.save_manifest(manifest)
    try:
        store.acquire()
    except LeaseHeld as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        out = run_sync(syncer, repos, since, only)
        manifest = store.manifest()
        manifest["last_sync"] = {"at": iso(datetime.now(UTC)), "fetched": out["fetched"], "errors": out["errors"]}
        store.save_manifest(manifest)
    finally:
        store.release()
    for r in out["repos"]:
        state = "unchanged" if r["unchanged"] else f"{r['fetched']} PRs"
        print(f"  {r['repo']}: {state}" + (f" (error: {r['error']})" if r["error"] else ""))
    print(f"Synced in {out['seconds']}s. GitHub requests: {out['requests']}, rate limit left: {out['rate_remaining']}")
    return 1 if out["errors"] else 0


def cmd_backfill(args: argparse.Namespace) -> int:
    if not args.all and not args.repo:
        raise SystemExit("name a repository (owner/repo) or pass --all")
    since = parse_since(args.since) or days_ago(180)
    return cmd_sync(args, since, None if args.all else [args.repo])


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    s = status(Store(cfg.store_uri))
    if args.json:
        print(json.dumps(s, indent=1))
        return 0
    print(f"Store      {s['store']}")
    applied = s["config"] or "nothing yet"
    print(f"Applied    {applied}" + (f" (hash {s['config_hash']}, {s['applied_at']})" if s["config_hash"] else ""))
    if s["last_sync"]:
        print(f"Last sync  {s['last_sync'].get('at')}: {s['last_sync'].get('fetched')} PRs fetched")
    for r in s["repos"]:
        print(
            f"  {r['repo']:<40} {r['prs']:>5} PRs  history from {r['history_from'][:10] or '-'}  "
            f"synced {r['synced_at'][:16] or 'never'}{'  restricted' if r['restricted'] else ''}"
        )
    if s["hidden"]:
        print(f"Hidden (removed, data kept): {', '.join(s['hidden'])}")
    if s["lease"]:
        print(f"Writer     {s['lease'].get('holder')}")
    return 0


TEMPLATE = """\
# AgentYield configuration. Reference: agentyield config schema, and the README.
version: 1

store: {store}

repositories:
  include:
{repos}

guardrails:
  restricted_labels: [security, embargo]
  comment_excerpts: false
"""


def cmd_init(args: argparse.Namespace) -> int:
    path = os.path.expanduser(args.path or cfgmod.user_config_path())
    if os.path.exists(path) and not args.force:
        raise SystemExit(f"{path} already exists (use --force to overwrite)")
    repos: list[str] = list(args.repo or [])
    if not repos:
        gh = shutil.which("gh")
        if not gh or not sys.stdin.isatty():
            raise SystemExit("pass the repositories to track: agentyield init --repo owner/name [--repo …]")
        out = subprocess.run(
            [gh, "repo", "list", "--limit", "100", "--json", "nameWithOwner", "-q", ".[].nameWithOwner"],
            capture_output=True,
            text=True,
            check=True,
        )
        names = out.stdout.split()
        for i, n in enumerate(names, 1):
            print(f"{i:>3}. {n}")
        picks = input("Repositories to track (numbers separated by spaces): ").split()
        repos = [names[int(p) - 1] for p in picks if p.isdigit() and 0 < int(p) <= len(names)]
    if not repos:
        raise SystemExit("no repositories chosen")
    body = TEMPLATE.format(store=args.store or cfgmod.DEFAULT_STORE, repos="\n".join(f"    - name: {r}" for r in repos))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print(f"Wrote {path} with {len(repos)} repositories.\nNext: agentyield config apply")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentyield", description="How much code AI agents ship, how well, how fast.")
    p.add_argument("--version", action="version", version=f"agentyield {__version__}")
    p.add_argument("--config", help="path or URI of agentyield.yaml (see `agentyield config show`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="write a starter agentyield.yaml")
    i.add_argument("--repo", action="append", help="owner/name to track; repeatable")
    i.add_argument("--path", help=f"where to write it (default {cfgmod.user_config_path()})")
    i.add_argument("--store", help=f"data location (default {cfgmod.DEFAULT_STORE})")
    i.add_argument("--force", action="store_true")

    c = sub.add_parser("config", help="validate, show, diff, apply, schema")
    csub = c.add_subparsers(dest="action", required=True)
    for name in ("validate", "show", "diff", "apply", "schema"):
        a = csub.add_parser(name)
        if name in ("diff", "apply"):
            a.add_argument("--since", help="history for added repositories: a date or Nd (default 180d)")
        if name == "apply":
            a.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")

    sub.add_parser("sync", help="fetch what changed since the last sync")
    b = sub.add_parser("backfill", help="fetch older history")
    b.add_argument("repo", nargs="?", help="owner/name")
    b.add_argument("--all", action="store_true", help="every tracked repository")
    b.add_argument("--since", help="a date (2025-01-31) or Nd (365d); default 180d")
    s = sub.add_parser("status", help="what is tracked and when it last synced")
    s.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: dict[str, Any] = {"validate": cmd_validate, "show": cmd_show, "diff": cmd_diff, "apply": cmd_apply}
    try:
        if args.cmd == "config":
            if args.action == "schema":
                print(json.dumps(json_schema(), indent=2))
                return 0
            return int(handlers[args.action](args))
        return int({"init": cmd_init, "sync": cmd_sync, "backfill": cmd_backfill, "status": cmd_status}[args.cmd](args))
    except cfgmod.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except GitHubError as exc:
        print(exc, file=sys.stderr)
        return 1
    except LeaseHeld as exc:
        print(exc, file=sys.stderr)
        return 2
