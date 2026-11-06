"""Agent spend per merged PR and per feature. Optional: configure `cost.sources`.

Sources, any mix:
  file         a CSV or JSON Lines file from any tool or vendor, local or in a bucket, with columns
               date (YYYY-MM-DD), person (GitHub login or email), amount_usd, and optionally tool
  claude_code  Anthropic's Claude Code Analytics Admin API: estimated cost per user per day

Spend for a person on a day is shared equally across the agent and assisted PRs they authored that had commits that
day (or, failing that, were open that day). What can't be matched is reported as unallocated, never guessed.

Privacy: daily spend per person is read at sync and never stored; only the per-PR allocations and the daily
unallocated totals are kept. Emails in the sources are mapped to GitHub logins with `cost.people` and dropped.
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

ANTHROPIC_VERSION = "2026-10-21"


def read_rows(text: str, name: str) -> list[dict[str, Any]]:
    if name.endswith((".jsonl", ".json")):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return list(csv.DictReader(io.StringIO(text)))


def from_file(path: str) -> list[dict[str, Any]]:
    if path.startswith(("gs://", "s3://")):
        from .store import backend_for  # noqa: PLC0415

        folder, _, name = path.rpartition("/")
        raw = backend_for(folder).get(name)
        text = raw.decode() if raw else ""
    else:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            text = fh.read()
    out = []
    for row in read_rows(text, path):
        try:
            out.append(
                {
                    "date": str(row["date"])[:10],
                    "person": str(row["person"]).strip(),
                    "amount_usd": float(row["amount_usd"]),
                    "tool": str(row.get("tool") or ""),
                }
            )
        except (KeyError, ValueError):
            continue  # a malformed row is skipped, not guessed at
    return out


def from_claude_code(
    admin_key: str, days: list[str], api_url: str = "https://api.anthropic.com"
) -> list[dict[str, Any]]:
    """Daily estimated cost per user from the Claude Code Analytics Admin API (amounts are in US cents)."""
    out = []
    for day in days:
        page: str | None = None
        while True:
            q = {"starting_at": day, "limit": 1000, **({"page": page} if page else {})}
            req = urllib.request.Request(
                f"{api_url.rstrip('/')}/v1/organizations/usage_report/claude_code?{urllib.parse.urlencode(q)}",
                headers={"x-api-key": admin_key, "anthropic-version": ANTHROPIC_VERSION, "User-Agent": "agentyield"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            for rec in data.get("data") or []:
                actor = rec.get("actor") or {}
                person = actor.get("email_address") or actor.get("api_key_name") or ""
                cents = sum(
                    float((m.get("estimated_cost") or {}).get("amount") or 0) for m in rec.get("model_breakdown") or []
                )
                if person and cents:
                    out.append({"date": day, "person": person, "amount_usd": cents / 100, "tool": "claude-code"})
            page = data.get("next_page") if data.get("has_more") else None
            if not page:
                break
    return out


def people_map(cost: dict[str, Any]) -> dict[str, str]:
    return {str(p["person"]).lower(): str(p["login"]) for p in cost.get("people") or []}


def gather(cost: dict[str, Any], since: date, until: date, base_dir: str = "") -> list[dict[str, Any]]:
    days = [(since + timedelta(days=i)).isoformat() for i in range((until - since).days + 1)]
    rows: list[dict[str, Any]] = []
    for src in cost.get("sources") or []:
        if src.get("type") == "file":
            path = str(src["path"])
            if base_dir and not os.path.isabs(path) and "://" not in path:
                path = os.path.join(base_dir, path)
            rows += [r for r in from_file(path) if since.isoformat() <= r["date"] <= until.isoformat()]
        elif src.get("type") == "claude_code":
            key = os.environ.get(str(src.get("admin_key_env") or "ANTHROPIC_ADMIN_KEY"), "")
            if not key:
                raise SystemExit("cost source claude_code needs an Admin API key in its admin_key_env variable")
            rows += from_claude_code(key, days, str(src.get("api_url") or "https://api.anthropic.com"))
    mapping = people_map(cost)
    for r in rows:
        r["login"] = mapping.get(r["person"].lower(), r["person"] if "@" not in r["person"] else "")
    return rows


def allocate(rows: list[dict[str, Any]], prs: list[dict[str, Any]]) -> dict[str, Any]:
    """Share each person-day of spend across that person's agent and assisted PRs active that day."""
    by_author: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in prs:
        if p["group"] in ("agent", "assisted"):
            by_author[p["author"].lower()].append(p)
    per_pr: dict[str, float] = defaultdict(float)
    unallocated: dict[str, float] = defaultdict(float)
    for r in rows:
        mine = by_author.get(r["login"].lower(), []) if r["login"] else []
        day = r["date"]
        active = [p for p in mine if day in p["commit_days"]] or [
            p
            for p in mine
            if (p["created_at"] or "")[:10] <= day <= ((p["closed_at"] or p["merged_at"] or "9999")[:10])
        ]
        if not active:
            unallocated[day] += r["amount_usd"]
            continue
        share = r["amount_usd"] / len(active)
        for p in active:
            per_pr[f"{p['repo']}#{p['number']}"] += share
    return {
        "per_pr": {k: round(v, 4) for k, v in per_pr.items()},
        "unallocated_usd": round(sum(unallocated.values()), 2),
        "unallocated_by_day": {k: round(v, 2) for k, v in sorted(unallocated.items())},
        "total_usd": round(sum(r["amount_usd"] for r in rows), 2),
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
