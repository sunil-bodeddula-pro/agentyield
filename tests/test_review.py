"""The review-comment classifier: rules, the generic chat-completions endpoint, the optional Anthropic SDK path,
measured accuracy, and how comments flow through sync without their text being kept."""

from __future__ import annotations

import json
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from helpers import CONFIG, PLANTED, OrgCase  # first: puts src on the path

# isort: split
from agentyield import review
from agentyield.review import Label, RulesClassifier, builtin_sample, evaluate


class FakeChat:
    """A chat-completions server: answers with a label per comment and records what it was sent."""

    def __init__(self, reject_schema: bool = False):
        self.bodies: list[dict[str, Any]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.bodies.append({**body, "auth": self.headers.get("Authorization")})
                if reject_schema and body["response_format"]["type"] == "json_schema":
                    self.send_response(400)
                    self.end_headers()
                    return
                prompt = body["messages"][1]["content"]
                n = prompt.count("\n[") + (1 if prompt.split("\n\n", 1)[1].startswith("[") else 0)
                labels = [
                    {"index": i, "category": "correctness" if i % 2 == 0 else "tests", "steering": False}
                    for i in range(n)
                ]
                data = json.dumps({"choices": [{"message": {"content": json.dumps({"labels": labels})}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class RulesTest(unittest.TestCase):
    def test_categories_and_steering(self) -> None:
        clf = RulesClassifier(["acme-agent[bot]"])
        got = [
            lb.category
            for lb in clf.classify(
                [
                    "This builds SQL from user input; use a parameterised query.",
                    "There's a race between the check and the update.",
                    "Please add a test for the empty case.",
                    "@acme-agent split this PR in two",
                    "@gemini fix the lint errors",
                    "LGTM",
                    "nit: typo in the comment",
                ]
            )
        ]
        self.assertEqual(got, ["security", "concurrency", "tests", "steering", "steering", "other", "style"])

    def test_measured_accuracy_on_the_builtin_sample(self) -> None:
        result = evaluate(RulesClassifier(), builtin_sample())
        self.assertEqual(result["sample"], 112)
        self.assertGreaterEqual(result["accuracy"], 0.75)  # the batch's target; real data needs its own sample
        self.assertEqual(result["per_category"]["steering"]["recall"], 1.0)
        self.assertEqual(set(result["per_category"]), set(review.CATEGORIES))


class OpenAICompatibleTest(unittest.TestCase):
    def test_schema_request_and_fallback(self) -> None:
        for reject in (False, True):
            fake = FakeChat(reject_schema=reject)
            os.environ["AY_LLM_KEY"] = "k-123"
            try:
                clf = review.classifier_for(
                    {
                        "classifier": "openai-compatible",
                        "base_url": fake.url,
                        "model": "local-model",
                        "api_key_env": "AY_LLM_KEY",
                    }
                )
                labels = clf.classify(["first", "second", "third"])
            finally:
                fake.close()
                os.environ.pop("AY_LLM_KEY")
            self.assertEqual([lb.category for lb in labels], ["correctness", "tests", "correctness"])
            first = fake.bodies[0]
            self.assertEqual((first["model"], first["temperature"], first["auth"]), ("local-model", 0, "Bearer k-123"))
            self.assertEqual(first["response_format"]["type"], "json_schema")
            if reject:
                self.assertEqual(fake.bodies[1]["response_format"], {"type": "json_object"})

    def test_needs_url_and_model(self) -> None:
        with self.assertRaises(SystemExit):
            review.classifier_for({"classifier": "openai-compatible"})


class AnthropicTest(unittest.TestCase):
    def test_uses_structured_output_and_handles_refusals(self) -> None:
        calls: list[dict[str, Any]] = []

        class Messages:
            def create(self, **kw: Any) -> Any:
                calls.append(kw)
                if len(calls) == 2:
                    return types.SimpleNamespace(stop_reason="refusal", content=[])
                text = json.dumps({"labels": [{"index": 0, "category": "security", "steering": False}]})
                return types.SimpleNamespace(
                    stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=text)]
                )

        fake_sdk = types.ModuleType("anthropic")
        fake_sdk.Anthropic = lambda **kw: types.SimpleNamespace(messages=Messages())  # type: ignore[attr-defined]
        sys.modules["anthropic"] = fake_sdk
        try:
            clf = review.classifier_for({"classifier": "anthropic"})
            clf.batch = 1  # type: ignore[attr-defined]
            labels = clf.classify(["first", "second"])
        finally:
            sys.modules.pop("anthropic")
        self.assertEqual([lb.category for lb in labels], ["security", "other"])  # a refusal is not guessed at
        self.assertEqual(calls[0]["model"], "claude-opus-5-5")
        self.assertEqual(calls[0]["output_config"]["format"]["type"], "json_schema")

    def test_missing_sdk_says_how_to_install(self) -> None:
        sys.modules["anthropic"] = None  # type: ignore[assignment]  # makes `import anthropic` fail
        try:
            with self.assertRaises(SystemExit) as ctx:
                review.classifier_for({"classifier": "anthropic"})
        finally:
            sys.modules.pop("anthropic")
        self.assertIn("agentyield[anthropic]", str(ctx.exception))


class ReviewFlowTest(OrgCase):
    def fact(self, repo: str, n: int) -> dict:
        with open(os.path.join(self.store, "v1", "repos", repo, "prs", f"{n}.json")) as fh:
            return dict(json.load(fh))

    def test_human_comments_classified_and_text_dropped(self) -> None:
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        rec = self.fact("acme/checkout-service", 1)
        inline = {c["id"]: c for c in rec["review_comments"] if c["kind"] == "inline"}
        self.assertEqual(set(inline), {"i501", "i502", "i503", "i504"})  # not the bot, not the author's reply
        self.assertEqual(
            [inline[k]["category"] for k in ("i501", "i503", "i504")], ["correctness", "tests", "steering"]
        )
        self.assertTrue(inline["i504"]["steering"])
        self.assertEqual(rec["bot_review_comments"], 1)
        self.assertNotIn("excerpt", inline["i501"])  # excerpts are off by default
        self.assert_no_planted(self.store_bytes())
        with open(os.path.join(self.store, "v1", "derived", "metrics", "prs", "acme", "checkout-service.json")) as fh:
            m = {r["number"]: r for r in json.load(fh)["prs"]}
        self.assertEqual(m[1]["steering_comments"], 1)
        self.assertEqual(m[1]["review_categories"]["tests"], 1)
        _, out = self.run_cli("metrics")
        self.assertIn("What reviewers flag", out)

    def test_cached_categories_are_not_sent_again(self) -> None:
        fake = FakeChat()
        self.addCleanup(fake.close)
        self.write_config(CONFIG + f"review:\n  classifier: openai-compatible\n  base_url: {fake.url}\n  model: m\n")
        self.run_cli("config", "apply", "--yes")
        sent = [m["content"] for b in fake.bodies for m in b["messages"] if m["role"] == "user"]
        self.assertTrue(sent)
        self.assertNotIn(PLANTED["github_token"], "".join(sent))  # filtered before it leaves the machine
        self.assertNotIn(PLANTED["review"], json.dumps(self.fact("acme/checkout-service", 1)))
        calls = len(fake.bodies)
        self.org.touch("acme/checkout-service", 1, minutes=7)
        self.run_cli("sync")
        self.assertEqual(len(fake.bodies), calls)  # nothing new to classify

    def test_a_classifier_that_is_down_does_not_stop_sync(self) -> None:
        self.write_config(
            CONFIG + "review:\n  classifier: openai-compatible\n  base_url: http://127.0.0.1:9/v1\n  model: m\n"
        )
        code, out = self.run_cli("config", "apply", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("comments left unclassified", out)
        cats = {c["category"] for c in self.fact("acme/checkout-service", 1)["review_comments"]}
        self.assertEqual(cats, {"unclassified"})
        self.write_config(CONFIG)  # back to the rules: the next sync classifies what was left
        self.run_cli("config", "apply", "--yes")
        self.org.touch("acme/checkout-service", 1, minutes=8)
        self.run_cli("sync")
        cats = {c["category"] for c in self.fact("acme/checkout-service", 1)["review_comments"]}
        self.assertNotIn("unclassified", cats)

    def test_eval_command_and_admin_view(self) -> None:
        self.run_cli("config", "apply", "--yes")
        code, out = self.run_cli("classifier", "eval")
        self.assertEqual(code, 0, out)
        self.assertIn("of 112 exact", out)
        self.assertIn("built-in sample", out)
        sample = os.path.join(self.dir, "mine.jsonl")
        with open(sample, "w") as fh:
            fh.write(json.dumps({"text": "Please add a test", "label": "tests"}) + "\n")
            fh.write(json.dumps({"text": "@copilot fix it", "label": "steering"}) + "\n")
        code, out = self.run_cli("classifier", "eval", "--sample", sample)
        self.assertIn("2 of 2 exact (100%) on your labelled sample", out)
        with open(os.path.join(self.store, "v1", "derived", "classifier_eval.json")) as fh:
            self.assertEqual(json.load(fh)["accuracy"], 1.0)


class LabelTest(unittest.TestCase):
    def test_unknown_categories_become_other(self) -> None:
        raw = json.dumps(
            {
                "labels": [
                    {"index": 0, "category": "made-up", "steering": False},
                    {"index": 1, "category": "steering", "steering": False},
                ]
            }
        )
        self.assertEqual(
            review._labels(raw, 3), [Label("other", False), Label("steering", True), Label("other", False)]
        )
