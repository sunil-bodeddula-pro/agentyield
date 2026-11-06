from __future__ import annotations

import os
import tempfile
import unittest

from helpers import PLANTED, OrgCase  # first: puts src on the path

# isort: split
from agentyield import config as cfgmod
from agentyield.schema import json_schema

GOOD = """\
version: 1
store: gs://acme-eng-metrics/agentyield
repositories:
  include:
    - name: acme/checkout-service
      team: payments
      exclude_paths: ["vendor/**"]
  select:
    org: acme
    property: agentyield-team
  exclude: [acme/legacy]
agents:
  in-house-bot:
    bot_logins: ['acme-agent[bot]']
metrics:
  revert_window: 30d
  agent_share_threshold: 0.5
guardrails:
  comment_excerpts: false
  retention: 90d
"""


class ValidateTest(unittest.TestCase):
    def test_good_config(self) -> None:
        data, problems = cfgmod.check(GOOD)
        self.assertEqual(problems, [])
        self.assertEqual(data["store"], "gs://acme-eng-metrics/agentyield")

    def test_problems_have_line_numbers(self) -> None:
        bad = (
            GOOD.replace("revert_window: 30d", "revert_window: thirty")
            .replace("exclude:", "exclud:")
            .replace("acme/checkout-service", "checkout-service")
        )
        _, problems = cfgmod.check(bad)
        found = {(p.where(), p.message.split(" (")[0].split(":")[0]) for p in problems}
        self.assertIn(("line 11: repositories.exclud", "unknown setting"), found)
        self.assertTrue(any(p.line == 16 and "duration" in p.message for p in problems))
        self.assertTrue(any(p.line == 5 and "owner/name" in p.message for p in problems))

    def test_repositories_are_required(self) -> None:
        _, problems = cfgmod.check("version: 1\n")
        self.assertEqual([p.where() for p in problems], ["line 1: repositories"])
        _, problems = cfgmod.check("version: 1\nrepositories: {}\n")
        self.assertIn("needs at least one of: include, select", problems[0].message)

    def test_yaml_errors_are_reported(self) -> None:
        _, problems = cfgmod.check("version: 1\nrepositories: [unclosed\n")
        self.assertIn("not valid YAML", problems[0].message)

    def test_schema_export(self) -> None:
        s = json_schema()
        self.assertEqual(s["required"], ["version", "repositories"])
        self.assertFalse(s["properties"]["repositories"]["additionalProperties"])


class LookupTest(unittest.TestCase):
    def test_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            old_cwd, old_env = os.getcwd(), os.environ.get("AGENTYIELD_CONFIG")
            os.environ.pop("AGENTYIELD_CONFIG", None)
            try:
                os.chdir(d)
                open("agentyield.yaml", "w").close()
                self.assertEqual(
                    cfgmod.locate(None),
                    os.path.join(os.path.realpath(d), "agentyield.yaml")
                    if os.path.realpath(d) == os.getcwd()
                    else os.path.abspath("agentyield.yaml"),
                )
                os.environ["AGENTYIELD_CONFIG"] = "github://acme/eng/agentyield.yaml@main"
                self.assertEqual(cfgmod.locate(None), "github://acme/eng/agentyield.yaml@main")
                self.assertEqual(cfgmod.locate("/explicit.yaml"), "/explicit.yaml")
            finally:
                os.chdir(old_cwd)
                if old_env is None:
                    os.environ.pop("AGENTYIELD_CONFIG", None)
                else:
                    os.environ["AGENTYIELD_CONFIG"] = old_env

    def test_github_uri_and_sibling(self) -> None:
        self.assertEqual(
            cfgmod.split_github_uri("github://acme/eng/conf/agentyield.yaml@main"),
            ("acme/eng", "conf/agentyield.yaml", "main"),
        )
        self.assertEqual(
            cfgmod.sibling("github://acme/eng/conf/agentyield.yaml@main", "deny-terms.txt"),
            "github://acme/eng/conf/deny-terms.txt@main",
        )
        self.assertEqual(cfgmod.sibling("gs://b/p/agentyield.yaml", "deny.txt"), "gs://b/p/deny.txt")


class RemoteConfigTest(OrgCase):
    def test_config_and_deny_terms_from_a_platform_repository(self) -> None:
        text = (
            f"version: 1\nstore: {self.store}\ngithub:\n  api_url: {self.gh.url}\nrepositories:\n  include:\n"
            "    - name: acme/checkout-service\nguardrails:\n  deny_terms_file: deny-terms.txt\n"
        )
        self.org.files[("acme/untracked", "metrics/agentyield.yaml")] = text
        self.org.files[("acme/untracked", "metrics/deny-terms.txt")] = f"# customers\n{PLANTED['deny_term']}\n"
        self.config = "github://acme/untracked/metrics/agentyield.yaml@main"
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assert_no_planted(self.store_bytes())
        code, out = self.run_cli("config", "show")
        self.assertIn("github://acme/untracked/metrics/agentyield.yaml@main", out)
        self.assertIn("acme/checkout-service", out)


class InitTest(OrgCase):
    def test_init_writes_a_valid_config(self) -> None:
        path = f"{self.dir}/new/agentyield.yaml"
        code, out = self.run_cli("init", "--repo", "acme/checkout-service", "--path", path, "--store", self.store)
        self.assertEqual(code, 0, out)
        with open(path) as fh:
            self.assertEqual(cfgmod.check(fh.read())[1], [])
        code, out = self.run_cli("init", "--repo", "acme/x", "--path", path)
        self.assertIn("already exists", out)
        code, out = self.run_cli("config", "schema")
        self.assertIn('"agentyield.yaml"', out)
