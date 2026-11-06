from __future__ import annotations

import json
import os
import unittest
from typing import ClassVar

from helpers import CONFIG, OrgCase  # first: puts src on the path

# isort: split
from agentyield.attribution import attribute
from agentyield.facts import DEFAULT_AGENTS, AgentRule


def commit(agent: bool, lines: int | None = None) -> dict:
    c: dict = {"sha": "x", "coauthors": ["Claude"] if agent else [], "human_coauthors": 0, "author_bot": ""}
    if lines is not None:
        c["lines"] = lines
    return c


def pr(author: str = "alice", branch: str = "feature/x", commits: list | None = None, **signals: object) -> dict:
    return {"author": author, "branch": branch, "commits": commits or [], "signals": {"labels": [], **signals}}


class AttributeTest(unittest.TestCase):
    agents: ClassVar[dict[str, AgentRule]] = {
        **DEFAULT_AGENTS,
        "copilot": AgentRule(bot_logins=["copilot-swe"], branch_prefixes=["copilot/"]),
    }

    def label(self, rec: dict, **kw: object) -> dict:
        return attribute(rec, self.agents, **kw)  # type: ignore[arg-type]

    def test_bot_author_is_agent_with_high_confidence(self) -> None:
        a = self.label(pr(author="copilot-swe", commits=[commit(False)]))
        self.assertEqual((a["label"], a["agent"], a["share"], a["confidence"]), ("agent", "copilot", 1.0, "high"))

    def test_share_by_lines(self) -> None:
        a = self.label(pr(branch="claude/x", commits=[commit(True, 30), commit(True, 10), commit(False, 20)]))
        self.assertEqual((a["label"], a["share"], a["method"], a["confidence"]), ("agent", 0.667, "lines", "high"))
        b = self.label(pr(commits=[commit(True, 10), commit(False, 40)]))
        self.assertEqual((b["label"], b["agent"], b["share"]), ("assisted", "claude-code", 0.2))

    def test_share_by_commits_when_lines_are_missing(self) -> None:
        a = self.label(pr(commits=[commit(True), commit(False), commit(False)]))
        self.assertEqual((a["label"], a["share"], a["method"]), ("assisted", 0.333, "commits"))

    def test_agent_branch_without_commit_evidence_is_medium(self) -> None:
        a = self.label(pr(branch="copilot/fix", commits=[commit(False)]))
        self.assertEqual(
            (a["label"], a["agent"], a["confidence"], a["method"]), ("agent", "copilot", "medium", "opened by agent")
        )

    def test_restricted_pr_uses_signals_recorded_at_sync(self) -> None:
        rec = {"author": "alice", "branch": "", "commits": [], "signals": {"branch": "claude-code", "labels": []}}
        a = self.label(rec)
        self.assertEqual((a["label"], a["agent"]), ("agent", "claude-code"))

    def test_human_excluded_threshold_and_correction(self) -> None:
        self.assertEqual(self.label(pr(commits=[commit(False)]))["label"], "human")
        self.assertEqual(self.label(pr(author="dependabot[bot]"))["label"], "excluded")
        mixed = pr(commits=[commit(True, 30), commit(False, 20)])
        self.assertEqual(self.label(mixed, threshold=0.8)["label"], "assisted")
        fixed = self.label(mixed, correction={"label": "human", "note": "pairing session"})
        self.assertEqual((fixed["label"], fixed["confidence"]), ("human", "manual"))


class AttributionFlowTest(OrgCase):
    def attribution(self, repo: str) -> dict:
        path = os.path.join(self.store, "v1", "derived", "attribution", repo + ".json")
        with open(path, encoding="utf-8") as fh:
            return dict(json.load(fh))

    def fact(self, repo: str, n: int) -> dict:
        with open(os.path.join(self.store, "v1", "repos", repo, "prs", f"{n}.json"), encoding="utf-8") as fh:
            return dict(json.load(fh))

    def test_apply_attributes_every_pr(self) -> None:
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        cs = self.attribution("acme/checkout-service")
        self.assertEqual(cs["prs"]["1"]["label"], "agent")
        self.assertEqual((cs["prs"]["1"]["share"], cs["prs"]["1"]["method"]), (0.667, "lines"))
        self.assertEqual([c["lines"] for c in self.fact("acme/checkout-service", 1)["commits"]], [30, 10, 20])
        self.assertEqual(cs["prs"]["2"]["label"], "human")
        self.assertEqual(cs["summary"]["labels"], {"agent": 1, "assisted": 0, "human": 3, "excluded": 0})
        self.assertEqual(cs["summary"]["coverage"], 0.25)
        wd = self.attribution("acme/web-dashboard")["prs"]["1"]
        self.assertEqual((wd["label"], wd["agent"], wd["confidence"]), ("agent", "copilot", "medium"))
        self.assertEqual(self.fact("acme/checkout-service", 2)["commits"][0]["human_coauthors"], 1)
        self.assert_no_planted(self.store_bytes())
        _, out = self.run_cli("attribution", "--prs")
        self.assertIn("coverage 25%", out)
        self.assertIn("#1      agent     claude-code   67%  high", out)
        self.assert_no_planted(out)

    def test_rule_change_recomputes_without_github_calls(self) -> None:
        self.run_cli("config", "apply", "--yes")
        self.write_config(
            CONFIG + "agents:\n  copilot:\n    bot_logins: [copilot-swe]\nmetrics:\n  agent_share_threshold: 0.9\n"
        )
        before = len(self.gh.requests)
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("rules", out)
        self.assertEqual(len(self.gh.requests), before)
        self.assertEqual(self.attribution("acme/web-dashboard")["prs"]["1"]["confidence"], "high")
        self.assertEqual(self.attribution("acme/checkout-service")["prs"]["1"]["label"], "assisted")

    def test_corrections_survive_sync_and_can_be_cleared(self) -> None:
        self.run_cli("config", "apply", "--yes")
        code, out = self.run_cli(
            "label",
            "set",
            "acme/checkout-service#2",
            "agent",
            "--agent",
            "claude-code",
            "--note",
            "agent run outside the repo",
        )
        self.assertEqual(code, 0, out)
        self.org.touch("acme/checkout-service", 2, minutes=4)
        self.run_cli("sync")
        p2 = self.attribution("acme/checkout-service")["prs"]["2"]
        self.assertEqual((p2["label"], p2["confidence"]), ("agent", "manual"))
        _, out = self.run_cli("label", "list")
        self.assertIn("acme/checkout-service#2: agent (claude-code)", out)
        self.run_cli("label", "clear", "acme/checkout-service#2")
        self.assertEqual(self.attribution("acme/checkout-service")["prs"]["2"]["label"], "human")
        code, out = self.run_cli("label", "set", "acme/untracked#1", "agent")
        self.assertIn("isn't tracked", out)

    def test_line_counts_are_fetched_once(self) -> None:
        self.run_cli("config", "apply", "--yes")
        self.org.touch("acme/checkout-service", 1, minutes=6)
        before = [p for p in self.gh.requests if "/commits/" in p]
        self.run_cli("sync")
        after = [p for p in self.gh.requests if "/commits/" in p]
        self.assertEqual(len(before), 3)
        self.assertEqual(after, before)

    def test_recompute_and_status(self) -> None:
        self.run_cli("config", "apply", "--yes")
        before = len(self.gh.requests)
        code, out = self.run_cli("recompute")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.gh.requests), before)
        _, out = self.run_cli("status")
        self.assertIn("attribution coverage 25%", out)
