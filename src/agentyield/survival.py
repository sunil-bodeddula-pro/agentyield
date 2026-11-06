"""Code survival: of the lines a merged PR added, how many are still there today, measured with `git blame` on a
local mirror. Opt in with `metrics.code_survival: true`; it needs `git` and room for a partial clone.

The mirror is a bare, blob-less clone in the local cache folder (never the shared store), so file paths and code
stay on this machine. Only counts are stored: lines added, lines still alive, and when it was measured.

Squash merges and merge commits are measured: a line counts as surviving when `git blame` at the default branch
attributes it to the PR's merge commit or one of the PR's own commits. Rebase merges rewrite commit IDs, so their
lines can't be traced and are reported as unknown.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from .attribution import excluded_lines

MIN_AGE = timedelta(days=30)
REMEASURE = timedelta(days=7)


def _git(args: list[str], cwd: str | None = None, token: str = "") -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:  # passed through git's config environment, so it never shows up in the process list
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraHeader",
            GIT_CONFIG_VALUE_0=f"Authorization: Bearer {token}",
        )
    out = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=600)
    return out.stdout


def available() -> bool:
    return shutil.which("git") is not None


def mirror(cache_dir: str, repo: str, url: str, token: str = "") -> str:
    """A bare, blob-less clone of the repository, created once and fetched on later runs."""
    path = os.path.join(os.path.expanduser(cache_dir), "mirrors", repo.replace("/", "__") + ".git")
    if os.path.isdir(path):
        _git(["fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*"], cwd=path, token=token)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _git(["clone", "--quiet", "--bare", "--filter=blob:none", url, path], token=token)
    return path


def measure(
    mirror_path: str, merge_sha: str, pr_shas: list[str], exclude: list[str], branch: str = "HEAD", token: str = ""
) -> tuple[int, int] | None:
    """(lines added by the PR, lines of them still alive on `branch`), or None if the merge can't be traced."""
    try:
        _git(["cat-file", "-e", f"{merge_sha}^{{commit}}"], cwd=mirror_path)
    except subprocess.CalledProcessError:
        return None
    numstat = _git(["diff", "--numstat", f"{merge_sha}^1", merge_sha], cwd=mirror_path, token=token)
    files, added = [], 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] == "-" or excluded_lines(parts[2], exclude):
            continue
        added += int(parts[0])
        files.append(parts[2])
    if not added:
        return 0, 0
    ours = {merge_sha, *pr_shas}
    alive = 0
    for path in files:
        try:
            blame = _git(["blame", "--line-porcelain", branch, "--", path], cwd=mirror_path, token=token)
        except subprocess.CalledProcessError:
            continue  # the file is gone: none of its lines survive
        for line in blame.splitlines():
            if len(line) > 40 and line[40] == " " and line.split(" ", 1)[0] in ours:
                alive += 1
    return added, min(alive, added)


def due(record: dict[str, Any], now: datetime) -> bool:
    merged = record.get("merged_at")
    if not merged or not record.get("merge_commit_sha"):
        return False
    if now - datetime.fromisoformat(merged.replace("Z", "+00:00")) < MIN_AGE:
        return False
    last = (record.get("survival") or {}).get("measured_at")
    return not last or now - datetime.fromisoformat(last.replace("Z", "+00:00")) >= REMEASURE


def update(record: dict[str, Any], mirror_path: str, exclude: list[str], now: datetime, token: str = "") -> bool:
    result = measure(
        mirror_path, record["merge_commit_sha"], [c["sha"] for c in record.get("commits") or []], exclude, token=token
    )
    merged = datetime.fromisoformat(record["merged_at"].replace("Z", "+00:00"))
    record["survival"] = {
        "measured_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_days": (now - merged).days,
        "added": result[0] if result else None,
        "alive": result[1] if result else None,
    }
    return result is not None
