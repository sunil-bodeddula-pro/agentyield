"""Find, read and check agentyield.yaml.

Lookup order, first match wins:
    1. --config <path or URI>
    2. the AGENTYIELD_CONFIG environment variable
    3. ./agentyield.yaml
    4. ~/.config/agentyield/agentyield.yaml   (Windows: %APPDATA%\\agentyield\\agentyield.yaml)
    5. /etc/agentyield/agentyield.yaml

A path or URI may be a local file, github://owner/repo/path/to/agentyield.yaml@ref, gs://… or s3://….
"""

from __future__ import annotations

import hashlib
import os
import platform
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml

from .facts import DEFAULT_AGENTS, AgentRule
from .guardrails import Filter
from .schema import SCHEMA, Path, Problem

DEFAULT_STORE = "~/.agentyield/data"
DEFAULT_RESTRICTED_LABELS = ["security", "embargo"]


class ConfigError(Exception):
    def __init__(self, source: str, problems: list[Problem]):
        lines = "\n".join(f"  {p.where()}: {p.message}" for p in problems)
        super().__init__(f"{source} has {len(problems)} problem{'s' * (len(problems) != 1)}:\n{lines}")
        self.problems = problems


def user_config_path() -> str:
    if platform.system() == "Windows" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], "agentyield", "agentyield.yaml")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "agentyield", "agentyield.yaml")


def candidates(flag: str | None) -> list[str]:
    out = [flag] if flag else []
    if os.environ.get("AGENTYIELD_CONFIG"):
        out.append(os.environ["AGENTYIELD_CONFIG"])
    out += [os.path.abspath("agentyield.yaml"), user_config_path(), "/etc/agentyield/agentyield.yaml"]
    return out


def locate(flag: str | None) -> str:
    """The config to use. An explicit --config or AGENTYIELD_CONFIG is used even if it doesn't exist yet."""
    for i, c in enumerate(candidates(flag)):
        explicit = i == 0 and (flag or os.environ.get("AGENTYIELD_CONFIG"))
        if explicit or "://" in c or os.path.exists(c):
            return c
    raise SystemExit("No agentyield.yaml found. Run `agentyield init`, or pass --config.")


Fetcher = Callable[[str, str, str], str]  # (owner/repo, path, ref) -> text


def split_github_uri(uri: str) -> tuple[str, str, str]:
    """github://owner/repo/path/to/file.yaml@ref -> (owner/repo, path, ref)."""
    rest, _, ref = uri[len("github://") :].partition("@")
    owner, repo, *path = rest.split("/")
    if not owner or not repo or not path:
        raise SystemExit(f"bad config URI {uri}: use github://owner/repo/path/agentyield.yaml@branch")
    return f"{owner}/{repo}", "/".join(path), ref


def read_text(source: str, fetch_github: Fetcher | None = None) -> str:
    if source.startswith("github://"):
        if fetch_github is None:
            raise SystemExit("reading a github:// config needs GitHub credentials")
        return fetch_github(*split_github_uri(source))
    if source.startswith(("gs://", "s3://")):
        from .store import backend_for  # noqa: PLC0415

        folder, _, name = source.rpartition("/")
        raw = backend_for(folder).get(name)
        if raw is None:
            raise SystemExit(f"config not found: {source}")
        return raw.decode()
    try:
        with open(os.path.expanduser(source), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        raise SystemExit(f"config not found: {source}") from None


def sibling(source: str, name: str) -> str:
    """A file next to the config, such as deny-terms.txt, in the same place the config came from."""
    if os.path.isabs(name) or "://" in name:
        return name
    if source.startswith("github://"):
        repo, path, ref = split_github_uri(source)
        folder = path.rpartition("/")[0]
        return f"github://{repo}/{folder + '/' if folder else ''}{name}" + (f"@{ref}" if ref else "")
    if "://" in source:
        return f"{source.rpartition('/')[0]}/{name}"
    return os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(source))), name)


def _line(node: yaml.Node | None, path: Path) -> int | None:
    """The line of the deepest part of `path` that exists (a missing key reports its parent's line)."""
    line = node.start_mark.line + 1 if node is not None else None
    for part in path:
        if isinstance(node, yaml.MappingNode):
            pair = next(((k, v) for k, v in node.value if k.value == part), None)
            if pair is None:
                break
            line, node = pair[0].start_mark.line + 1, pair[1]
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value):
            node = node.value[part]
            line = node.start_mark.line + 1
        else:
            break
    return line


def check(text: str) -> tuple[dict[str, Any], list[Problem]]:
    """Parse and validate. Problems carry line numbers."""
    try:
        data = yaml.safe_load(text)
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return {}, [Problem((), f"not valid YAML: {getattr(exc, 'problem', exc)}", mark.line + 1 if mark else None)]
    if not isinstance(data, dict):
        return {}, [Problem((), "the file must be a mapping of settings")]
    problems: list[Problem] = []
    SCHEMA.check(data, (), problems)
    for p in problems:
        p.line = _line(root, p.path)
    return data, problems


@dataclass
class RepoSpec:
    name: str
    team: str = ""
    branch: str = ""
    exclude_paths: list[str] = field(default_factory=list)
    restricted: bool = False
    via: str = "include"  # include | select


@dataclass
class Config:
    source: str
    text: str
    data: dict[str, Any]
    deny_terms: list[str] = field(default_factory=list)
    hash_override: str = ""

    @property
    def hash(self) -> str:
        return self.hash_override or hashlib.sha256(self.text.encode()).hexdigest()[:16]

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.data.get(name) or {})

    @property
    def store_uri(self) -> str:
        return str(self.data.get("store") or DEFAULT_STORE)

    @property
    def api_url(self) -> str:
        return str(self.section("github").get("api_url") or "https://api.github.com")

    @property
    def token_env(self) -> str:
        return str((self.section("github").get("auth") or {}).get("token_env") or "GITHUB_TOKEN")

    def agents(self) -> dict[str, AgentRule]:
        out = dict(DEFAULT_AGENTS)
        for name, rule in (self.data.get("agents") or {}).items():
            out[name] = AgentRule(**{k: list(v) for k, v in (rule or {}).items()})
        return out

    def filter(self) -> Filter:
        g = self.section("guardrails")
        return Filter(
            deny_terms=list(g.get("deny_terms") or []) + self.deny_terms,
            extra_secret_patterns=list(g.get("secret_patterns") or []),
            restricted_labels=list(g.get("restricted_labels") or DEFAULT_RESTRICTED_LABELS),
        )

    def guardrail_fingerprint(self) -> str:
        """Changes when anything that decides what is kept changes; then stored facts are filtered again."""
        g = self.section("guardrails")
        restricted = sorted(r.get("name", "") for r in self.included() if r.get("restricted"))
        parts = [
            sorted(g.get("deny_terms") or []),
            sorted(self.deny_terms),
            sorted(g.get("secret_patterns") or []),
            sorted(g.get("restricted_labels") or DEFAULT_RESTRICTED_LABELS),
            restricted,
        ]
        return hashlib.sha256(repr(parts).encode()).hexdigest()[:16]

    def included(self) -> list[dict[str, Any]]:
        return list(self.section("repositories").get("include") or [])

    def resolve_repos(
        self, property_values: Callable[[str], Iterable[dict[str, Any]]] | None = None
    ) -> dict[str, RepoSpec]:
        """Every repository to track: the include list, plus custom-property selection, minus exclude."""
        repos = self.section("repositories")
        out: dict[str, RepoSpec] = {}
        sel = repos.get("select")
        if sel and property_values:
            wanted = set(sel.get("values") or [])
            for row in property_values(sel["org"]):
                props = {p.get("property_name"): p.get("value") for p in row.get("properties") or []}
                value = props.get(sel["property"])
                if isinstance(value, list):
                    value = value[0] if value else None
                if value in (None, "", False) or (wanted and str(value) not in wanted):
                    continue
                name = row.get("repository_full_name", "")
                team = "" if value is True else str(value)
                out[name.lower()] = RepoSpec(name=name, team=team, via="select")
        for r in self.included():
            out[r["name"].lower()] = RepoSpec(
                name=r["name"],
                team=r.get("team", ""),
                branch=r.get("branch", ""),
                exclude_paths=list(r.get("exclude_paths") or []),
                restricted=bool(r.get("restricted")),
            )
        for name in repos.get("exclude") or []:
            out.pop(name.lower(), None)
        return out


def load(source: str, fetch_github: Fetcher | None = None) -> Config:
    text = read_text(source, fetch_github)
    data, problems = check(text)
    if problems:
        raise ConfigError(source, problems)
    cfg = Config(source=source, text=text, data=data)
    terms_file = cfg.section("guardrails").get("deny_terms_file")
    if terms_file:
        raw = read_text(sibling(source, terms_file), fetch_github)
        cfg.deny_terms = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
    return cfg
