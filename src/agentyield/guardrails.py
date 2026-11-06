"""The sensitive-data filter. Every piece of text passes through `Filter.clean` before it is stored, and the
fields that aren't needed (code, PR bodies, comment text, commit messages, emails) are never stored at all.

It fails closed: if a check raises, the text is dropped rather than kept. Pattern matching misses some secrets,
which is why so little text is kept in the first place.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

# (kind, pattern). Specific token formats first, then generic assignments, then personal data.
SECRET_PATTERNS: list[tuple[str, str]] = [
    ("secret", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    ("secret", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    ("secret", r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    ("secret", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ("secret", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("secret", r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    ("secret", r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{20,}\b"),
    ("secret", r"\bey[A-Za-z0-9_-]{10,}\.ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    ("secret", r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@[^\s/]+"),  # URL with user:password
    (
        "secret",
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\b"
        r"\s*[:=]\s*['\"]?[^\s'\"]{6,}",
    ),
]
PERSONAL_PATTERNS: list[tuple[str, str]] = [
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ("path", r"(?:/home/|/Users/|[A-Za-z]:\\Users\\)[^\s/\\]+"),
    ("host", r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:internal|corp|lan|intranet|local)\b"),
    ("ip", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # international (+44 20 7946 0958) or 3-3-4 (555-123-4567, (555) 123-4567); dates such as 2026-11-05 don't match
    ("phone", r"(?<![\w+])\+\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){2,5}(?!\w)"),
    ("phone", r"(?<!\w)(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\w)"),
]
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
WORDISH = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
HEX = re.compile(r"^[0-9a-f]+$")


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass
class Filter:
    deny_terms: list[str] = field(default_factory=list)
    extra_secret_patterns: list[str] = field(default_factory=list)
    restricted_labels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        pats = SECRET_PATTERNS + [("secret", p) for p in self.extra_secret_patterns] + PERSONAL_PATTERNS
        self._compiled = [(kind, re.compile(p)) for kind, p in pats]
        terms = sorted({t.strip() for t in self.deny_terms if t.strip()}, key=len, reverse=True)
        self._deny = re.compile("|".join(rf"(?<!\w){re.escape(t)}(?!\w)" for t in terms), re.I) if terms else None
        self._restricted = {label.lower() for label in self.restricted_labels}

    def clean(self, text: str | None) -> tuple[str, list[str]]:
        """The text with sensitive parts replaced by [REDACTED:kind], and the kinds found."""
        if not text:
            return "", []
        try:
            return self._clean(str(text))
        except Exception:  # fail closed: never keep text the filter couldn't check
            return "", ["filter-error"]

    def _clean(self, text: str) -> tuple[str, list[str]]:
        found: list[str] = []

        def sub(kind: str, rx: re.Pattern[str], s: str) -> str:
            def repl(m: re.Match[str]) -> str:
                found.append(kind)
                return f"[REDACTED:{kind}]"

            return rx.sub(repl, s)

        for kind, rx in self._compiled:
            text = sub(kind, rx, text)
        text = CARD.sub(lambda m: self._card(m, found), text)
        text = WORDISH.sub(lambda m: self._high_entropy(m, found), text)
        if self._deny:
            text = sub("term", self._deny, text)
        return text, sorted(set(found))

    @staticmethod
    def _card(m: re.Match[str], found: list[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("card")
            return "[REDACTED:card]"
        return m.group(0)

    @staticmethod
    def _high_entropy(m: re.Match[str], found: list[str]) -> str:
        s = m.group(0)
        if s.startswith("REDACTED") or (HEX.match(s.lower()) and len(s) in (40, 64)):  # git SHAs are fine
            return s
        classes = sum(bool(re.search(p, s)) for p in ("[a-z]", "[A-Z]", "[0-9]"))
        if classes >= 3 and _entropy(s) >= 4.0:
            found.append("secret")
            return "[REDACTED:secret]"
        return s

    def clean_all(self, items: Iterable[str]) -> tuple[list[str], list[str]]:
        out, kinds = [], set()
        for item in items:
            text, found = self.clean(item)
            if text:
                out.append(text)
            kinds.update(found)
        return out, sorted(kinds)

    def restricted_label(self, labels: Iterable[str]) -> bool:
        return any(label.lower() in self._restricted for label in labels)
