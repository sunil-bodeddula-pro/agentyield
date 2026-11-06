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
agentyield attribution [--prs]       who wrote the PRs: agent, assisted or human, with the evidence
agentyield label set <repo#n> <label>  correct one PR's label; it survives re-syncs (also: clear, list)
agentyield recompute                 rebuild derived files from stored facts, with no GitHub calls
agentyield metrics [--scope team:x]  agent share of code, speed, review and reliability, agent against human
agentyield serve [--every 15m]       the live dashboard, syncing on a timer
agentyield build [--out site]        the dashboard as static files: totals only, no PR list or titles
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

from . import __version__, web
from . import config as cfgmod
from .derive import SUMMARY, attribution_path, clear_correction, corrections, recompute, set_correction
from .github import AppToken, GitHub, GitHubError, token_from
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
    """A GitHub App installation when the config names one and its variables are set, else a token or gh."""
    api = cfg.api_url if cfg else os.environ.get("AGENTYIELD_GITHUB_API", "https://api.github.com")
    auth = (cfg.section("github").get("auth") or {}) if cfg else {}
    names = [auth.get(k) for k in ("app_id_env", "private_key_env", "installation_id_env")]
    if all(names) and all(os.environ.get(str(n)) for n in names):
        app_id, key, inst = (os.environ[str(n)] for n in names)
        if not key.lstrip().startswith("-----") and os.path.exists(key):  # a path to the .pem file
            with open(key, encoding="utf-8") as fh:
                key = fh.read()
        return GitHub(AppToken(app_id, key, inst, api), api)
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
        recompute(store, cfg, repos)
    finally:
        store.release()
    for r in out["repos"]:
        state = "unchanged" if r["unchanged"] else f"{r['fetched']} PRs"
        print(f"  {r['repo']}: {state}" + (f" (error: {r['error']})" if r["error"] else ""))
    print(f"Synced in {out['seconds']}s. GitHub requests: {out['requests']}, rate limit left: {out['rate_remaining']}")
    return 1 if out["errors"] else 0


def applied_state(args: argparse.Namespace) -> tuple[Store, cfgmod.Config, dict[str, cfgmod.RepoSpec]]:
    current = load_config(args.config)
    store = Store(current.store_uri)
    return store, _applied_config(store, current), applied_repos(store.manifest())


def cmd_recompute(args: argparse.Namespace) -> int:
    store, cfg, repos = applied_state(args)
    summary = recompute(store, cfg, repos)
    for repo, info in sorted(summary.items()):
        print(f"  {repo}: {info['prs']} PRs, attribution coverage {info['coverage']:.0%}")
    print("Rebuilt the derived files from stored facts (no GitHub calls).")
    return 0


PR_KEY = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def cmd_label(args: argparse.Namespace) -> int:
    store, cfg, repos = applied_state(args)
    if args.action == "list":
        items = corrections(store)
        for key, c in sorted(items.items()):
            print(f"  {key}: {c['label']}{' (' + c['agent'] + ')' if c.get('agent') else ''}  {c.get('note', '')}")
        if not items:
            print("No corrections.")
        return 0
    m = PR_KEY.match(args.pr or "")
    if not m:
        raise SystemExit("name the PR as owner/repo#number, for example acme/checkout-service#42")
    repo_key = m.group(1).lower()
    if repo_key not in repos:
        raise SystemExit(f"{m.group(1)} isn't tracked by the applied config")
    key = f"{repos[repo_key].name}#{m.group(2)}"
    if args.action == "set":
        set_correction(
            store, cfg.filter(), key, args.label, args.agent or "", args.note or "", os.environ.get("USER", "")
        )
        print(f"{key} is now labelled {args.label}; it stays that way through re-syncs.")
    elif clear_correction(store, key):
        print(f"Removed the correction for {key}.")
    else:
        print(f"No correction for {key}.")
    recompute(store, cfg, {repo_key: repos[repo_key]})
    return 0


def cmd_attribution(args: argparse.Namespace) -> int:
    store, _, repos = applied_state(args)
    out: dict[str, Any] = {}
    for key, spec in sorted(repos.items()):
        if args.repo and key != args.repo.lower():
            continue
        out[spec.name] = store.read(attribution_path(spec.name), {}) or {}
    if args.json:
        print(json.dumps(out, indent=1))
        return 0
    for repo, data in out.items():
        info = data.get("summary") or {}
        if not info:
            print(f"{repo}: nothing computed yet (run `agentyield sync`)")
            continue
        lb = info["labels"]
        agents = ", ".join(f"{a} {n}" for a, n in info["agents"].items()) or "none"
        print(
            f"{repo}: {info['prs']} PRs | agent {lb['agent']}, assisted {lb['assisted']}, human {lb['human']}, "
            f"excluded {lb['excluded']} | coverage {info['coverage']:.0%} | agents: {agents}"
        )
        if args.prs:
            for num, p in sorted(data["prs"].items(), key=lambda kv: int(kv[0])):
                share = "" if p["share"] is None else f" {p['share']:.0%}"
                print(
                    f"    #{num:<6} {p['label']:<9} {p['agent'] or '':<12}{share:>5}  {p['confidence']:<7} "
                    f"{'; '.join(p['evidence'])}"
                )
    return 0


LINES = [
    ("Agent share of merged code", "agent_share_of_code", "pct"),
    ("PRs merged", "merged", "int"),
    ("Merge rate", "merge_rate", "pct"),
    ("Median working hours to first review", "median_hours_to_first_review", "h"),
    ("Median working hours to merge", "median_hours_to_merge", "h"),
    ("Review round trips (median)", "median_review_iterations", "num"),
    ("Commits after first review, per PR", "commits_after_first_review_per_pr", "num"),
    ("Force-pushes per PR", "force_pushes_per_pr", "num"),
    ("Human review coverage", "human_review_coverage", "pct"),
    ("Self-merged without review", "self_merge_rate", "pct"),
    ("Reverted within the window", "revert_rate", "pct"),
    ("Needed a follow-up fix", "follow_up_fix_rate", "pct"),
    ("User-facing changes", "user_facing_share", "pct"),
    ("Issues closed", "issues_closed", "int"),
    ("Features shipped", "features_shipped", "int"),
    ("Passed CI on the first push", "ci_first_pass_rate", "pct"),
    ("Fix-up commits after a CI failure, per PR", "fixups_after_ci_failure_per_pr", "num"),
    ("Coverage on changed lines (median)", "median_coverage_changed", "cov"),
    ("New code-scanning findings per PR", "scan_alerts_per_pr", "num"),
    ("Code still alive after 30+ days", "code_survival", "pct"),
    ("Hours from first commit to production", "median_hours_to_production", "h"),
]


def fmt(value: Any, kind: str) -> str:
    if value is None:
        return "-"
    return {
        "pct": f"{value:.0%}",
        "h": f"{value:.1f}h",
        "cov": f"{value:.1f}%",
        "num": f"{value:.2f}".rstrip("0").rstrip("."),
    }.get(kind, f"{value:,}")


def cmd_metrics(args: argparse.Namespace) -> int:
    store, _, _ = applied_state(args)
    data = store.read(SUMMARY)
    if not data:
        raise SystemExit("No metrics yet: run `agentyield sync` or `agentyield recompute`.")
    scope = args.scope or "org"
    block = data["scopes"].get(scope)
    if block is None:
        raise SystemExit(f"no scope {scope!r}; choose from: {', '.join(data['scopes'])}")
    if args.json:
        print(json.dumps(block, indent=1))
        return 0
    g = block["groups"]
    print(f"{scope}  (built {data['built_at']}; '-' means fewer than {data['min_sample']} PRs to go on)")
    print(f"  {'':<40}{'All':>9}{'Agent':>9}{'Assisted':>10}{'Human':>9}")
    for label, key, kind in LINES:
        cells = "".join(
            f"{fmt(g[grp][key], kind):>{w}}" for grp, w in (("all", 9), ("agent", 9), ("assisted", 10), ("human", 9))
        )
        print(f"  {label:<40}{cells}")
    m = block["matched"]
    if m["metrics"]:
        print(f"Matched on repository and PR size ({m['agent_prs_matched']} of {m['agent_prs']} agent PRs):")
        for key, v in m["metrics"].items():
            kind = "h" if "hours" in key else "pct" if "rate" in key else "num"
            print(f"  {key.replace('_', ' '):<40} agent {fmt(v['agent'], kind):>7}   human {fmt(v['human'], kind):>7}")
    dl = block.get("delivery") or {}
    if dl.get("deployments"):
        print(
            f"Production deployments: {dl['deployments']}, failed {dl['failed']} "
            f"(Failed production deployments: {fmt(dl['failure_rate'], 'pct')})"
        )
    c = block["change"]
    print("Last 4 weeks against the 4 before:")
    for key, v in c.items():
        kind = "pct" if key in ("agent_share_of_code", "merge_rate") else "h" if "hours" in key else "int"
        print(
            f"  {key.replace('_', ' '):<40} {fmt(v['last_4_weeks'], kind):>8}  (was {fmt(v['previous_4_weeks'], kind)})"
        )
    return 0


def parse_every(value: str) -> int:
    m = re.fullmatch(r"(\d+)([smh]?)", value or "")
    if not m:
        raise SystemExit(f"--every takes a number of seconds, or 30m, 2h; not {value!r}")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def cmd_serve(args: argparse.Namespace) -> int:
    store, cfg, repos = applied_state(args)
    dash = dict(cfg.section("outputs").get("dashboard") or {})
    host = args.host or str(dash.get("host") or "127.0.0.1")
    port = args.port or int(dash.get("port") or 8780)
    every = 0 if args.no_sync else parse_every(args.every)
    if every:
        print(f"Syncing every {every // 60} minutes; first sync now.", flush=True)
        cmd_sync(args)
    web.serve(
        web.Dashboard.from_config(store, cfg, repos),
        host,
        port,
        sync=lambda: cmd_sync(args),
        every=every,
        allow_unauthenticated=args.allow_unauthenticated,
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    store, cfg, _ = applied_state(args)
    out = args.out or str(cfg.section("outputs").get("static_site") or "site")
    for path in web.build_site(store, out):
        print(f"wrote {path}")
    print("The static copy shows totals only: no PR list and no titles.")
    return 0


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
        a = r.get("attribution") or {}
        if a:
            lb = a["labels"]
            print(
                f"  {'':<40} agent {lb['agent']}, assisted {lb['assisted']}, human {lb['human']}, "
                f"excluded {lb['excluded']}; attribution coverage {a['coverage']:.0%}"
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
    sub.add_parser("recompute", help="rebuild derived files from stored facts (no GitHub calls)")
    lab = sub.add_parser("label", help="correct who wrote a PR: set, clear, list")
    lsub = lab.add_subparsers(dest="action", required=True)
    ls = lsub.add_parser("set")
    ls.add_argument("pr", help="owner/repo#number")
    ls.add_argument("label", choices=["agent", "assisted", "human", "excluded"])
    ls.add_argument("--agent", help="which agent, for agent or assisted")
    ls.add_argument("--note", help="why; filtered like any stored text")
    lc = lsub.add_parser("clear")
    lc.add_argument("pr", help="owner/repo#number")
    lsub.add_parser("list")
    sv = sub.add_parser("serve", help="the live dashboard; syncs on a timer")
    sv.add_argument("--host", help="address to listen on (default 127.0.0.1)")
    sv.add_argument("--port", type=int, help="port (default 8780)")
    sv.add_argument("--every", default="15m", help="sync interval: 15m (default), 1h, or seconds")
    sv.add_argument("--no-sync", action="store_true", help="serve the store as it is, without syncing")
    sv.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="listen beyond 127.0.0.1 without sign-in (only behind your own access control)",
    )
    bd = sub.add_parser("build", help="write the dashboard as static files (totals only)")
    bd.add_argument("--out", help="folder to write (default outputs.static_site, or ./site)")
    me = sub.add_parser("metrics", help="agent share, speed, review and reliability; agent against human")
    me.add_argument("--scope", help="org (default), team:<name> or repo:<owner/name>")
    me.add_argument("--json", action="store_true")
    at = sub.add_parser("attribution", help="who wrote the PRs: agent, assisted, human")
    at.add_argument("--repo", help="only this owner/name")
    at.add_argument("--prs", action="store_true", help="list every PR with its label and evidence")
    at.add_argument("--json", action="store_true")
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
        commands: dict[str, Any] = {
            "init": cmd_init,
            "sync": cmd_sync,
            "backfill": cmd_backfill,
            "status": cmd_status,
            "recompute": cmd_recompute,
            "label": cmd_label,
            "attribution": cmd_attribution,
            "metrics": cmd_metrics,
            "serve": cmd_serve,
            "build": cmd_build,
        }
        return int(commands[args.cmd](args))
    except cfgmod.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except GitHubError as exc:
        print(exc, file=sys.stderr)
        return 1
    except LeaseHeld as exc:
        print(exc, file=sys.stderr)
        return 2
