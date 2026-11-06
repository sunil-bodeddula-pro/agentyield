"""What reviewers catch that agents miss: sort human review comments into categories.

Three classifiers, chosen with `review.classifier`; none is tied to a particular agent or vendor:
  rules              built in, the default: keyword rules, no model, nothing leaves the machine
  openai-compatible  any chat-completions endpoint (`review.base_url`, `review.model`): a local server such as
                     Ollama, vLLM or LM Studio, or a hosted provider
  anthropic          Claude through Anthropic's SDK, for organisations that use Claude (`agentyield[anthropic]`)

Comment text is filtered by the guardrails before any classifier sees it, and only the category is stored (plus a
short filtered excerpt if `guardrails.comment_excerpts` is on). Accuracy is measured with
`agentyield classifier eval` against a labelled sample; label your own comments for a number that holds for you.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any, Protocol

CATEGORIES: dict[str, str] = {
    "correctness": "Wrong behaviour, a bug, a missed edge case or a misread requirement",
    "scope": "Changes nobody asked for, work outside the task, duplicate or unwanted features",
    "design": "Structure, duplication, reuse of existing helpers, naming of abstractions, over-engineering",
    "error_handling": "Swallowed errors, missing retries, timeouts or fallbacks, unhandled failure paths",
    "security": "Secrets, injection, authorisation, unsafe input handling or dependencies",
    "performance": "Slow or wasteful code: N+1 queries, excess I/O, needless work in loops, memory",
    "concurrency": "Race conditions, locking, ordering, thread or async safety",
    "tests": "Missing, weak or wrong tests and assertions",
    "docs": "Documentation, comments, docstrings, changelogs and logging messages",
    "style": "Formatting, naming of variables, typos, lint-level nits",
    "build_ci": "Build, configuration, dependencies, CI pipelines and deployment settings",
    "conventions": "Team or project conventions: API compatibility, migrations, feature flags, house rules",
    "steering": "An instruction to an AI agent (for example '@copilot fix the tests'), not a review of the code",
    "other": "Questions, praise, acknowledgements and discussion with no change requested",
}

# Ordered: the first category whose pattern matches wins, so specific concerns come before general ones.
RULES: list[tuple[str, str]] = [
    (
        "security",
        r"secur|inject|\bsql\b|user input|untrusted|xss|csrf|secret|credential|password|token leak|"
        r"auth(?:entication|ori[sz]ation)?\b|"
        r"permission|sanitiz|escap(?:e|ing) (?:user|input)|vulnerab|\bcve\b|unsafe|privilege|encrypt",
    ),
    (
        "concurrency",
        r"race condition|\brace\b|deadlock|thread[- ]?safe|concurren|\block(?:ing|s|ed)?\b|mutex|"
        r"atomic|async|await|in parallel|ordering",
    ),
    (
        "performance",
        r"perf|slow|n\+1|o\(n|quadratic|cache|memory|allocat|latency|expensive|inefficien|"
        r"every (?:iteration|loop)|in a loop|batch (?:the|these)|too many (?:queries|calls|requests)",
    ),
    (
        "error_handling",
        r"error handling|exception|swallow|try/except|try/catch|\bcatch\b|retry|retries|timeout|"
        r"fallback|fails? silently|handle the (?:case|failure|error)|panic|unhandled",
    ),
    ("tests", r"\btest|assert|coverage|mock|fixture|spec\b|regression"),
    (
        "build_ci",
        r"\bci\b|pipeline|workflow|build|dockerfile|makefile|dependenc|package\.json|requirements|"
        r"lockfile|version pin|\bdeploy|config(?:uration)? file|env var|github action",
    ),
    (
        "docs",
        r"\bdocs?\b|document|docstring|comment (?:here|this|explaining)|readme|changelog|log message|"
        r"jsdoc|explain (?:why|what)",
    ),
    (
        "conventions",
        r"convention|we (?:usually|always|never|don't)|our (?:style guide|guidelines|pattern)|"
        r"backward[s]? compat|breaking change|migration|feature flag|house style|team (?:rule|standard)",
    ),
    (
        "scope",
        r"out of scope|not (?:part of|needed for) this|unrelated|separate pr|didn't ask|wasn't asked|"
        r"scope creep|unnecessary change|revert this (?:file|change)|why (?:is|was) this (?:file )?changed",
    ),
    (
        "design",
        r"refactor|duplicat|reuse|existing (?:helper|util|function)|abstraction|extract|single "
        r"responsibility|too (?:complex|clever)|over-?engineer|simplif|coupling|layer|separation of concerns",
    ),
    (
        "correctness",
        r"bug|wrong|incorrect|doesn't work|does not work|broken|edge case|off[- ]by[- ]one|null|"
        r"\bnone\b|undefined|should (?:be|return|not)|returns? the wrong|logic|regress|crash|miss(?:ing|es) "
        r"the case|what (?:if|happens when)",
    ),
    (
        "style",
        r"nit|typo|naming|rename|format|indent|whitespace|lint|spacing|camel|snake_case|spelling|unused "
        r"(?:import|variable)|trailing",
    ),
]
# @-handles of widely used coding agents and AI reviewers, from many vendors; configured agents' bot logins and
# names are added to these. Only used to recognise "@agent, please do X" comments, never to attribute PRs.
KNOWN_AGENT_HANDLES = (
    "copilot",
    "claude",
    "codex",
    "devin",
    "cursor",
    "gemini",
    "jules",
    "amazon-q",
    "q",
    "sweep",
    "aider",
    "openhands",
    "coderabbitai",
    "sourcery-ai",
    "qodo",
)
GENERIC_STEERING = r"^\s*@[\w-]*(?:bot|agent|ai)\b|^\s*/(?:fix|retry|rebase|review)\b"


def steering_pattern(mentions: Sequence[str] = ()) -> re.Pattern[str]:
    """An instruction to an agent: a comment that starts by @-mentioning one of the configured agents (their bot
    logins and names), any account named like a bot or agent, or a slash command."""
    handles = [*KNOWN_AGENT_HANDLES, *mentions]
    names = sorted({m.lstrip("@").removesuffix("[bot]") for m in handles if m}, key=len, reverse=True)
    named = rf"^\s*@(?:{'|'.join(re.escape(n) for n in names)})\b|" if names else ""
    return re.compile(named + GENERIC_STEERING, re.I)


OTHER = re.compile(r"^\s*(?:lgtm|looks good|thanks|thank you|nice|great|\+1|approved|ship it)\b", re.I)


@dataclass
class Label:
    category: str
    steering: bool


class Classifier(Protocol):
    name: str

    def classify(self, texts: Sequence[str]) -> list[Label]: ...


class RulesClassifier:
    name = "rules"

    def __init__(self, mentions: Sequence[str] = ()) -> None:
        self._rules = [(cat, re.compile(p, re.I)) for cat, p in RULES]
        self._steering = steering_pattern(mentions)

    def classify(self, texts: Sequence[str]) -> list[Label]:
        out = []
        for text in texts:
            if self._steering.search(text):
                out.append(Label("steering", True))
                continue
            low = text.lower()
            category = next((cat for cat, rx in self._rules if rx.search(low)), None)
            if category is None:
                category = "other"
            if OTHER.search(text) and category in ("style", "correctness") and len(text) < 40:
                category = "other"
            out.append(Label(category, False))
        return out


SYSTEM = (
    "You sort code review comments into categories. Each comment was written by a human reviewer on a pull "
    "request. Answer with one category per comment, using only these names:\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in CATEGORIES.items())
    + "\nSet steering to true only when the comment instructs an AI agent rather than reviewing the code."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "steering": {"type": "boolean"},
                },
                "required": ["index", "category", "steering"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["labels"],
    "additionalProperties": False,
}


def _prompt(texts: Sequence[str]) -> str:
    lines = [f"[{i}] {t[:1500]}" for i, t in enumerate(texts)]
    return "Label each comment by its index.\n\n" + "\n\n".join(lines)


def _labels(raw: str, n: int) -> list[Label]:
    data = json.loads(raw)
    by = {int(item["index"]): item for item in data.get("labels") or []}
    out = []
    for i in range(n):
        item = by.get(i) or {}
        cat = str(item.get("category") or "other")
        out.append(Label(cat if cat in CATEGORIES else "other", bool(item.get("steering")) or cat == "steering"))
    return out


class OpenAICompatibleClassifier:
    """Any chat-completions endpoint: Ollama, vLLM, LM Studio, OpenAI, Azure OpenAI, OpenRouter and others.
    Plain HTTP from the standard library; no provider SDK."""

    name = "openai-compatible"

    def __init__(self, base_url: str, model: str, api_key_env: str = "", batch: int = 20):
        self.base_url, self.model, self.batch = base_url.rstrip("/"), model, batch
        self.api_key = os.environ.get(api_key_env, "") if api_key_env else ""

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode(), method="POST", headers=headers
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return dict(json.loads(resp.read()))

    def classify(self, texts: Sequence[str]) -> list[Label]:
        out: list[Label] = []
        for i in range(0, len(texts), self.batch):
            chunk = texts[i : i + self.batch]
            body: dict[str, Any] = {
                "model": self.model,
                "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _prompt(chunk)}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "labels", "strict": True, "schema": SCHEMA},
                },
            }
            try:
                data = self._post(body)
            except urllib.error.HTTPError as err:
                if err.code != 400:
                    raise
                # Some servers don't take a JSON schema; ask for JSON and rely on the prompt.
                body["response_format"] = {"type": "json_object"}
                body["messages"][0]["content"] = (
                    SYSTEM + '\nReply with JSON: {"labels": [{"index", "category", "steering"}]}'
                )
                data = self._post(body)
            content = data["choices"][0]["message"]["content"] or '{"labels": []}'
            out.extend(_labels(content, len(chunk)))
        return out


class AnthropicClassifier:
    """Claude through Anthropic's official SDK, for organisations that use Claude."""

    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", api_key_env: str = "ANTHROPIC_API_KEY", batch: int = 40):
        try:
            import anthropic  # noqa: PLC0415  optional extra
        except ImportError as exc:
            raise SystemExit(
                "review.classifier: anthropic needs the Anthropic SDK: pip install 'agentyield[anthropic]'"
            ) from exc
        key = os.environ.get(api_key_env)
        self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.model, self.batch = model, batch

    def classify(self, texts: Sequence[str]) -> list[Label]:
        out: list[Label] = []
        for i in range(0, len(texts), self.batch):
            chunk = texts[i : i + self.batch]
            response = self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=SYSTEM,
                messages=[{"role": "user", "content": _prompt(chunk)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            )
            if response.stop_reason in ("refusal", "max_tokens"):
                out.extend(Label("other", False) for _ in chunk)  # don't guess; leave these unclassified
                continue
            text = next((b.text for b in response.content if b.type == "text"), '{"labels": []}')
            out.extend(_labels(text, len(chunk)))
        return out


def classifier_for(review: dict[str, Any], mentions: Sequence[str] = ()) -> Classifier:
    kind = str(review.get("classifier") or "rules")
    if kind == "openai-compatible":
        if not review.get("base_url") or not review.get("model"):
            raise SystemExit("review.classifier: openai-compatible needs review.base_url and review.model")
        return OpenAICompatibleClassifier(
            str(review["base_url"]), str(review["model"]), str(review.get("api_key_env") or "")
        )
    if kind == "anthropic":
        return AnthropicClassifier(
            str(review.get("model") or "claude-opus-5-5"), str(review.get("api_key_env") or "ANTHROPIC_API_KEY")
        )
    return RulesClassifier(mentions)


def builtin_sample() -> list[dict[str, str]]:
    text = resources.files("agentyield").joinpath("data/review_sample.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def load_sample(path: str | None) -> list[dict[str, str]]:
    if not path:
        return builtin_sample()
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def evaluate(clf: Classifier, sample: list[dict[str, str]]) -> dict[str, Any]:
    """Exact-match accuracy, per-category precision and recall, and the most common confusions."""
    predicted = [lb.category for lb in clf.classify([s["text"] for s in sample])]
    truth = [s["label"] for s in sample]
    exact = sum(p == t for p, t in zip(predicted, truth, strict=True))
    per: dict[str, dict[str, Any]] = {}
    for cat in CATEGORIES:
        tp = sum(p == cat and t == cat for p, t in zip(predicted, truth, strict=True))
        pp, tt = predicted.count(cat), truth.count(cat)
        per[cat] = {
            "support": tt,
            "precision": round(tp / pp, 3) if pp else None,
            "recall": round(tp / tt, 3) if tt else None,
        }
    confusions = Counter((t, p) for p, t in zip(predicted, truth, strict=True) if p != t)
    return {
        "classifier": clf.name,
        "sample": len(sample),
        "exact": exact,
        "accuracy": round(exact / len(sample), 3) if sample else None,
        "per_category": per,
        "top_confusions": [{"label": t, "predicted": p, "count": n} for (t, p), n in confusions.most_common(8)],
    }
