"""Quality signals gathered at sync time, stored as facts:

  - CI on every commit of a PR: pass, fail, pending or none, from check runs and commit statuses. Gives
    "passed CI on the first push" and "fix-up commits after a CI failure".
  - Coverage on changed lines, read from a status or check you name (`metrics.coverage`), such as codecov/patch.
  - New code-scanning findings in a PR, by severity. Repositories without code scanning are marked unavailable
    and not asked again for a week.
  - Production deployments and their outcome, for lead time to production and the failed-deployment rate.

Only states, times, counts and percentages are kept; check output text is read for a percentage and dropped.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .github import GitHub, GitHubError

FAIL = {"failure", "timed_out", "action_required", "startup_failure", "error"}
PASS = {"success", "neutral", "skipped"}
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
RETRY_UNAVAILABLE = 7 * 24 * 3600
MAX_COMMITS_CHECKED = 30


def commit_ci(gh: GitHub, repo: str, sha: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """(state, check runs, combined status) for one commit."""
    runs = (gh.get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100}).data or {}).get("check_runs") or []
    status = gh.get(f"/repos/{repo}/commits/{sha}/status").data or {}
    statuses = status.get("statuses") or []
    states = [str(r.get("conclusion") or r.get("status") or "") for r in runs] + [str(s.get("state")) for s in statuses]
    if not states:
        return "none", runs, status
    if any(s in FAIL for s in states):
        return "fail", runs, status
    if all(s in PASS for s in states):
        return "pass", runs, status
    return "pending", runs, status


def coverage_from(runs: list[dict[str, Any]], status: dict[str, Any], rule: dict[str, Any]) -> float | None:
    """A percentage from the named status context or check run, e.g. '87.50% of diff hit'."""
    pattern = re.compile(str(rule.get("pattern") or PERCENT.pattern))
    texts: list[str] = []
    if rule.get("status_context"):
        texts += [
            str(s.get("description") or "")
            for s in status.get("statuses") or []
            if s.get("context") == rule["status_context"]
        ]
    if rule.get("check_name"):
        texts += [
            f"{(r.get('output') or {}).get('title') or ''} {(r.get('output') or {}).get('summary') or ''}"
            for r in runs
            if r.get("name") == rule["check_name"]
        ]
    for text in texts:
        m = pattern.search(text)
        if m:
            return round(float(m.group(1)), 2)
    return None


def add_ci(gh: GitHub, repo: str, record: dict[str, Any], old: dict[str, Any], coverage_rule: dict[str, Any]) -> None:
    """CI state per commit (final states are reused from the stored record), and coverage on the head commit."""
    known = {c["sha"]: c["ci"] for c in old.get("commits") or [] if c.get("ci") in ("pass", "fail", "none")}
    commits = record["commits"]
    checked = commits if len(commits) <= MAX_COMMITS_CHECKED else commits[:10] + commits[-10:]
    head = record.get("head_sha", "")
    for c in checked:
        if c["sha"] in known and c["sha"] != head:
            c["ci"] = known[c["sha"]]
            continue
        try:
            state, runs, status = commit_ci(gh, repo, c["sha"])
        except GitHubError:
            continue
        c["ci"] = state
        if c["sha"] == head and coverage_rule:
            record["coverage_changed"] = coverage_from(runs, status, coverage_rule)
    if coverage_rule and "coverage_changed" not in record and old.get("coverage_changed") is not None:
        record["coverage_changed"] = old["coverage_changed"]


def add_scanning(gh: GitHub, repo: str, record: dict[str, Any], cursor: dict[str, Any]) -> None:
    """New code-scanning findings in this PR, by severity; skips repositories where scanning isn't available."""
    if time.time() - float(cursor.get("code_scanning_unavailable", 0)) < RETRY_UNAVAILABLE:
        return
    try:
        alerts = list(gh.paginate(f"/repos/{repo}/code-scanning/alerts", {"pr": record["number"], "per_page": 100}))
    except GitHubError as exc:
        if exc.status in (403, 404):
            cursor["code_scanning_unavailable"] = time.time()
        return
    by: dict[str, int] = {}
    for a in alerts:
        if a.get("state") not in ("open", None):
            continue
        rule = a.get("rule") or {}
        sev = str(rule.get("security_severity_level") or rule.get("severity") or "unknown")
        by[sev] = by.get(sev, 0) + 1
    record["scan"] = {"alerts": sum(by.values()), "by_severity": by}


def sync_deployments(gh: GitHub, repo: str, environments: list[str], old: dict[str, Any], since: str) -> dict[str, Any]:
    """Production deployments since `since`, with their final state and when they first succeeded."""
    known = {d["id"]: d for d in old.get("deployments") or [] if d.get("state") in ("success", "failure", "error")}
    out: list[dict[str, Any]] = [d for d in old.get("deployments") or [] if d.get("created_at", "") >= since]
    seen = {d["id"] for d in out}
    etags = dict(old.get("etags") or {})
    for env in environments:
        try:
            resp = gh.get(f"/repos/{repo}/deployments", {"environment": env, "per_page": 100}, etags.get(env, ""))
        except GitHubError:
            continue
        if resp.status == 304:  # nothing new for this environment; costs no rate limit
            continue
        etags[env] = resp.etag
        items = resp.data or []
        for d in items:
            if d.get("created_at", "") < since:
                continue
            if d["id"] in known:
                if d["id"] not in seen:
                    out.append(known[d["id"]])
                    seen.add(d["id"])
                continue
            try:
                statuses = gh.get(f"/repos/{repo}/deployments/{d['id']}/statuses", {"per_page": 100}).data or []
            except GitHubError:
                statuses = []
            ordered = sorted(statuses, key=lambda s: s.get("created_at", ""))
            final = next((s for s in reversed(ordered) if s.get("state") in ("success", "failure", "error")), None)
            success = next((s for s in ordered if s.get("state") == "success"), None)
            row = {
                "id": d["id"],
                "environment": env,
                "sha": d.get("sha", ""),
                "created_at": d.get("created_at"),
                "state": final.get("state") if final else (ordered[-1].get("state") if ordered else "pending"),
                "success_at": success.get("created_at") if success else None,
            }
            out = [x for x in out if x["id"] != d["id"]] + [row]
            seen.add(d["id"])
    return {"deployments": sorted(out, key=lambda d: d.get("created_at") or ""), "etags": etags}
