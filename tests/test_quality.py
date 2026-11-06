"""Quality signals: CI on every push, coverage on changed lines, code-scanning findings, production deployments,
and code survival measured with git blame on a real repository built in a temporary folder."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from helpers import CONFIG, OrgCase  # first: puts src on the path

# isort: split
from agentyield import survival
from agentyield.attribution import DEFAULT_EXCLUDED_FILES
from mockgh import ts

QUALITY = (
    CONFIG
    + """metrics:
  min_sample: 1
  coverage:
    status_context: codecov/patch
"""
)
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.org",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.org",
}


def git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(path: str) -> str:
    """A repository whose 'merge' commit adds 10 lines (plus a lock file); a later commit rewrites 4 of them.
    Returns the merge commit's SHA: 10 lines added, 6 still alive."""
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    with open(os.path.join(path, "app.py"), "w") as fh:
        fh.write("base = 1\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    with open(os.path.join(path, "app.py"), "a") as fh:
        fh.writelines(f"line_{i} = {i}\n" for i in range(10))
    with open(os.path.join(path, "package-lock.json"), "w") as fh:
        fh.write("{}\n" * 50)
    git(path, "add", ".")
    git(path, "commit", "-qm", "feat: the PR, squash-merged")
    merge = git(path, "rev-parse", "HEAD")
    with open(os.path.join(path, "app.py")) as fh:
        lines = fh.readlines()
    for i in (1, 2, 3, 4):  # rewrite line_0 .. line_3
        lines[i] = f"line_{i - 1} = 'changed'\n"
    with open(os.path.join(path, "app.py"), "w") as fh:
        fh.writelines(lines)
    git(path, "commit", "-qam", "fix: later change")
    return merge


@unittest.skipUnless(shutil.which("git"), "needs git")
class SurvivalTest(unittest.TestCase):
    def test_blame_counts_surviving_lines(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "repo")
            merge = make_repo(repo)
            self.assertEqual(survival.measure(repo, merge, [], DEFAULT_EXCLUDED_FILES), (10, 6))
            self.assertIsNone(survival.measure(repo, "f" * 40, [], DEFAULT_EXCLUDED_FILES))  # untraceable
            mirror = survival.mirror(os.path.join(d, "cache"), "acme/app", repo)
            self.assertTrue(mirror.endswith("acme__app.git"))
            self.assertEqual(survival.measure(mirror, merge, [], DEFAULT_EXCLUDED_FILES), (10, 6))
            survival.mirror(os.path.join(d, "cache"), "acme/app", repo)  # second run fetches, doesn't re-clone

    def test_due(self) -> None:
        now = datetime.now(UTC)
        rec = {"merged_at": ts(now - timedelta(days=40)), "merge_commit_sha": "a" * 40}
        self.assertTrue(survival.due(rec, now))
        self.assertFalse(survival.due({**rec, "merged_at": ts(now - timedelta(days=10))}, now))
        self.assertFalse(survival.due({**rec, "survival": {"measured_at": ts(now - timedelta(days=2))}}, now))


class QualityFlowTest(OrgCase):
    def fact(self, repo: str, n: int) -> dict:
        with open(os.path.join(self.store, "v1", "repos", repo, "prs", f"{n}.json")) as fh:
            return dict(json.load(fh))

    def metrics(self, repo: str) -> dict:
        with open(os.path.join(self.store, "v1", "derived", "metrics", "prs", repo + ".json")) as fh:
            return {r["number"]: r for r in json.load(fh)["prs"]}

    def test_ci_coverage_scanning_and_deployments(self) -> None:
        self.write_config(QUALITY)
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        pr1 = self.fact("acme/checkout-service", 1)
        self.assertEqual([c["ci"] for c in pr1["commits"]], ["fail", "pass", "pass"])
        self.assertEqual(pr1["coverage_changed"], 87.5)
        self.assertEqual(pr1["scan"], {"alerts": 2, "by_severity": {"high": 1, "warning": 1}})
        self.assertNotIn("scan", self.fact("acme/web-dashboard", 1))  # scanning not enabled there
        m = self.metrics("acme/checkout-service")
        self.assertEqual((m[1]["ci_first_pass"], m[1]["fixups_after_ci_failure"]), (False, 2))
        self.assertEqual((m[2]["ci_first_pass"], m[2]["fixups_after_ci_failure"]), (True, 0))
        self.assertIsNone(m[3]["ci_first_pass"])  # no CI on that commit
        self.assertEqual(m[1]["hours_to_production"], 48.0)  # first commit to the successful deployment
        self.assertEqual(m[2]["hours_to_production"], 24.0)
        with open(os.path.join(self.store, "v1", "derived", "metrics", "summary.json")) as fh:
            summary = json.load(fh)["scopes"]
        self.assertEqual(summary["org"]["delivery"], {"deployments": 2, "failed": 1, "failure_rate": 0.5})
        self.assertEqual(summary["team:web"]["delivery"]["deployments"], 0)
        g = summary["org"]["groups"]["all"]
        self.assertEqual((g["ci_first_pass_rate"], g["median_coverage_changed"]), (0.5, 87.5))
        self.assertEqual(g["scan_alerts_per_pr"], 0.5)  # 2 findings over 4 scanned merged PRs (3 + security-patches)

        before = len(self.gh.requests)
        self.run_cli("sync")
        new = self.gh.requests[before:]
        self.assertFalse(any("web-dashboard/code-scanning" in p for p in new), "unavailable scanning retried")
        _, out = self.run_cli("metrics")
        self.assertIn("Passed CI on the first push", out)
        self.assertIn("Failed production deployments", out)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_code_survival_through_sync(self) -> None:
        base = os.path.join(self.dir, "repos")
        merge = make_repo(os.path.join(self.dir, "work"))
        os.makedirs(os.path.join(base, "acme"))
        git(
            self.dir,
            "clone",
            "-q",
            "--bare",
            os.path.join(self.dir, "work"),
            os.path.join(base, "acme", "checkout-service.git"),
        )
        old = self.org.now - timedelta(days=40)
        n = self.org.add_pr(
            "acme/checkout-service",
            title="feat: old work",
            created=old,
            merged=old + timedelta(hours=2),
            events=[self.org.commit(old + timedelta(hours=1), "feat: old work", "1" * 40)],
        )
        self.org.pulls["acme/checkout-service"][n]["merge_commit_sha"] = merge
        os.environ["AGENTYIELD_GIT_BASE"] = "file://" + base
        self.addCleanup(lambda: os.environ.pop("AGENTYIELD_GIT_BASE", None))
        self.write_config(QUALITY + f"  code_survival: true\ncache_dir: {self.dir}/cache\n")
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        surv = self.fact("acme/checkout-service", n)["survival"]
        self.assertEqual((surv["added"], surv["alive"], surv["age_days"]), (10, 6, 39))  # merged 40 days less 2h ago
        self.assertEqual(self.metrics("acme/checkout-service")[n]["survival_alive"], 6)
        self.assertNotIn(b"app.py", self.store_bytes())  # paths stay in the local mirror, never in the store
