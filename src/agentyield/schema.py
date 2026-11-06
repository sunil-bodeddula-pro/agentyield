"""The shape of agentyield.yaml, written once and used twice: to validate the file (with line numbers) and to
export a JSON Schema for editors (`agentyield config schema`).

Unknown keys are errors, so a typo such as `repositores:` is reported instead of silently ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

Path = tuple[str | int, ...]

DURATION = re.compile(r"^(\d+)([mhdw])$")
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass
class Problem:
    path: Path
    message: str
    line: int | None = None

    def where(self) -> str:
        dotted = ".".join(str(p) for p in self.path) or "(top level)"
        return f"line {self.line}: {dotted}" if self.line else dotted


class Spec:
    description = ""

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        raise NotImplementedError

    def json_schema(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class Str(Spec):
    description: str = ""
    pattern: re.Pattern[str] | None = None
    hint: str = ""

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if not isinstance(value, str):
            out.append(Problem(path, f"must be text, got {type(value).__name__}"))
        elif self.pattern and not self.pattern.match(value):
            out.append(Problem(path, f"{value!r} is not valid: {self.hint}"))

    def json_schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {"type": "string", "description": self.description}
        if self.pattern:
            s["pattern"] = self.pattern.pattern
        return s


@dataclass
class Enum(Spec):
    choices: tuple[str, ...]
    description: str = ""

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if value not in self.choices:
            out.append(Problem(path, f"must be one of {', '.join(self.choices)}, got {value!r}"))

    def json_schema(self) -> dict[str, Any]:
        return {"enum": list(self.choices), "description": self.description}


@dataclass
class Int(Spec):
    description: str = ""
    minimum: int = 0
    maximum: int | None = None

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            out.append(Problem(path, f"must be a whole number, got {value!r}"))
        elif value < self.minimum or (self.maximum is not None and value > self.maximum):
            out.append(Problem(path, f"must be between {self.minimum} and {self.maximum}, got {value}"))

    def json_schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {"type": "integer", "minimum": self.minimum, "description": self.description}
        if self.maximum is not None:
            s["maximum"] = self.maximum
        return s


@dataclass
class Fraction(Spec):
    description: str = ""

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            out.append(Problem(path, f"must be a number from 0 to 1, got {value!r}"))

    def json_schema(self) -> dict[str, Any]:
        return {"type": "number", "minimum": 0, "maximum": 1, "description": self.description}


@dataclass
class Bool(Spec):
    description: str = ""

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if not isinstance(value, bool):
            out.append(Problem(path, f"must be true or false, got {value!r}"))

    def json_schema(self) -> dict[str, Any]:
        return {"type": "boolean", "description": self.description}


@dataclass
class Duration(Spec):
    description: str = ""

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if not isinstance(value, str) or not DURATION.match(value):
            out.append(Problem(path, f"must be a duration such as 15m, 12h, 30d or 2w, got {value!r}"))

    def json_schema(self) -> dict[str, Any]:
        return {"type": "string", "pattern": DURATION.pattern, "description": self.description}


@dataclass
class List(Spec):
    item: Spec
    description: str = ""

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if not isinstance(value, list):
            out.append(Problem(path, "must be a list"))
            return
        for i, v in enumerate(value):
            self.item.check(v, (*path, i), out)

    def json_schema(self) -> dict[str, Any]:
        return {"type": "array", "items": self.item.json_schema(), "description": self.description}


@dataclass
class Obj(Spec):
    fields: dict[str, Spec]
    required: tuple[str, ...] = ()
    description: str = ""
    one_of_required: tuple[str, ...] = ()

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if not isinstance(value, dict):
            out.append(Problem(path, "must be a mapping of keys to values"))
            return
        for key in value:
            if key not in self.fields:
                near = [k for k in self.fields if k[:3] == str(key)[:3]]
                tip = f" (did you mean {near[0]!r}?)" if near else ""
                out.append(Problem((*path, key), f"unknown setting{tip}"))
        for key in self.required:
            if key not in value:
                out.append(Problem((*path, key), "is required"))
        if self.one_of_required and not any(k in value for k in self.one_of_required):
            out.append(Problem(path, f"needs at least one of: {', '.join(self.one_of_required)}"))
        for key, spec in self.fields.items():
            if key in value and value[key] is not None:
                spec.check(value[key], (*path, key), out)

    def json_schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {k: v.json_schema() for k, v in self.fields.items()},
            "description": self.description,
        }
        if self.required:
            s["required"] = list(self.required)
        return s


@dataclass
class Map(Spec):
    value: Spec
    description: str = ""
    key_pattern: re.Pattern[str] = field(default_factory=lambda: re.compile(r"^[A-Za-z0-9_.-]+$"))

    def check(self, value: Any, path: Path, out: list[Problem]) -> None:
        if not isinstance(value, dict):
            out.append(Problem(path, "must be a mapping of names to settings"))
            return
        for k, v in value.items():
            if not isinstance(k, str) or not self.key_pattern.match(k):
                out.append(Problem((*path, str(k)), "names may use letters, digits, '.', '_' and '-'"))
            self.value.check(v, (*path, str(k)), out)

    def json_schema(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": self.value.json_schema(), "description": self.description}


TEXTS = List(Str())
REPO = Str("owner/name", REPO_NAME, "use owner/name, for example acme/checkout-service")

SCHEMA = Obj(
    description="AgentYield configuration",
    required=("version", "repositories"),
    fields={
        "version": Int("Config format version", minimum=1, maximum=1),
        "store": Str("Where data is kept: a folder, gs://bucket/path or s3://bucket/path"),
        "github": Obj(
            {
                "api_url": Str("GitHub API URL; change it for GitHub Enterprise Server"),
                "auth": Obj(
                    {
                        "token_env": Str("Environment variable holding a GitHub token"),
                        "app_id_env": Str("Environment variable holding a GitHub App ID"),
                        "private_key_env": Str("Environment variable holding the App's private key (PEM)"),
                        "installation_id_env": Str("Environment variable holding the App installation ID"),
                    }
                ),
            }
        ),
        "repositories": Obj(
            description="Which repositories to track. Nothing else is ever read.",
            one_of_required=("include", "select"),
            fields={
                "include": List(
                    Obj(
                        required=("name",),
                        fields={
                            "name": REPO,
                            "team": Str("Team this repository belongs to"),
                            "branch": Str("Main branch; defaults to the repository's default branch"),
                            "exclude_paths": List(Str("Glob of files left out of line counts")),
                            "restricted": Bool("Numbers only: no titles, branch names or labels are kept"),
                        },
                    ),
                    "Repositories listed by name",
                ),
                "select": Obj(
                    required=("org", "property"),
                    description="Repositories where an organisation admin set a custom property",
                    fields={
                        "org": Str("Organisation login"),
                        "property": Str("Custom property name; its value becomes the team"),
                        "values": TEXTS,
                    },
                ),
                "exclude": List(REPO, "Never tracked, even if included or selected"),
            },
        ),
        "teams": Map(Obj({"github_team": Str("org/team-slug"), "members": TEXTS})),
        "agents": Map(
            Obj(
                {
                    "bot_logins": TEXTS,
                    "trailers": List(Str("Text a commit trailer starts with, e.g. 'Co-Authored-By: Claude'")),
                    "branch_prefixes": TEXTS,
                    "labels": TEXTS,
                    "body_markers": List(Str("Text in a PR description that marks agent work")),
                }
            ),
            "How each agent's work is recognised",
        ),
        "functionality": Obj(
            {
                "issue_tracker": Enum(("github", "jira", "linear")),
                "feature_labels": TEXTS,
                "user_facing_types": List(Str("Conventional commit type, e.g. feat or fix")),
            }
        ),
        "metrics": Obj(
            {
                "working_hours": Obj(
                    {"timezone": Str(), "days": Str("e.g. Mon-Fri"), "start": Str("HH:MM"), "end": Str("HH:MM")}
                ),
                "revert_window": Duration(),
                "follow_up_window": Duration(),
                "min_sample": Int(minimum=1),
                "ignore_authors": TEXTS,
                "agent_share_threshold": Fraction("Share of a PR's lines above which it counts as agent-authored"),
            }
        ),
        "guardrails": Obj(
            {
                "deny_terms": List(Str("Word or phrase removed from all stored text")),
                "deny_terms_file": Str("File with one deny term per line, next to the config"),
                "secret_patterns": List(Str("Extra regular expression treated as a secret")),
                "restricted_labels": TEXTS,
                "comment_excerpts": Bool("Keep redacted review-comment excerpts (off by default)"),
                "retention": Duration(),
            }
        ),
        "outputs": Obj(
            {
                "dashboard": Obj({"port": Int(minimum=1, maximum=65535), "base_url": Str()}),
                "static_site": Str(),
                "publish": Str("github-pages:owner/repo, gs://… or s3://…"),
                "digest": Obj({"slack_webhook_env": Str()}),
            }
        ),
    },
)


def json_schema() -> dict[str, Any]:
    s = SCHEMA.json_schema()
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    s["title"] = "agentyield.yaml"
    return s
