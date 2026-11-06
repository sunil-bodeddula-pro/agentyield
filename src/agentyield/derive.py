"""The derived layer: everything computed from stored facts, rebuilt by `agentyield recompute` (and after every
sync and config apply) with no GitHub calls.

    derived/attribution/<owner>/<repo>.json   per PR: label, agent, agent share, confidence, evidence
    corrections.json                          admin corrections, keyed "owner/repo#number"; they always win
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from .attribution import DEFAULT_IGNORED, DEFAULT_THRESHOLD, LABELS, attribute
from .config import Config, RepoSpec
from .guardrails import Filter
from .store import Store

CORRECTIONS = "corrections.json"


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
    return summary
