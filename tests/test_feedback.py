"""The feedback loop and digests: rule suggestions, before-and-after comparisons, the weekly digest by webhook and
email, and its schedule. Checks the numbers on hand-built rows, and that nothing private reaches a digest."""

from __future__ import annotations

import json
import os
import threading
import unittest
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock

from helpers import CONFIG, PLANTED, OrgCase  # first: puts src on the path

# isort: split
from agentyield import digest, web
from agentyield.cli import _applied_config, send_digests
from agentyield.config import load
from agentyield.feedback import CI_RULE, RULES, STEERING_RULE, as_markdown, experiment, suggestions
from agentyield.ops import applied_repos
from agentyield.store import Store

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)  # a Wednesday; its week starts 2026-11-02


def row(group: str, cats: dict[str, int] | None = None, ci: bool | None = None, steering: int = 0, **kw: Any) -> Any:
    return {
        "group": group,
        "created_week": "2026-10-26",
        "merged_week": "2026-10-26",
        "review_categories": cats or {},
        "ci_first_pass": ci,
        "steering_comments": steering,
        **kw,
    }


class SuggestionsTest(unittest.TestCase):
    def test_flags_what_reviewers_raise_more_on_agent_work(self) -> None:
        prs = [
            row("agent", {"tests": 4, "style": 1}, ci=False),
            row("assisted", {"tests": 2, "style": 1}, ci=False),
            row("agent", {"correctness": 2}, ci=False),
            row("human", {"style": 2, "tests": 1, "correctness": 3}, ci=True),
            row("human", {"style": 2}, ci=True),
            row("agent", {"tests": 9}, created_week="2026-08-03", merged_week="2026-08-03"),  # outside 8 weeks
        ]
        out = suggestions(prs, NOW, weeks=8, min_comments=3)
        by_cat = {s["category"]: s for s in out}
        # agent comments: tests 6, style 2, correctness 2 (10); human: style 4, tests 1, correctness 3 (8)
        self.assertEqual(set(by_cat), {"tests", "ci_first_push"})  # style is below 3; correctness is below 3
        t = by_cat["tests"]
        self.assertEqual((t["comments"], t["prs"], t["agent_share"], t["human_share"]), (6, 2, 0.6, 0.125))
        self.assertEqual(t["rule"], RULES["tests"])
        self.assertIn("6 review comments on 2 agent PRs (60%", t["evidence"])
        self.assertEqual(by_cat["ci_first_push"]["rule"], CI_RULE)
        self.assertIn("only 0% of 3 agent PRs", by_cat["ci_first_push"]["evidence"])
        # strongest evidence first: CI scores 3 PRs x 100% failing = 3.0; tests 6 x (0.6 - 0.125) = 2.85
        self.assertEqual([s["category"] for s in out], ["ci_first_push", "tests"])

    def test_same_rate_on_human_work_is_not_an_agent_gap(self) -> None:
        prs = [
            row("agent", {"style": 3}),
            row("agent", {"docs": 3}),
            row("human", {"style": 3}),
            row("human", {"docs": 3}),
        ]
        self.assertEqual(suggestions(prs, NOW, min_comments=3), [])

    def test_steering_and_markdown(self) -> None:
        prs = [row("agent", steering=2), row("agent", steering=1)]
        out = suggestions(prs, NOW, min_comments=3)
        self.assertEqual([s["rule"] for s in out], [STEERING_RULE])
        md = as_markdown("team:payments", out)
        self.assertIn("## Suggested additions to the agents' instructions (team:payments)", md)
        self.assertIn(f"- {STEERING_RULE}", md)
        self.assertIn("No suggestions for org", as_markdown("org", []))


class Row(dict[str, Any]):
    """A metrics row with only the fields a test cares about; every other count reads as 0."""

    def __missing__(self, key: str) -> int:
        return 0


class ExperimentTest(unittest.TestCase):
    def test_before_and_after_windows(self) -> None:
        def pr(day: str, group: str, hours: float, merged: bool = True, repo: str = "acme/a") -> dict[str, Any]:
            return Row(
                {
                    "repo": repo,
                    "team": "",
                    "group": group,
                    "created_at": day + "T09:00:00Z",
                    "merged_at": day + "T12:00:00Z" if merged else None,
                    "closed_at": None,
                    "merged": merged,
                    "closed_unmerged": False,
                    "hours_to_merge": hours if merged else None,
                    "lines": 100,
                    "agent_lines": 100 if group == "agent" else 0,
                }
            )

        prs = [
            pr("2026-09-10", "agent", 8.0),  # before (4 weeks before 2026-10-01 is 2026-09-03)
            pr("2026-09-20", "agent", 6.0),
            pr("2026-09-25", "human", 2.0),
            pr("2026-08-01", "agent", 99.0),  # too early
            pr("2026-10-05", "agent", 2.0),  # after
            pr("2026-10-12", "agent", 3.0, merged=False),
            pr("2026-10-14", "agent", 1.0, repo="acme/other"),  # other scope
        ]
        exp = {"name": "Tests-first rule", "date": "2026-10-01", "scope": "repo:acme/a", "note": "n"}
        out = experiment(prs, exp, weeks=4, min_sample=1, today=date(2026, 10, 20))
        self.assertFalse(out["complete"])  # 2026-10-20 is under 4 weeks after
        self.assertEqual((out["before"]["agent_prs"], out["before"]["merged"]), (2, 2))
        self.assertEqual(out["before"]["median_hours_to_merge"], 7.0)
        self.assertEqual((out["after"]["agent_prs"], out["after"]["merged"]), (2, 1))
        self.assertEqual(out["after"]["merge_rate"], 1.0)  # the open PR is not decided yet: merge rate skips it
        self.assertEqual(out["after"]["median_hours_to_merge"], 2.0)
        self.assertTrue(experiment(prs, exp, 4, 1, date(2026, 11, 30))["complete"])


class DueTest(unittest.TestCase):
    def test_weekly_slot(self) -> None:
        mon_0830 = datetime(2026, 11, 2, 8, 30)
        mon_0915 = datetime(2026, 11, 2, 9, 15)
        self.assertTrue(digest.due("Mon 09:00", mon_0830, None))  # never sent: send now
        self.assertFalse(digest.due("Mon 09:00", mon_0830, "2026-10-26T09:01:00"))  # last week's went; not 9 yet
        self.assertTrue(digest.due("Mon 09:00", mon_0915, "2026-10-26T09:01:00"))
        self.assertFalse(digest.due("Mon 09:00", mon_0915, "2026-11-02T09:00:30"))  # already sent this slot
        self.assertFalse(digest.due("monday 09:00", datetime(2026, 11, 6, 17), "2026-11-02T09:05:00"))  # Friday
        self.assertTrue(digest.due("Fri 17:00", datetime(2026, 11, 6, 17), "2026-11-02T09:05:00"))


class Hook:
    """A chat webhook that records what it receives."""

    def __init__(self) -> None:
        self.posts: list[Any] = []
        hook = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                hook.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hook"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class DigestFlowTest(OrgCase):
    def setUp(self) -> None:
        super().setUp()
        self.hook = Hook()
        self.addCleanup(self.hook.close)
        os.environ.update(AY_HOOK=self.hook.url, AY_SMTP_USER="u", AY_SMTP_PASS="p")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("AY_HOOK", "AY_SMTP_USER", "AY_SMTP_PASS")])
        day = self.org.now.date()
        self.write_config(
            CONFIG
            + f"""metrics:
  min_sample: 1
feedback:
  min_comments: 1
experiments:
  - name: AGENTS.md for {PLANTED["deny_term"]}
    date: '{(day - timedelta(days=3)).isoformat()}'
    note: tests first, ask {PLANTED["email"]}
  - name: secret repo change
    date: '{day.isoformat()}'
    scope: repo:acme/security-patches
outputs:
  digest:
    scopes: [org, team:payments]
    webhook_env: AY_HOOK
    email:
      host: smtp.acme.example
      port: 2525
      from: agentyield@acme.example
      to: [leads@acme.example]
      username_env: AY_SMTP_USER
      password_env: AY_SMTP_PASS
    schedule: "Mon 09:00"
"""
        )
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)

    def test_suggest_and_experiments_commands(self) -> None:
        code, out = self.run_cli("suggest")
        self.assertEqual(code, 0, out)
        # checkout-service#1 (agent): a correctness, a security and a tests comment, CI failed first, one steering
        for rule in (RULES["correctness"], RULES["security"], RULES["tests"], CI_RULE, STEERING_RULE):
            self.assertIn(rule, out)
        _, js = self.run_cli("suggest", "--scope", "team:web", "--json")
        self.assertEqual(json.loads(js), [])  # web-dashboard's agent PR had no review comments
        code, out = self.run_cli("experiments")
        self.assertEqual(code, 0, out)
        self.assertIn("AGENTS.md for [REDACTED:term]", out)
        self.assertIn("still filling in", out)
        with open(os.path.join(self.store, "v1", "derived", "feedback.json"), "rb") as fh:
            self.assert_no_planted(fh.read())

    def test_dashboard_hides_experiments_on_repositories_the_viewer_cannot_read(self) -> None:
        current = load(self.config)
        store = Store(current.store_uri)
        dash = web.Dashboard.from_config(store, _applied_config(store, current), applied_repos(store.manifest()))
        everyone = dash.summary(web.Viewer("lead", False, None), "org")["feedback"]
        some = dash.summary(web.Viewer("dev", False, {"acme/checkout-service"}), "org")["feedback"]
        self.assertEqual(len(everyone["experiments"]), 2)
        self.assertEqual([e["scope"] for e in some["experiments"]], ["org"])
        self.assertTrue(some["suggestions"])

    def test_digest_by_webhook_and_email_on_schedule(self) -> None:
        sent: list[Any] = []

        class FakeSMTP:
            def __init__(self, host: str, port: int, timeout: int) -> None:
                sent.append(("connect", host, port))

            def __enter__(self) -> FakeSMTP:
                return self

            def __exit__(self, *a: Any) -> None:
                pass

            def starttls(self) -> None:
                sent.append(("starttls",))

            def login(self, user: str, password: str) -> None:
                sent.append(("login", user, password))

            def send_message(self, msg: Any) -> None:
                sent.append(("send", msg["To"], msg["Subject"], msg.get_content()))

        current = load(self.config)
        store = Store(current.store_uri)
        monday = datetime(2026, 11, 9, 9, 30, tzinfo=UTC)
        with mock.patch("smtplib.SMTP", FakeSMTP):
            done = send_digests(store, current, now=monday)
            self.assertEqual(
                done, ["org by webhook", "org by email", "team:payments by webhook", "team:payments by email"]
            )
            self.assertEqual(send_digests(store, current, now=monday + timedelta(hours=2)), [])  # once a week
            self.assertEqual(len(send_digests(store, current, now=monday + timedelta(days=7))), 4)
        self.assertEqual(len(self.hook.posts), 4)
        org_text = self.hook.posts[0]["text"]
        self.assertIn("AgentYield weekly digest for the organisation", org_text)
        self.assertIn("- PRs merged: 5", org_text)
        self.assertIn("Suggested rule for the agents' instructions:", org_text)
        self.assertIn("team payments", self.hook.posts[1]["text"])
        self.assertIn(("connect", "smtp.acme.example", 2525), sent)
        self.assertIn(("login", "u", "p"), sent)
        mails = [s for s in sent if s[0] == "send"]
        self.assertEqual(mails[0][1:3], ("leads@acme.example", "AgentYield weekly digest: the organisation"))
        blob = json.dumps(self.hook.posts) + json.dumps(mails)
        self.assert_no_planted(blob)
        for title in ("idempotency keys", "dark mode", "chart overflow", "ancient history"):  # no PR titles
            self.assertNotIn(title.lower(), blob.lower())

    def test_digest_command_prints_without_sending(self) -> None:
        code, out = self.run_cli("digest", "--scope", "team:web")
        self.assertEqual(code, 0, out)
        self.assertIn("AgentYield weekly digest for team web", out)
        self.assertEqual(self.hook.posts, [])
        _, out = self.run_cli("digest", "--scope", "team:nobody")
        self.assertIn("no data yet for team nobody", out)


if __name__ == "__main__":
    unittest.main()
