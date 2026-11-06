"""The derived layer: everything computed from stored facts, rebuilt by `agentyield recompute` (and after every
sync and config apply) with no GitHub calls.

    derived/attribution/<owner>/<repo>.json   per PR: label, agent, agent share, confidence, evidence
    derived/metrics/prs/<owner>/<repo>.json   per PR: timings, size, back-and-forth, reverts, functionality
    derived/metrics/summary.json              organisation, team and repository rollups, matched, weekly
    corrections.json                          admin corrections, keyed "owner/repo#number"; they always win
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from .attribution import DEFAULT_IGNORED, DEFAULT_THRESHOLD, LABELS, attribute
from .config import Config, RepoSpec
from .cost import allocate, gather
from .feedback import experiment, suggestions
from .guardrails import Filter
from .metrics import Settings, pr_metrics, rollup
from .store import Store

CORRECTIONS = "corrections.json"


def metrics_path(repo: str) -> str:
    return f"derived/metrics/prs/{repo}.json"


SUMMARY = "derived/metrics/summary.json"
COST = "derived/cost.json"


def recompute_with_cost(store: Store, cfg: Config, repos: dict[str, RepoSpec]) -> None:
    """Recompute, then allocate spend (which needs the attribution just computed) and recompute the metrics."""
    recompute(store, cfg, repos)
    base = "" if "://" in cfg.source else os.path.dirname(os.path.abspath(os.path.expanduser(cfg.source)))
    if refresh_cost(store, cfg, repos, base) is not None:
        recompute(store, cfg, repos)


FEEDBACK = "derived/feedback.json"


def write_feedback(store: Store, cfg: Config, rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Suggested agent rules per scope, and before-and-after comparisons for the recorded changes."""
    fb = cfg.section("feedback")
    weeks, min_comments = int(fb.get("weeks") or 8), int(fb.get("min_comments") or 3)
    counted = [r for r in rows if r["group"] != "excluded"]
    scopes: dict[str, list[dict[str, Any]]] = {"org": counted}
    for r in counted:
        scopes.setdefault(f"repo:{r['repo']}", []).append(r)
        if r["team"]:
            scopes.setdefault(f"team:{r['team']}", []).append(r)
    flt = cfg.filter()
    exps = []
    for exp in cfg.data.get("experiments") or []:
        result = experiment(
            rows, exp, int(fb.get("experiment_weeks") or 4), metric_settings(cfg).min_sample, now.date()
        )
        result["name"], result["note"] = flt.clean(result["name"])[0], flt.clean(result["note"])[0]
        exps.append(result)
    data = {
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scopes": {s: suggestions(sel, now, weeks, min_comments) for s, sel in sorted(scopes.items())},
        "experiments": exps,
    }
    store.write(FEEDBACK, data)
    return data


def tracker_issues(store: Store) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for rel in store.files("trackers/"):
        issue = store.read(rel)
        if issue:
            out[issue["key"]] = issue
    return out


def refresh_cost(store: Store, cfg: Config, repos: dict[str, RepoSpec], base_dir: str = "") -> dict[str, Any] | None:
    """Fetch spend for the history in the store, allocate it to agent PRs, and keep only the allocations."""
    cost_cfg = cfg.section("cost")
    if not cost_cfg.get("sources"):
        return None
    rows: list[dict[str, Any]] = []
    for spec in repos.values():
        rows.extend((store.read(metrics_path(spec.name), {}) or {}).get("prs") or [])
    starts = [r["first_commit_at"][:10] for r in rows if r.get("first_commit_at")]
    if not starts:
        return None
    since = datetime.fromisoformat(min(starts)).date()
    result = allocate(gather(cost_cfg, since, datetime.now(UTC).date(), base_dir), rows)
    store.write(COST, result)
    return result


def metric_settings(cfg: Config) -> Settings:
    return Settings.from_config(cfg.section("metrics"), cfg.section("functionality"))


def write_pr_metrics(store: Store, cfg: Config, spec: RepoSpec, attributions: dict[str, Any]) -> None:
    s = metric_settings(cfg)
    base = store.repo_dir(spec.name)
    facts = {int(r["number"]): r for r in (store.read(rel) for rel in store.pr_files(spec.name)) if r}
    issues = {int(i["number"]): i for i in (store.read(rel) for rel in store.files(f"{base}/issues/")) if i}
    releases = (store.read(f"{base}/releases.json", {}) or {}).get("releases") or []
    deployments = (store.read(f"{base}/deployments.json", {}) or {}).get("deployments") or []
    trackers = tracker_issues(store)
    costs = (store.read(COST, {}) or {}).get("per_pr") or {}
    rows = [
        pr_metrics(rec, attributions[str(n)], facts, issues, releases, s, deployments, trackers, costs)
        for n, rec in sorted(facts.items())
        if str(n) in attributions
    ]
    store.write(metrics_path(spec.name), {"repo": spec.name, "config": cfg.hash, "prs": rows})


def write_rollup(store: Store, cfg: Config, now: datetime | None = None) -> dict[str, Any]:
    """Organisation, team and repository rollups over every applied repository (hidden ones excluded)."""
    applied = (store.manifest().get("applied") or {}).get("repos") or {}
    rows: list[dict[str, Any]] = []
    deployments: dict[str, list[dict[str, Any]]] = {}
    teams: dict[str, str] = {}
    for r in applied.values():
        rows.extend((store.read(metrics_path(r["name"]), {}) or {}).get("prs") or [])
        deployments[r["name"]] = (store.read(f"repos/{r['name']}/deployments.json", {}) or {}).get("deployments") or []
        teams[r["name"]] = r.get("team", "")
    s = metric_settings(cfg)
    when = now or datetime.now(UTC)
    data = {
        "config": cfg.hash,
        "built_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_sample": s.min_sample,
        "scopes": rollup(
            rows,
            s.min_sample,
            when,
            deployments=deployments,
            teams=teams,
            tracker_issues=tracker_issues(store),
            threshold=settings(cfg)[0],
        ),
        "cost": {k: v for k, v in (store.read(COST, {}) or {}).items() if k in ("total_usd", "unallocated_usd")},
    }
    store.write(SUMMARY, data)
    write_feedback(store, cfg, rows, when)
    return data


def attribution_path(repo: str) -> str:
    return f"derived/attribution/{repo}.json"


def corrections(store: Store) -> dict[str, dict[str, Any]]:
    return dict(store.read(CORRECTIONS, {}) or {})


def set_correction(
    store: Store, flt: Filter, key: str, label: str, agent: str = "", note: str = "", by: str = ""
) -> dict[str, Any]:
    if label not in LABELS:
        raise SystemExit(f"label must be one of {', '.join(LABELS)}")
    items = corrections(store)
    items[key] = {
        "label": label,
        "agent": agent,
        "note": flt.clean(note)[0],
        "by": by,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    store.write(CORRECTIONS, items)
    return items[key]


def clear_correction(store: Store, key: str) -> bool:
    items = corrections(store)
    if items.pop(key, None) is None:
        return False
    store.write(CORRECTIONS, items)
    return True


def settings(cfg: Config) -> tuple[float, list[str]]:
    m = cfg.section("metrics")
    threshold = float(m.get("agent_share_threshold", DEFAULT_THRESHOLD))
    raw = m.get("ignore_authors")
    ignored = [str(a) for a in raw] if isinstance(raw, list) else DEFAULT_IGNORED
    return threshold, ignored


def recompute(store: Store, cfg: Config, repos: dict[str, RepoSpec]) -> dict[str, dict[str, Any]]:
    """Rebuild attribution for each repository. Returns a summary per repository."""
    agents = cfg.agents()
    threshold, ignored = settings(cfg)
    fixes = corrections(store)
    summary: dict[str, dict[str, Any]] = {}
    for spec in repos.values():
        prs: dict[str, Any] = {}
        for rel in store.pr_files(spec.name):
            rec = store.read(rel)
            if not rec:
                continue
            key = f"{spec.name}#{rec['number']}"
            result = attribute(rec, agents, threshold, ignored, fixes.get(key) or fixes.get(key.lower()))
            prs[str(rec["number"])] = result
        labels = Counter(p["label"] for p in prs.values())
        agents_seen = Counter(p["agent"] for p in prs.values() if p["agent"] and p["label"] in ("agent", "assisted"))
        counted = sum(n for label, n in labels.items() if label != "excluded")
        signalled = sum(n for label, n in labels.items() if label in ("agent", "assisted"))
        info = {
            "prs": len(prs),
            "labels": {label: labels.get(label, 0) for label in LABELS},
            "agents": dict(agents_seen.most_common()),
            "coverage": round(signalled / counted, 3) if counted else 0.0,
            "low_confidence": sum(1 for p in prs.values() if p["confidence"] == "medium"),
        }
        store.write(
            attribution_path(spec.name),
            {
                "repo": spec.name,
                "config": cfg.hash,
                "threshold": threshold,
                "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "summary": info,
                "prs": prs,
            },
        )
        summary[spec.name] = info
        write_pr_metrics(store, cfg, spec, prs)
    write_rollup(store, cfg)
    return summary
