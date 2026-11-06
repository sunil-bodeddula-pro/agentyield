"""Who wrote each pull request: an agent, a person with an agent's help, or a person.

    agent     an agent opened the PR or wrote at least `agent_share_threshold` of its changed lines
    assisted  a person's PR where some commits carry an agent co-author
    human     no agent signal at all
    excluded  dependency bots and other authors in `metrics.ignore_authors`; left out of every metric

Attribution is derived from stored facts only, so changing agent rules or the threshold and running
`agentyield recompute` needs no GitHub calls. An admin's correction (`agentyield label set`) always wins and
survives re-syncs.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from typing import Any

from .facts import AgentRule, trailer_names

DEFAULT_THRESHOLD = 0.5
DEFAULT_IGNORED = ["dependabot[bot]", "renovate[bot]", "github-actions[bot]"]
DEFAULT_EXCLUDED_FILES = [
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.sum",
    "poetry.lock",
    "Cargo.lock",
    "vendor/*",
    "*/vendor/*",
    "node_modules/*",
    "*.min.js",
    "*.min.css",
    "dist/*",
    "*/generated/*",
]
LABELS = ("agent", "assisted", "human", "excluded")


def commit_agent(commit: dict[str, Any], agents: dict[str, AgentRule]) -> str | None:
    """The agent behind one commit: a matching co-author trailer, or a matching bot author."""
    names = [n.lower() for n in commit.get("coauthors") or []]
    bot = str(commit.get("author_bot") or "")
    for agent, rule in agents.items():
        prefixes = trailer_names(rule)
        if any(n.startswith(p) for n in names for p in prefixes):
            return agent
        if bot and (bot in rule.bot_logins or any(bot.lower().startswith(b.lower()) for b in rule.bot_logins)):
            return agent
    return None


def pr_signal_agent(rec: dict[str, Any], agents: dict[str, AgentRule]) -> tuple[str | None, str]:
    """An agent that opened the PR, and the strongest evidence for it."""
    author = rec.get("author", "")
    for agent, rule in agents.items():
        if author and author in rule.bot_logins:
            return agent, "bot author"
    signals = rec.get("signals") or {}
    branch = rec.get("branch") or ""
    for agent, rule in agents.items():
        if branch and any(branch.startswith(p) for p in rule.branch_prefixes):
            return agent, "branch"
    # Restricted PRs keep no branch or labels; the matches recorded at sync time stand in for them.
    if signals.get("branch") in agents:
        return str(signals["branch"]), "branch"
    for key, why in (("labels", "label"), ("body_markers", "PR description marker")):
        for agent in signals.get(key) or []:
            if agent in agents:
                return str(agent), why
    return None, ""


def excluded_lines(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], p) for p in patterns)


def attribute(
    rec: dict[str, Any],
    agents: dict[str, AgentRule],
    threshold: float = DEFAULT_THRESHOLD,
    ignored: list[str] | None = None,
    correction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if correction:
        return {
            "label": correction["label"],
            "agent": correction.get("agent") or None,
            "share": 1.0 if correction["label"] == "agent" else None,
            "confidence": "manual",
            "method": "correction",
            "evidence": [f"set by an admin: {correction.get('note') or 'no note'}"],
        }
    if rec.get("author") in (ignored if ignored is not None else DEFAULT_IGNORED):
        return {
            "label": "excluded",
            "agent": None,
            "share": None,
            "confidence": "high",
            "method": "ignored author",
            "evidence": [f"author {rec.get('author')} is in ignore_authors"],
        }
    commits = rec.get("commits") or []
    per_commit = [commit_agent(c, agents) for c in commits]
    agent_commits = [a for a in per_commit if a]
    opener, why = pr_signal_agent(rec, agents)
    evidence = [f"{why}: {opener}"] if opener else []
    share: float | None
    if opener and why == "bot author":
        share, method = 1.0, "bot author"
    elif agent_commits:
        lines = [c.get("lines") for c in commits]
        if all(isinstance(n, int) for n in lines) and sum(lines) > 0:
            total = sum(int(n or 0) for n in lines)
            share = sum(int(n or 0) for n, a in zip(lines, per_commit, strict=True) if a) / total
            method = "lines"
        else:
            share, method = len(agent_commits) / len(commits), "commits"
        evidence.append(
            f"{len(agent_commits)} of {len(commits)} commits by {Counter(agent_commits).most_common(1)[0][0]}"
        )
    elif opener:
        share, method = 1.0, "opened by agent"  # agent branch or marker, no per-commit evidence either way
    else:
        return {
            "label": "human",
            "agent": None,
            "share": 0.0,
            "confidence": "none",
            "method": "no signal",
            "evidence": [],
        }
    agent = opener or Counter(agent_commits).most_common(1)[0][0]
    label = "agent" if share >= threshold or (opener and why == "bot author") else "assisted"
    if opener and method in ("opened by agent", "bot author"):
        label = "agent"
    strong = why == "bot author" or bool(agent_commits)
    return {
        "label": label,
        "agent": agent,
        "share": round(share, 3),
        "confidence": "high" if strong else "medium",
        "method": method,
        "evidence": evidence,
    }


def needs_line_counts(rec: dict[str, Any], agents: dict[str, AgentRule]) -> bool:
    """Only PRs with both agent and non-agent commits need per-commit line counts (one API call per commit)."""
    commits = rec.get("commits") or []
    found = {commit_agent(c, agents) is not None for c in commits}
    return found == {True, False} and not all(isinstance(c.get("lines"), int) for c in commits)
