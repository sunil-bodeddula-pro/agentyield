"""Metrics against numbers worked out by hand for 20 PRs.

Working hours are Mon to Fri, 09:00 to 18:00 UTC. The week of Monday 5 October 2026 to Monday 12 October.
Every expected value below is computed by hand in the comments, not by the code under test.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime
from typing import Any

from helpers import OrgCase  # first: puts src on the path

# isort: split
from agentyield.metrics import Settings, WorkingHours, matched, pr_metrics, summarize


# day of October 2026 -> date; 5 = Mon, 9 = Fri, 12 = Mon
def at(day: int, hh: int, mm: int = 0) -> str:
    return datetime(2026, 10, day, hh, mm, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(
    n: int,
    created: str,
    merged: str | None = None,
    review: str | None = None,
    *,
    lines: int,
    kind: str,
    closed: bool = False,
    approved: bool = True,
    author: str = "dev",
    merged_by: str = "lead",
    files: tuple[str, ...] = (),
    closes: tuple[int, ...] = (),
    reverts: int | None = None,
    after_review: int = 0,
    iterations: int = 0,
    force: int = 0,
) -> dict[str, Any]:
    reviews = (
        [{"at": review, "state": "approved" if approved else "commented", "reviewer": "rev", "human": True}]
        if review
        else []
    )
    return {
        "repo": "acme/lab",
        "team": "core",
        "number": n,
        "created_at": created,
        "ready_at": created,
        "first_review_at": review,
        "merged_at": merged,
        "state": "merged" if merged else ("closed" if closed else "open"),
        "lines": lines,
        "reviews": reviews,
        "author": author,
        "merged_by": merged_by if merged else "",
        "title_type": kind,
        "labels": [],
        "files": list(files or (f"f{n}",)),
        "closes": list(closes),
        "reverts": reverts,
        "commits": [],
        "summary": {
            "commits": 1,
            "commits_after_first_review": after_review,
            "review_iterations": iterations,
            "changes_requested": 0,
            "force_pushes": force,
        },
    }


AGENT = {"label": "agent", "share": 1.0, "agent": "claude-code"}
HUMAN = {"label": "human", "share": 0.0, "agent": None}

FACTS = [
    # agent PRs: hours to review / merge worked out in working hours
    (pr(1, at(5, 10), at(5, 15), at(5, 12), lines=40, kind="feat", closes=(101,)), AGENT),  # 2h / 5h
    (pr(2, at(5, 16), at(6, 12), at(6, 10), lines=30, kind="fix"), AGENT),  # 2+1=3h / 2+3=5h
    (
        pr(3, at(9, 17), at(12, 11), at(12, 10), lines=20, kind="feat", after_review=2, iterations=1, force=1),
        AGENT,
    ),  # 1+1=2h / 1+2=3h
    (pr(4, at(6, 9), at(6, 17), at(6, 9, 30), lines=300, kind="feat"), AGENT),  # 0.5h / 8h
    (pr(5, at(7, 9), lines=50, kind="feat", closed=True), AGENT),  # closed unmerged
    (pr(6, at(7, 10), at(7, 11), lines=8, kind="chore", author="bot-run", merged_by="bot-run"), AGENT),  # 1h, self
    (pr(7, at(8, 9), at(8, 18), at(8, 13), lines=45, kind="fix", files=("shared",)), AGENT),  # 4h / 9h
    (pr(8, at(8, 10), lines=25, kind="fix", closed=True), AGENT),  # closed unmerged
    # human PRs
    (pr(9, at(5, 10), at(6, 10), at(5, 14), lines=35, kind="feat"), HUMAN),  # 4h / 8+1=9h
    (pr(10, at(8, 9), at(8, 10), at(8, 9, 30), lines=300, kind="revert", reverts=4), HUMAN),  # 1h, reverts #4
    (pr(11, at(9, 9), at(9, 12), at(9, 10), lines=12, kind="fix", files=("shared",)), HUMAN),  # 3h, fixes #7's file
    (pr(12, at(5, 9), at(6, 17), at(5, 17), lines=400, kind="feat"), HUMAN),  # 8h / 9+8=17h
    (pr(13, at(6, 9), at(7, 9), at(6, 11), lines=45, kind="fix"), HUMAN),  # 9h
    (pr(14, at(7, 9), at(7, 15), at(7, 10), lines=60, kind="refactor"), HUMAN),  # 6h
    (pr(15, at(8, 9), at(9, 9), at(8, 12), lines=20, kind="feat", closes=(102,)), HUMAN),  # 9h
    (pr(16, at(9, 13), at(12, 10), at(9, 15), lines=30, kind="docs"), HUMAN),  # 5+1=6h
    (pr(20, at(6, 9), lines=10, kind="fix", closed=True), HUMAN),  # closed unmerged
    # assisted PRs (share below the threshold), and an excluded dependency bot
    (
        pr(17, at(5, 10), at(5, 13), at(5, 11), lines=100, kind="feat"),
        {"label": "assisted", "share": 0.25, "agent": "claude-code"},
    ),
    (
        pr(18, at(6, 10), at(6, 16), at(6, 12), lines=40, kind="fix"),
        {"label": "assisted", "share": 0.4, "agent": "claude-code"},
    ),
    (
        pr(19, at(6, 9), at(6, 10), lines=5, kind="chore", author="dependabot[bot]"),
        {"label": "excluded", "share": None, "agent": None},
    ),
]
ISSUES = {
    101: {"number": 101, "state": "closed", "labels": ["feature"]},
    102: {"number": 102, "state": "closed", "labels": ["bug"]},
}


class WorkingHoursTest(unittest.TestCase):
    def test_spans(self) -> None:
        wh = WorkingHours()
        p = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
        self.assertEqual(wh.between(p(at(9, 17)), p(at(12, 10))), 2.0)  # Fri 17-18, Mon 9-10
        self.assertEqual(wh.between(p(at(10, 12)), p(at(11, 12))), 0.0)  # all weekend
        self.assertEqual(wh.between(p(at(5, 7)), p(at(5, 20))), 9.0)  # before and after hours
        london = WorkingHours.from_config({"timezone": "Europe/London", "days": "Mon-Thu", "start": "10:00"})
        self.assertEqual(london.between(p(at(5, 9)), p(at(5, 12))), 3.0)  # BST: 09-12 UTC is 10-13 London
        self.assertEqual(london.between(p(at(9, 9)), p(at(9, 17))), 0.0)  # Friday isn't a working day


class HandCheckedTest(unittest.TestCase):
    def setUp(self) -> None:
        s = Settings.from_config({"min_sample": 5}, {})
        repo = {f["number"]: f for f, _ in FACTS}
        self.rows = [pr_metrics(f, a, repo, ISSUES, [], s) for f, a in FACTS]
        self.by = {r["number"]: r for r in self.rows}
        counted = [r for r in self.rows if r["group"] != "excluded"]
        self.groups = {
            g: summarize([r for r in counted if g == "all" or r["group"] == g], 5)
            for g in ("all", "agent", "assisted", "human")
        }

    def test_per_pr(self) -> None:
        expected = {
            1: (2.0, 5.0),
            2: (3.0, 5.0),
            3: (2.0, 3.0),
            4: (0.5, 8.0),
            6: (None, 1.0),
            7: (4.0, 9.0),
            9: (4.0, 9.0),
            12: (8.0, 17.0),
            16: (2.0, 6.0),
        }
        for n, (review, merge) in expected.items():
            self.assertEqual((self.by[n]["hours_to_first_review"], self.by[n]["hours_to_merge"]), (review, merge), n)
        self.assertEqual([self.by[n]["size"] for n in (6, 1, 5, 4)], ["XS", "S", "M", "L"])
        self.assertTrue(self.by[4]["reverted"])
        self.assertTrue(self.by[7]["follow_up_fix"])
        self.assertFalse(self.by[11]["follow_up_fix"])
        self.assertTrue(self.by[6]["self_merged"])
        self.assertTrue(self.by[1]["feature"])
        self.assertEqual((self.by[15]["issues_closed"], self.by[15]["feature"]), (1, False))
        self.assertEqual(self.by[17]["agent_lines"], 25.0)

    def test_agent_group(self) -> None:
        a = self.groups["agent"]
        self.assertEqual((a["merged"], a["closed_unmerged"], a["merge_rate"]), (6, 2, 0.75))
        self.assertEqual(a["median_hours_to_merge"], 5.0)  # 1,3,5,5,8,9
        self.assertEqual(a["p85_hours_to_merge"], 8.0)  # index round(0.85 * 5) = 4
        self.assertEqual(a["median_hours_to_first_review"], 2.0)  # 0.5,2,2,3,4
        self.assertEqual(a["human_review_coverage"], 0.833)  # 5 of 6
        self.assertEqual(a["self_merge_rate"], 0.167)  # #6
        self.assertEqual(a["revert_rate"], 0.167)  # #4
        self.assertEqual(a["follow_up_fix_rate"], 0.167)  # #7
        self.assertEqual(a["user_facing_share"], 0.833)  # all but the chore
        self.assertEqual(a["commits_after_first_review_per_pr"], 0.333)  # 2 commits over 6 PRs
        self.assertEqual((a["issues_closed"], a["features_shipped"]), (1, 1))

    def test_human_and_all(self) -> None:
        h = self.groups["human"]
        self.assertEqual((h["merged"], h["merge_rate"], h["median_hours_to_merge"]), (8, 0.889, 7.5))
        self.assertEqual((h["human_review_coverage"], h["user_facing_share"], h["revert_rate"]), (1.0, 0.625, 0.0))
        al = self.groups["all"]
        self.assertEqual((al["merged"], al["closed_unmerged"], al["merge_rate"]), (16, 3, 0.842))
        self.assertEqual((al["lines_merged"], al["agent_lines_merged"]), (1485, 484.0))
        self.assertEqual(al["agent_share_of_code"], 0.326)  # 484 / 1485
        self.assertIsNone(self.groups["assisted"]["merge_rate"])  # 2 PRs: below min_sample, not a number

    def test_matched(self) -> None:
        m = matched([r for r in self.rows if r["group"] != "excluded"])
        self.assertEqual((m["agent_prs_matched"], m["agent_prs"]), (7, 8))  # the XS PR has no human peer
        # hours to merge: S 5 vs 9 (weight 5/7), L 8 vs 9 (1/7); M has no merged agent PR
        self.assertEqual(m["metrics"]["median_hours_to_merge"], {"agent": 5.5, "human": 9.0})
        # merge rate: S .8 vs .833, L 1 vs 1, M 0 vs 1, weights 5/7, 1/7, 1/7
        self.assertEqual(m["metrics"]["merge_rate"], {"agent": 0.714, "human": 0.881})


class MetricsFlowTest(OrgCase):
    def test_sync_builds_metrics_and_cli_prints_them(self) -> None:
        self.org.issues[("acme/checkout-service", 12)] = {
            "number": 12,
            "state": "closed",
            "labels": [{"name": "feature"}],
            "title": f"Ship it for {'Project Bluebird'}",
        }
        self.org.pulls["acme/checkout-service"][1]["body"] += "\nCloses #12"
        self.org.releases["acme/checkout-service"] = [{"tag_name": "v1.0", "published_at": "2099-01-01T00:00:00Z"}]
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.store, "v1", "derived", "metrics", "summary.json")) as fh:
            summary = json.load(fh)
        self.assertIn("org", summary["scopes"])
        self.assertIn("team:payments", summary["scopes"])
        org = summary["scopes"]["org"]["groups"]["all"]
        self.assertEqual(org["features_shipped"], 1)
        self.assertEqual(org["released"] >= 1, True)
        self.assertEqual(len(summary["scopes"]["org"]["weekly"]), 12)
        code, out = self.run_cli("metrics")
        self.assertEqual(code, 0, out)
        self.assertIn("Agent share of merged code", out)
        code, out = self.run_cli("metrics", "--scope", "repo:acme/checkout-service", "--json")
        self.assertEqual(json.loads(out)["groups"]["all"]["issues_closed"], 1)
        self.assert_no_planted(self.store_bytes())
        _, out = self.run_cli("metrics", "--scope", "team:nope")
        self.assertIn("no scope", out)
