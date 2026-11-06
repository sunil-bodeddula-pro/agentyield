"""Jira and Linear features, and agent spend from a file and from the Claude Code analytics API, against fake
servers. Checks agent share and lead time per feature, the cost allocation, and that emails and per-person spend
never reach the store."""

from __future__ import annotations

import base64
import json
import os
import threading
import unittest
import urllib.parse
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from helpers import CONFIG, PLANTED, OrgCase  # first: puts src on the path

# isort: split
from agentyield.cost import allocate
from agentyield.trackers import extract_keys
from mockgh import ts


class FakeTrackers:
    """Jira (REST), Linear (GraphQL) and the Claude Code analytics endpoint, in one local server."""

    def __init__(self, jira: dict[str, Any], linear: dict[str, Any], analytics: dict[str, list[Any]]):
        self.calls: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _send(self, code: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                fake.calls.append(self.path)
                url = urllib.parse.urlparse(self.path)
                if url.path.startswith("/rest/api/3/issue/"):
                    expected = "Basic " + base64.b64encode(b"me@acme.example:jira-token").decode()
                    if self.headers.get("Authorization") != expected:
                        return self._send(401, {})
                    key = url.path.rsplit("/", 1)[1]
                    return self._send(200, jira[key]) if key in jira else self._send(404, {})
                if url.path == "/v1/organizations/usage_report/claude_code":
                    if self.headers.get("x-api-key") != "admin-key":
                        return self._send(401, {})
                    day = urllib.parse.parse_qs(url.query)["starting_at"][0]
                    return self._send(200, {"data": analytics.get(day, []), "has_more": False, "next_page": None})
                self._send(404, {})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append("POST " + self.path)
                if self.headers.get("Authorization") != "lin-key":
                    return self._send(401, {})
                key = body["variables"]["id"]
                self._send(200, {"data": {"issue": linear.get(key)}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def jira_issue(
    key: str,
    kind: str,
    done: bool,
    parent: str = "",
    parent_kind: str = "Epic",
    title: str = "",
    resolved: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": title or key,
        "issuetype": {"name": kind},
        "status": {"statusCategory": {"key": "done" if done else "indeterminate"}},
        "labels": labels or [],
        "created": "2026-09-01T09:00:00.000+0000",
        "resolutiondate": resolved,
    }
    if parent:
        fields["parent"] = {"key": parent, "fields": {"issuetype": {"name": parent_kind}}}
    return {"key": key, "fields": fields}


class KeysTest(unittest.TestCase):
    def test_only_configured_prefixes(self) -> None:
        texts = ["feat: CHK-12 checkout", "claude/CHK-7-idem", "UTF-8 and ISO-8601 aren't issues", "see ENG-3"]
        self.assertEqual(extract_keys(texts, {"CHK"}), ["CHK-7", "CHK-12"])
        self.assertEqual(extract_keys(texts, {"CHK", "ENG"}), ["CHK-7", "CHK-12", "ENG-3"])
        self.assertEqual(extract_keys(texts, set()), [])


class AllocateTest(unittest.TestCase):
    def test_shares_person_days_across_active_agent_prs(self) -> None:
        prs = [
            {
                "repo": "r",
                "number": 1,
                "group": "agent",
                "author": "al",
                "commit_days": ["2026-10-01"],
                "created_at": "2026-10-01",
                "closed_at": None,
                "merged_at": None,
            },
            {
                "repo": "r",
                "number": 2,
                "group": "assisted",
                "author": "al",
                "commit_days": ["2026-10-01"],
                "created_at": "2026-10-01",
                "closed_at": None,
                "merged_at": None,
            },
            {
                "repo": "r",
                "number": 3,
                "group": "human",
                "author": "al",
                "commit_days": ["2026-10-01"],
                "created_at": "2026-10-01",
                "closed_at": None,
                "merged_at": None,
            },
        ]
        rows = [
            {"date": "2026-10-01", "login": "al", "amount_usd": 4.0},
            {"date": "2026-10-01", "login": "zed", "amount_usd": 1.5},
        ]
        result = allocate(rows, prs)
        self.assertEqual(result["per_pr"], {"r#1": 2.0, "r#2": 2.0})  # human PRs get no agent spend
        self.assertEqual((result["unallocated_usd"], result["total_usd"]), (1.5, 5.5))


class FeatureFlowTest(OrgCase):
    def setUp(self) -> None:
        super().setUp()
        d = self.org.now - timedelta(days=10)
        h = timedelta(hours=1)
        self.d, self.h = d, h
        cs, wd = "acme/checkout-service", "acme/web-dashboard"
        self.org.pulls[cs][1]["head"]["ref"] = "claude/CHK-12-idem"
        self.org.pulls[cs][2]["body"] = "Part of CHK-13"
        self.org.pulls[wd][1]["title"] = "feat: dark mode ENG-7"
        jira = {
            "CHK-12": jira_issue("CHK-12", "Story", True, "CHK-1"),
            "CHK-13": jira_issue("CHK-13", "Bug", True, "CHK-1"),
            "CHK-1": jira_issue(
                "CHK-1", "Epic", True, title=f"Checkout for {PLANTED['deny_term']}", resolved=ts(d + 48 * h)
            ),
        }
        linear = {
            "ENG-7": {
                "identifier": "ENG-7",
                "title": "Dark mode toggle",
                "createdAt": ts(d),
                "completedAt": ts(d + 4 * h),
                "state": {"type": "completed"},
                "labels": {"nodes": []},
                "parent": None,
                "project": {"id": "p1", "name": "Dark mode"},
            }
        }
        day1 = (d + h).date().isoformat()
        analytics = {
            day1: [
                {
                    "actor": {"type": "user_actor", "email_address": "alice@acme.example"},
                    "model_breakdown": [{"model": "m", "estimated_cost": {"currency": "USD", "amount": 150}}],
                }
            ]
        }
        self.trk = FakeTrackers(jira, linear, analytics)
        self.addCleanup(self.trk.close)
        with open(os.path.join(self.dir, "spend.csv"), "w") as fh:
            fh.write("date,person,amount_usd,tool\n")
            fh.write(f"{day1},alice,3.00,cursor\n")
            fh.write(f"{day1},carol,1.00,cursor\n")  # carol only has human PRs: unallocated
            fh.write(f"{day1},dev@acme.example,2.00,copilot\n")
        os.environ.update(
            JIRA_EMAIL="me@acme.example", JIRA_API_TOKEN="jira-token", LINEAR_API_KEY="lin-key", AY_ADMIN="admin-key"
        )
        self.addCleanup(
            lambda: [os.environ.pop(k, None) for k in ("JIRA_EMAIL", "JIRA_API_TOKEN", "LINEAR_API_KEY", "AY_ADMIN")]
        )
        self.write_config(
            CONFIG
            + f"""functionality:
  jira:
    base_url: {self.trk.url}
    projects: [CHK]
  linear:
    teams: [ENG]
    api_url: {self.trk.url}/graphql
cost:
  sources:
    - type: file
      path: spend.csv
    - type: claude_code
      admin_key_env: AY_ADMIN
      api_url: {self.trk.url}
  people:
    - person: dev@acme.example
      login: copilot-swe
    - person: alice@acme.example
      login: alice
metrics:
  min_sample: 1
"""
        )

    def summary(self) -> dict[str, Any]:
        with open(os.path.join(self.store, "v1", "derived", "metrics", "summary.json")) as fh:
            return dict(json.load(fh))

    def test_features_cost_and_privacy(self) -> None:
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        scopes = self.summary()["scopes"]
        feats = {f["feature"]: f for f in scopes["org"]["features"]["list"]}
        epic = feats["CHK-1"]
        # agent PR 1 (50 lines at a stored 66.7% agent = 33.4) and human PR 2 (50 lines): 33.4 of 100 -> human-led
        self.assertEqual((epic["prs"], epic["merged_prs"], epic["agent_share"], epic["led_by"]), (2, 2, 0.334, "human"))
        self.assertTrue(epic["done"])
        self.assertEqual(epic["lead_time_days"], 2.0)  # first commit d+1h to the epic's resolution at d+48h
        self.assertIn("[REDACTED:term]", epic["title"])
        proj = feats["project:p1"]
        self.assertEqual((proj["led_by"], proj["done"], proj["title"]), ("agent", True, "Dark mode"))
        self.assertEqual(proj["lead_time_days"], 0.1)  # d+1h to completion at d+4h
        f = scopes["org"]["features"]
        self.assertEqual((f["features"], f["done"], f["agent_led_done"], f["human_led_done"]), (2, 2, 1, 1))

        with open(os.path.join(self.store, "v1", "derived", "cost.json")) as fh:
            cost = json.load(fh)
        self.assertEqual(cost["per_pr"], {"acme/checkout-service#1": 4.5, "acme/web-dashboard#1": 2.0})
        self.assertEqual((cost["unallocated_usd"], cost["total_usd"]), (1.0, 7.5))
        g = scopes["org"]["groups"]["agent"]
        self.assertEqual((g["agent_spend_usd"], g["spend_per_merged_pr"]), (6.5, 3.25))
        self.assertEqual(feats["project:p1"]["spend_usd"], 2.0)

        blob = self.store_bytes()
        for value in (b"alice@acme.example", b"dev@acme.example", b"me@acme.example", b"cursor"):
            self.assertNotIn(value, blob)
        self.assert_no_planted(blob)
        _, out = self.run_cli("metrics")
        self.assertIn("Features (Jira/Linear): 2 of 2 done", out)
        self.assertIn("Spend per merged PR", out)

    def test_tracker_issues_are_cached_and_static_copy_has_no_titles(self) -> None:
        self.run_cli("config", "apply", "--yes")
        jira_calls = [c for c in self.trk.calls if c.startswith("/rest/")]
        self.assertEqual(len(jira_calls), 3)  # CHK-12, its epic CHK-1, and CHK-13 (the epic is already cached)
        self.org.touch("acme/checkout-service", 1, minutes=9)
        self.run_cli("sync")
        self.assertEqual(len([c for c in self.trk.calls if c.startswith("/rest/")]), 3)  # done issues aren't re-read
        with open(os.path.join(self.store, "v1", "derived", "cost.json")) as fh:  # sync reads the live people map
            self.assertEqual(json.load(fh)["per_pr"]["acme/checkout-service#1"], 4.5)
        out = os.path.join(self.dir, "site")
        self.run_cli("build", "--out", out)
        with open(os.path.join(out, "data", "summary.json")) as fh:
            raw = fh.read()
        self.assertNotIn("Dark mode", raw)
        self.assertIn('"agent_led_done": 1', raw)
