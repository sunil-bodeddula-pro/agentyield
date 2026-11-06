"""The feedback loop: turn what the numbers show into changes a team can make.

- Suggested agent rules: when reviewers keep flagging one category on agent PRs more than on human PRs, or agent
  PRs keep failing CI on the first push, draft a rule for the agents' instructions file (AGENTS.md, CLAUDE.md,
  .cursorrules, copilot-instructions.md: whatever your agents read). Drafts come from templates, with no model,
  and are suggestions for a person to review: AgentYield never writes to a repository.
- Before-and-after comparisons: for each change you record under `experiments` (a new agent, model or
  instructions file), compare agent work in the weeks before and after, with sample sizes. Other things change
  at the same time, so read these as signals, not proof.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .metrics import summarize, week_of

RULES: dict[str, str] = {
    "correctness": "Before opening a PR, write or update tests that pin down the requirement and its edge cases "
    "(empty input, missing values, limits), and run them.",
    "scope": "Change only what the task asks for. List other improvements you notice in the PR description "
    "instead of making them.",
    "design": "Before adding a helper or abstraction, search the codebase for an existing one and reuse it. "
    "Prefer the simplest structure that works.",
    "error_handling": "Handle failure paths explicitly: no empty catch blocks, timeouts on every network call, and "
    "errors passed to the caller with enough context to debug.",
    "security": "Never build SQL, shell commands or HTML from user input; use parameterised APIs. Never commit "
    "secrets: read them from the environment.",
    "performance": "Don't call the database or the network inside a loop; batch the calls. Don't load a whole table "
    "or file to filter it in memory.",
    "concurrency": "Protect shared state with the project's locking, await every async call, and don't assume the "
    "order events arrive in.",
    "tests": "Every change ships with tests for its new behaviour. Never change an existing assertion to make a "
    "test pass without explaining why in the PR.",
    "docs": "When behaviour changes, update the docstrings, README and changelog in the same PR. Log messages "
    "include the IDs needed to debug.",
    "style": "Run the project's formatter and linter before every commit, and fix what they report.",
    "build_ci": "Run the same build and CI checks locally before opening a PR. Don't add a dependency without "
    "saying why in the PR description.",
    "conventions": "Follow the project's conventions for public APIs, migrations and feature flags; when unsure, "
    "copy the pattern of the nearest existing example.",
}
CI_RULE = "Run the tests and linters locally before pushing; the first push of a PR should pass CI."
STEERING_RULE = (
    "Reviewers keep giving agents the same instructions in comments. Move standing instructions "
    "(how to build, test and format; what not to touch) into the instructions file."
)


def _n(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _in_weeks(p: dict[str, Any], weeks: set[str]) -> bool:
    return p.get("merged_week") in weeks or p.get("created_week") in weeks


def suggestions(
    prs: list[dict[str, Any]], now: datetime, weeks: int = 8, min_comments: int = 3
) -> list[dict[str, Any]]:
    """Rule suggestions for one scope, strongest evidence first."""
    window = {(datetime.fromisoformat(week_of(now)) - timedelta(weeks=i)).date().isoformat() for i in range(weeks)}
    sel = [p for p in prs if _in_weeks(p, window)]
    agent = [p for p in sel if p["group"] in ("agent", "assisted")]
    human = [p for p in sel if p["group"] == "human"]

    def counts(group: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
        total: dict[str, int] = {}
        for p in group:
            for cat, n in (p.get("review_categories") or {}).items():
                total[cat] = total.get(cat, 0) + n
        return total, sum(total.values())

    a_counts, a_total = counts(agent)
    h_counts, h_total = counts(human)
    out: list[dict[str, Any]] = []
    for cat, n in a_counts.items():
        if n < min_comments or cat not in RULES:
            continue
        a_share = n / a_total
        h_share = h_counts.get(cat, 0) / h_total if h_total else None
        if h_share is not None and a_share < max(h_share * 1.5, h_share + 0.05):
            continue  # reviewers flag this as often on human work: not an agent-specific gap
        prs_hit = sum(1 for p in agent if (p.get("review_categories") or {}).get(cat))
        compare = f", against {h_share:.0%} on human PRs" if h_share is not None else ""
        out.append(
            {
                "kind": "review",
                "category": cat,
                "rule": RULES[cat],
                "comments": n,
                "prs": prs_hit,
                "agent_share": round(a_share, 3),
                "human_share": round(h_share, 3) if h_share is not None else None,
                "evidence": f"{_n(n, 'review comment')} on {_n(prs_hit, 'agent PR')} ({a_share:.0%} of reviewers' "
                f"comments on agent work{compare}) in the last {weeks} weeks",
                "score": n * (a_share - (h_share or 0)),
            }
        )

    def ci_rate(group: list[dict[str, Any]]) -> tuple[float | None, int]:
        known = [bool(p["ci_first_pass"]) for p in group if p.get("ci_first_pass") is not None]
        return (sum(known) / len(known) if known else None), len(known)

    (a_ci, ci_n), (h_ci, _) = ci_rate(agent), ci_rate(human)
    if a_ci is not None and ci_n >= min_comments and a_ci < 0.7 and (h_ci is None or a_ci < h_ci - 0.1):
        out.append(
            {
                "kind": "ci",
                "category": "ci_first_push",
                "rule": CI_RULE,
                "prs": ci_n,
                "evidence": f"only {a_ci:.0%} of {_n(ci_n, 'agent PR')} passed CI on the first push"
                + (f", against {h_ci:.0%} of human PRs" if h_ci is not None else ""),
                "score": ci_n * (1 - a_ci),
            }
        )
    steering = sum(p.get("steering_comments", 0) for p in agent)
    if steering >= min_comments and agent and steering / len(agent) >= 0.5:
        out.append(
            {
                "kind": "steering",
                "category": "steering",
                "rule": STEERING_RULE,
                "comments": steering,
                "prs": len(agent),
                "evidence": f"{_n(steering, 'steering comment')} on {_n(len(agent), 'agent PR')}",
                "score": float(steering),
            }
        )
    return sorted(out, key=lambda s: -s["score"])


def as_markdown(scope: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return f"No suggestions for {scope}: nothing reviewers flag stands out on agent work yet.\n"
    lines = [
        f"## Suggested additions to the agents' instructions ({scope})",
        "",
        "Drafts for a person to review; edit them to fit before adding them to AGENTS.md or your agent's "
        "instructions file.",
        "",
    ]
    for s in items:
        lines.append(f"- {s['rule']}")
        lines.append(f"  <!-- why: {s['evidence']} -->")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ experiments
def _scope_rows(prs: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    if scope == "org":
        return prs
    kind, _, name = scope.partition(":")
    key = "repo" if kind == "repo" else "team"
    return [p for p in prs if str(p.get(key, "")).lower() == name.lower()]


KEYS = (
    "merged",
    "merge_rate",
    "median_hours_to_merge",
    "commits_after_first_review_per_pr",
    "revert_rate",
    "follow_up_fix_rate",
    "ci_first_pass_rate",
    "review_comments_per_pr",
    "agent_share_of_code",
)


def experiment(
    prs: list[dict[str, Any]], exp: dict[str, Any], weeks: int, min_sample: int, today: date
) -> dict[str, Any]:
    """Agent work in the `weeks` before the change against the `weeks` after it (or up to today)."""
    when = date.fromisoformat(str(exp["date"]))
    before_from, after_to = when - timedelta(weeks=weeks), min(today, when + timedelta(weeks=weeks))
    rows = [p for p in _scope_rows(prs, str(exp.get("scope") or "org")) if p["group"] != "excluded"]

    def window(lo: date, hi: date) -> list[dict[str, Any]]:
        def day(p: dict[str, Any]) -> str:
            return str(p.get("merged_at") or p.get("closed_at") or p.get("created_at") or "")[:10]

        return [p for p in rows if lo.isoformat() <= day(p) < hi.isoformat()]

    out: dict[str, Any] = {
        "name": exp["name"],
        "date": when.isoformat(),
        "scope": exp.get("scope") or "org",
        "note": exp.get("note", ""),
        "weeks": weeks,
        "complete": after_to >= when + timedelta(weeks=weeks),
    }
    for label, lo, hi in (("before", before_from, when), ("after", when, after_to)):
        sel = window(lo, hi)
        agent = summarize([p for p in sel if p["group"] in ("agent", "assisted")], min_sample)
        allw = summarize(sel, min_sample)
        out[label] = {
            "agent_prs": agent["prs"],
            **{k: agent[k] for k in KEYS if k != "agent_share_of_code"},
            "agent_share_of_code": allw["agent_share_of_code"],
        }
    return out
