"""Turn one pull request (its detail and its timeline) into the record AgentYield stores.

Only facts are kept: times, counts, logins, the conventional-commit type of each commit, and which agent rules
matched. PR descriptions, commit messages, review text and author emails are read here and dropped. The few text
fields that remain (title, branch, labels) go through the guardrail filter, and are blanked for restricted
repositories or PRs with a restricted label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .guardrails import Filter
from .trackers import extract_keys

CONVENTIONAL = re.compile(r"^(\w+)(?:\([^)]*\))?!?:\s")
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:([\w.-]+/[\w.-]+))?#(\d+)", re.I)
REVERTS = re.compile(r"\breverts\s+(?:([\w.-]+/[\w.-]+))?#(\d+)", re.I)
COAUTHOR = re.compile(r"^\s*co-authored-by:\s*(.*?)\s*<([^>]*)>\s*$", re.I | re.M)
BOT_EMAIL = re.compile(r"(\[bot\]@users\.noreply\.github\.com|^no-?reply@)", re.I)


@dataclass
class AgentRule:
    bot_logins: list[str] = field(default_factory=list)
    trailers: list[str] = field(default_factory=list)
    branch_prefixes: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    body_markers: list[str] = field(default_factory=list)


# Starting points, overridden or extended by `agents:` in the config.
DEFAULT_AGENTS: dict[str, AgentRule] = {
    "claude-code": AgentRule(
        trailers=["Co-Authored-By: Claude"], branch_prefixes=["claude/"], body_markers=["Generated with Claude Code"]
    ),
    "copilot": AgentRule(branch_prefixes=["copilot/"]),
    "codex": AgentRule(branch_prefixes=["codex/"]),
}


def trailer_names(rule: AgentRule) -> list[str]:
    """'Co-Authored-By: Claude' -> 'claude': the co-author name a rule looks for."""
    out = []
    for t in rule.trailers:
        name = t.split(":", 1)[1] if t.lower().startswith("co-authored-by:") else t
        if name.strip():
            out.append(name.strip().lower())
    return out


def is_agent_identity(name: str, email: str, agents: dict[str, AgentRule]) -> bool:
    """A co-author or commit author that is a tool, not a person. Only these names are ever stored."""
    low = name.lower()
    if BOT_EMAIL.search(email) or "[bot]" in low:
        return True
    return any(low.startswith(n) for rule in agents.values() for n in trailer_names(rule))


def commit_identities(ev: dict[str, Any], agents: dict[str, AgentRule]) -> dict[str, Any]:
    """Agent co-authors and bot authors of one commit. People are kept as a count, never by name or email."""
    message = ev.get("message") or ""
    agents_found, humans = [], 0
    for name, email in COAUTHOR.findall(message):
        if is_agent_identity(name, email, agents):
            agents_found.append(name.strip())
        else:
            humans += 1
    author = ev.get("author") or {}
    a_name, a_email = str(author.get("name") or ""), str(author.get("email") or "")
    author_bot = a_name if is_agent_identity(a_name, a_email, agents) else ""
    return {"coauthors": sorted(set(agents_found)), "human_coauthors": humans, "author_bot": author_bot}


def closing_issues(repo: str, body: str) -> list[int]:
    """Issues this PR closes ('Closes #12', 'fixes acme/app#7'), in the same repository only."""
    out = {int(n) for other, n in CLOSES.findall(body or "") if not other or other.lower() == repo.lower()}
    return sorted(out)


def reverted_pr(repo: str, title: str, body: str) -> int | None:
    """The PR this one reverts. GitHub's Revert button writes 'Reverts owner/repo#N' in the description."""
    if not (title or "").lower().startswith("revert"):
        return None
    for other, n in REVERTS.findall(body or ""):
        if not other or other.lower() == repo.lower():
            return int(n)
    return None


def _commit_time(ev: dict[str, Any]) -> str:
    return str((ev.get("committer") or {}).get("date") or (ev.get("author") or {}).get("date") or "")


def _matches_trailer(message: str, prefix: str) -> bool:
    p = prefix.lower()
    return any(line.strip().lower().startswith(p) for line in message.splitlines())


def signals_for(pr: dict[str, Any], commits: list[dict[str, Any]], agents: dict[str, AgentRule]) -> dict[str, Any]:
    """Which agent rules this PR matches. Only agent names are recorded, never the text that matched."""
    login = (pr.get("user") or {}).get("login", "")
    branch = (pr.get("head") or {}).get("ref", "") or ""
    labels = [lb.get("name", "") for lb in pr.get("labels") or []]
    body = (pr.get("body") or "").lower()
    out: dict[str, Any] = {"bot_author": None, "branch": None, "labels": [], "body_markers": [], "trailers": {}}
    for name, rule in agents.items():
        if login in rule.bot_logins and not out["bot_author"]:
            out["bot_author"] = name
        if any(branch.startswith(p) for p in rule.branch_prefixes) and not out["branch"]:
            out["branch"] = name
        if any(lb in rule.labels for lb in labels):
            out["labels"].append(name)
        if any(m.lower() in body for m in rule.body_markers):
            out["body_markers"].append(name)
        hits = sum(1 for c in commits if any(_matches_trailer(c.get("message", ""), t) for t in rule.trailers))
        if hits:
            out["trailers"][name] = hits
    return out


def summarize(
    author: str, commits: list[dict[str, Any]], reviews: list[dict[str, Any]], force_pushes: list[str]
) -> dict[str, Any]:
    """The back-and-forth before merge: commits, commits after the first human review, review round trips."""
    human = sorted((r for r in reviews if r["human"]), key=lambda r: r["at"])
    first_review = human[0]["at"] if human else ""
    times = sorted(c["at"] for c in commits if c["at"])
    after = sum(1 for t in times if first_review and t > first_review)
    # A round trip: a human review that is followed by new commits before the next review.
    iterations = 0
    for i, r in enumerate(human):
        nxt = human[i + 1]["at"] if i + 1 < len(human) else "9999"
        if any(r["at"] < t < nxt for t in times):
            iterations += 1
    return {
        "commits": len(commits),
        "commits_after_first_review": after,
        "review_iterations": iterations,
        "changes_requested": sum(1 for r in human if r["state"] == "changes_requested"),
        "human_reviews": len(human),
        "force_pushes": len(force_pushes),
    }


def build_record(
    repo: str,
    pr: dict[str, Any],
    timeline: list[dict[str, Any]],
    flt: Filter,
    agents: dict[str, AgentRule],
    repo_restricted: bool,
    issue_prefixes: set[str] | None = None,
) -> dict[str, Any]:
    author = (pr.get("user") or {}).get("login", "")
    raw_commits = [ev for ev in timeline if ev.get("event") == "committed"]
    commits = []
    for ev in raw_commits:
        message = ev.get("message") or ""
        m = CONVENTIONAL.match(message.splitlines()[0] if message else "")
        commits.append(
            {
                "sha": ev.get("sha", ""),
                "at": _commit_time(ev),
                "type": m.group(1).lower() if m else "",
                "message": message,
                **commit_identities(ev, agents),
            }
        )
    for c in commits:  # tool names only, but they are stored text, so they are filtered too
        c["coauthors"] = flt.clean_all(c["coauthors"])[0]
        c["author_bot"] = flt.clean(c["author_bot"])[0]
    reviews = []
    for ev in timeline:
        if ev.get("event") == "reviewed" and ev.get("submitted_at"):
            user = ev.get("user") or {}
            reviews.append(
                {
                    "at": ev["submitted_at"],
                    "state": str(ev.get("state", "")).lower(),
                    "reviewer": user.get("login", ""),
                    "human": user.get("type") != "Bot" and user.get("login") != author,
                }
            )
    force_pushes = [str(ev.get("created_at", "")) for ev in timeline if ev.get("event") == "head_ref_force_pushed"]
    ready = [str(ev.get("created_at", "")) for ev in timeline if ev.get("event") == "ready_for_review"]
    signals = signals_for(pr, commits, agents)  # the only use of commit messages and the PR body

    label_names = [lb.get("name", "") for lb in pr.get("labels") or []]
    restricted = repo_restricted or flt.restricted_label(label_names)
    title, t_found = ("", []) if restricted else flt.clean(pr.get("title"))
    branch, b_found = ("", []) if restricted else flt.clean((pr.get("head") or {}).get("ref"))
    labels, l_found = ([], []) if restricted else flt.clean_all(label_names)
    merged_at = pr.get("merged_at")
    state = "merged" if merged_at else str(pr.get("state", ""))
    human_reviews = sorted(r["at"] for r in reviews if r["human"])
    return {
        "repo": repo,
        "number": pr.get("number"),
        "state": state,
        "draft": bool(pr.get("draft")),
        "restricted": restricted,
        "title": title,
        "branch": branch,
        "labels": labels,
        "author": author,
        "author_is_bot": (pr.get("user") or {}).get("type") == "Bot",
        "merged_by": (pr.get("merged_by") or {}).get("login", ""),
        "base": (pr.get("base") or {}).get("ref", ""),
        "head_sha": (pr.get("head") or {}).get("sha", ""),
        "merge_commit_sha": pr.get("merge_commit_sha") if pr.get("merged_at") else None,
        "created_at": pr.get("created_at"),
        "ready_at": ready[0] if ready else pr.get("created_at"),
        "first_review_at": human_reviews[0] if human_reviews else None,
        "merged_at": merged_at,
        "closed_at": pr.get("closed_at"),
        "updated_at": pr.get("updated_at"),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changed_files", 0),
        "commits": [
            {k: c[k] for k in ("sha", "at", "type", "coauthors", "human_coauthors", "author_bot")} for c in commits
        ],
        "reviews": [{k: r[k] for k in ("at", "state", "reviewer", "human")} for r in reviews],
        "force_pushes": force_pushes,
        "signals": signals,
        "summary": summarize(author, commits, reviews, force_pushes),
        "redacted": sorted(set(t_found + b_found + l_found)),
        # read from the description here and kept as numbers only; the description itself is dropped
        "closes": closing_issues(repo, pr.get("body") or ""),
        "issue_keys": extract_keys(
            [pr.get("title") or "", (pr.get("head") or {}).get("ref") or "", pr.get("body") or ""],
            issue_prefixes or set(),
        ),
        "reverts": reverted_pr(repo, pr.get("title") or "", pr.get("body") or ""),
        "title_type": _title_type(pr.get("title") or ""),
    }


def _title_type(title: str) -> str:
    m = CONVENTIONAL.match(title)
    return m.group(1).lower() if m else ""
