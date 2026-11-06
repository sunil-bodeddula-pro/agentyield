from __future__ import annotations

import json
import os

from helpers import CONFIG, PLANTED, OrgCase


class ApplyAndSyncTest(OrgCase):
    def pr(self, repo: str, n: int) -> dict:
        with open(os.path.join(self.store, "v1", "repos", repo, "prs", f"{n}.json"), encoding="utf-8") as fh:
            return dict(json.load(fh))

    def test_apply_backfills_only_configured_repos_within_cutoff(self) -> None:
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        prs = sorted(os.listdir(os.path.join(self.store, "v1", "repos", "acme", "checkout-service", "prs")))
        self.assertEqual(prs, ["1.json", "2.json", "3.json", "4.json"])  # PR 5 is 400 days old
        self.assertFalse(any("untracked" in p for p in self.gh.requests), "an unlisted repository was read")
        self.assertTrue(os.path.exists(os.path.join(self.store, "v1", "repos", "acme", "security-patches")))

    def test_back_and_forth_summary(self) -> None:
        self.run_cli("config", "apply", "--yes")
        rec = self.pr("acme/checkout-service", 1)
        self.assertEqual(
            rec["summary"],
            {
                "commits": 3,
                "commits_after_first_review": 1,
                "review_iterations": 1,
                "changes_requested": 1,
                "human_reviews": 2,
                "force_pushes": 1,
            },
        )
        self.assertEqual(rec["state"], "merged")
        self.assertEqual([c["type"] for c in rec["commits"]], ["feat", "test", "fix"])
        self.assertEqual(rec["signals"]["trailers"], {"claude-code": 2})
        self.assertEqual(rec["signals"]["branch"], "claude-code")
        self.assertEqual(rec["signals"]["body_markers"], ["claude-code"])
        self.assertEqual(self.pr("acme/web-dashboard", 1)["signals"]["branch"], "copilot")

    def test_nothing_sensitive_reaches_the_store_or_output(self) -> None:
        _, out1 = self.run_cli("config", "apply", "--yes")
        _, out2 = self.run_cli("sync")
        _, out3 = self.run_cli("status")
        self.assert_no_planted(self.store_bytes())
        self.assert_no_planted(out1 + out2 + out3)
        rec = self.pr("acme/checkout-service", 2)
        self.assertIn("[REDACTED:secret]", rec["title"])
        self.assertIn("[REDACTED:term]", rec["title"])
        self.assertEqual(rec["branch"], "fix/[REDACTED:secret]")
        self.assertIn("secret", rec["redacted"])
        restricted = self.pr("acme/security-patches", 1)
        self.assertEqual((restricted["title"], restricted["branch"], restricted["labels"]), ("", "", []))
        self.assertTrue(restricted["restricted"])
        by_label = self.pr("acme/checkout-service", 3)
        self.assertTrue(by_label["restricted"])
        self.assertEqual(by_label["title"], "")
        self.assertNotIn("email", json.dumps(self.pr("acme/checkout-service", 1)["commits"]))

    def test_second_sync_is_free_and_updates_are_picked_up(self) -> None:
        self.run_cli("config", "apply", "--yes")
        before = len(self.gh.requests)
        code, out = self.run_cli("sync")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("unchanged"), 3)
        self.assertEqual(len(self.gh.requests) - before, 3)  # one conditional list request per repository
        self.org.touch("acme/checkout-service", 4, minutes=5)
        self.org.pulls["acme/checkout-service"][4]["title"] = "docs: access guide"
        _, out = self.run_cli("sync")
        self.assertIn("acme/checkout-service: 1 PRs", out)
        self.assertEqual(self.pr("acme/checkout-service", 4)["title"], "docs: access guide")

    def test_backfill_reaches_older_history(self) -> None:
        self.run_cli("config", "apply", "--yes")
        code, out = self.run_cli("backfill", "acme/checkout-service", "--since", "500d")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.pr("acme/checkout-service", 5)["title"], "feat: ancient history")

    def test_sync_needs_an_applied_config(self) -> None:
        code, out = self.run_cli("sync")
        self.assertNotEqual(code, 0)
        self.assertIn("config apply", out)


class ConfigChangeTest(OrgCase):
    def test_diff_then_apply_add_remove_restrict_and_tighten(self) -> None:
        self.run_cli("config", "apply", "--yes")
        changed = (
            CONFIG.replace("    - name: acme/web-dashboard\n      team: web\n", "")
            .replace("      team: payments\n", "      team: checkout\n      restricted: true\n")
            .replace('deny_terms: ["{deny}"]', 'deny_terms: ["{deny}", "idempotency"]')
        )
        self.write_config(changed)
        _, out = self.run_cli("config", "diff")
        for expected in ("remove", "acme/web-dashboard", "restrict", "team", "payments -> checkout", "guardrails"):
            self.assertIn(expected, out)
        self.assertFalse(os.path.exists(os.path.join(self.store, "v1", "config", "x")))
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.store, "v1", "repos", "acme", "checkout-service", "prs", "1.json")) as fh:
            rec = json.load(fh)
        self.assertEqual((rec["title"], rec["team"], rec["restricted"]), ("", "checkout", True))
        _, status = self.run_cli("status")
        self.assertIn("Hidden (removed, data kept): acme/web-dashboard", status)
        _, out = self.run_cli("config", "diff")
        self.assertIn("No changes", out)

    def test_new_deny_term_applies_on_the_next_sync_without_apply(self) -> None:
        self.run_cli("config", "apply", "--yes")
        self.write_config(CONFIG.replace('deny_terms: ["{deny}"]', 'deny_terms: ["{deny}", "Initech"]'))
        self.org.pulls["acme/web-dashboard"][2]["title"] = "fix: chart overflow for Initech"
        self.org.touch("acme/web-dashboard", 2, minutes=3)
        self.run_cli("sync")
        self.assertNotIn(b"Initech", self.store_bytes())

    def test_unapplied_change_is_reported_and_not_used(self) -> None:
        self.run_cli("config", "apply", "--yes")
        self.write_config(CONFIG.replace("acme/security-patches", "acme/untracked"))
        _, out = self.run_cli("sync")
        self.assertIn("aren't applied yet", out)
        self.assertFalse(any("untracked" in p for p in self.gh.requests))

    def test_custom_property_selection_and_opt_in_on_sync(self) -> None:
        self.write_config(
            CONFIG.split("repositories:")[0] + "repositories:\n  select:\n    org: acme\n"
            "    property: agentyield-team\n"
        )
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        repos = sorted(os.listdir(os.path.join(self.store, "v1", "repos", "acme")))
        self.assertEqual(repos, ["checkout-service", "web-dashboard"])
        self.org.properties["acme/security-patches"] = {"agentyield-team": "platform"}
        _, out = self.run_cli("sync")
        self.assertIn("acme/security-patches: opted in by custom property", out)
        _, status = self.run_cli("status")
        self.assertIn("acme/security-patches", status)

    def test_lease_blocks_a_second_writer(self) -> None:
        self.run_cli("config", "apply", "--yes")
        lease = os.path.join(self.store, "v1", ".lease")
        with open(lease, "w") as fh:
            json.dump({"holder": "other-machine:1", "expires": 9_999_999_999}, fh)
        code, out = self.run_cli("sync")
        self.assertEqual(code, 2)
        self.assertIn("other-machine", out)


class PlantedValuesTest(OrgCase):
    def test_planted_values_are_really_in_the_fixtures(self) -> None:
        """Guards the leak tests: they only mean something if GitHub's answers contain the planted values."""
        blob = json.dumps({"pulls": self.org.pulls, "timelines": {str(k): v for k, v in self.org.timelines.items()}})
        for name, value in PLANTED.items():
            self.assertIn(value, blob, name)
