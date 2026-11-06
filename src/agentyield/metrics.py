"""The metrics engine: per-PR metrics, then rollups for the organisation, each team and each repository, split by
who wrote the work (agent, assisted, human), compared on matched work, and trended week by week.

Everything here is computed from stored facts and attribution, with no GitHub calls. Times between events are
measured in working hours (`metrics.working_hours`), so weekends and nights don't inflate review times. A rate
or median computed from fewer than `metrics.min_sample` PRs is reported as None, with its sample size, rather
than as a misleading number.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GROUPS = ("all", "agent", "assisted", "human")
SIZE_BANDS = ((10, "XS"), (50, "S"), (250, "M"), (1000, "L"))
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DURATION = re.compile(r"^(\d+)([mhdw])$")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def duration(text: str, default: timedelta) -> timedelta:
    m = DURATION.match(text or "")
    if not m:
        return default
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def zone(name: str) -> tzinfo:
    """A time zone by name. UTC needs no database; others need the system's, or the tzdata package on Windows."""
    if name.upper() in ("UTC", "Z", "ETC/UTC"):
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise SystemExit(
            f"unknown time zone {name!r} in metrics.working_hours (on Windows: pip install tzdata)"
        ) from None


@dataclass
class WorkingHours:
    tz: tzinfo = UTC
    days: frozenset[int] = frozenset(range(5))
    start: time = time(9)
    end: time = time(18)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> WorkingHours:
        raw = raw or {}
        days_text = str(raw.get("days") or "Mon-Fri").lower()
        days: set[int] = set()
        for part in days_text.split(","):
            a, _, b = part.strip().partition("-")
            ia, ib = DAY_NAMES.index(a[:3]), DAY_NAMES.index((b or a)[:3])
            days.update(range(ia, ib + 1) if ia <= ib else [*range(ia, 7), *range(ib + 1)])
        return cls(
            zone(str(raw.get("timezone") or "UTC")),
            frozenset(days),
            time.fromisoformat(str(raw.get("start") or "09:00")),
            time.fromisoformat(str(raw.get("end") or "18:00")),
        )

    def between(self, a: datetime, b: datetime) -> float:
        """Working hours from a to b."""
        if b <= a:
            return 0.0
        a, b = a.astimezone(self.tz), b.astimezone(self.tz)
        total, day = 0.0, a.date()
        while day <= b.date():
            if day.weekday() in self.days:
                lo = max(a, datetime.combine(day, self.start, self.tz))
                hi = min(b, datetime.combine(day, self.end, self.tz))
                if hi > lo:
                    total += (hi - lo).total_seconds() / 3600
            day += timedelta(days=1)
        return round(total, 2)


@dataclass
class Settings:
    hours: WorkingHours
    revert_window: timedelta
    follow_up_window: timedelta
    min_sample: int
    feature_labels: list[str]
    user_facing_types: list[str]

    @classmethod
    def from_config(cls, metrics: dict[str, Any], functionality: dict[str, Any]) -> Settings:
        return cls(
            WorkingHours.from_config(metrics.get("working_hours")),
            duration(str(metrics.get("revert_window") or ""), timedelta(days=30)),
            duration(str(metrics.get("follow_up_window") or ""), timedelta(days=14)),
            int(metrics.get("min_sample") or 10),
            [x.lower() for x in functionality.get("feature_labels") or ["feature", "enhancement"]],
            [x.lower() for x in functionality.get("user_facing_types") or ["feat", "fix"]],
        )


def size_band(lines: int) -> str:
    return next((name for limit, name in SIZE_BANDS if lines < limit), "XL")


def week_of(d: datetime) -> str:
    monday: date = d.astimezone(UTC).date() - timedelta(days=d.astimezone(UTC).weekday())
    return monday.isoformat()


def change_type(rec: dict[str, Any]) -> str:
    """The PR's conventional-commit type: from its title, else the most common type among its commits."""
    if rec.get("title_type"):
        return str(rec["title_type"])
    types = [c.get("type") for c in rec.get("commits") or [] if c.get("type")]
    return max(set(types), key=types.count) if types else ""


def agent_lines(rec: dict[str, Any], attr: dict[str, Any]) -> float:
    lines = rec_lines(rec)
    if attr["label"] == "agent" and attr.get("share") is None:
        return float(lines)
    return lines * float(attr.get("share") or 0) if attr["label"] in ("agent", "assisted") else 0.0


def rec_lines(rec: dict[str, Any]) -> int:
    if isinstance(rec.get("lines"), int):
        return int(rec["lines"])
    return int(rec.get("additions") or 0) + int(rec.get("deletions") or 0)


def pr_metrics(
    rec: dict[str, Any],
    attr: dict[str, Any],
    repo_prs: dict[int, dict[str, Any]],
    issues: dict[int, dict[str, Any]],
    releases: list[dict[str, Any]],
    s: Settings,
) -> dict[str, Any]:
    created, ready = parse_ts(rec.get("created_at")), parse_ts(rec.get("ready_at") or rec.get("created_at"))
    first_review, merged = parse_ts(rec.get("first_review_at")), parse_ts(rec.get("merged_at"))
    lines = rec_lines(rec)
    reviews = rec.get("reviews") or []
    human_approved = any(r["human"] and r["state"] == "approved" for r in reviews)
    kind = change_type(rec)
    labels = [lb.lower() for lb in rec.get("labels") or []]

    reverted = follow_up = False
    if merged:
        for other in repo_prs.values():
            om = parse_ts(other.get("merged_at"))
            if not om or other["number"] == rec["number"] or om <= merged:
                continue
            if other.get("reverts") == rec["number"] and om - merged <= s.revert_window:
                reverted = True
            if (
                om - merged <= s.follow_up_window
                and change_type(other) == "fix"
                and set(other.get("files") or []) & set(rec.get("files") or [])
            ):
                follow_up = True

    closed_issues = [n for n in rec.get("closes") or [] if (issues.get(n) or {}).get("state") == "closed"]
    feature_issues = [
        n
        for n in closed_issues
        if {lb.lower() for lb in issues[n].get("labels") or []} & set(s.feature_labels)
        or str(issues[n].get("type", "")).lower() == "feature"
    ]
    is_feature = bool(feature_issues) or bool(set(labels) & set(s.feature_labels))
    release = next(
        (
            r
            for r in sorted(releases, key=lambda r: r["published_at"])
            if merged and not r.get("draft") and (parse_ts(r["published_at"]) or merged) >= merged
        ),
        None,
    )
    return {
        "number": rec["number"],
        "repo": rec["repo"],
        "team": rec.get("team", ""),
        "group": attr["label"],
        "agent": attr.get("agent"),
        "state": rec.get("state"),
        "merged": bool(merged),
        "closed_unmerged": rec.get("state") == "closed" and not merged,
        "created_week": week_of(created) if created else None,
        "merged_week": week_of(merged) if merged else None,
        "lines": lines,
        "agent_lines": round(agent_lines(rec, attr), 1),
        "size": size_band(lines),
        "hours_to_first_review": s.hours.between(ready, first_review) if ready and first_review else None,
        "hours_to_merge": s.hours.between(ready, merged) if ready and merged else None,
        "calendar_hours_to_merge": round((merged - ready).total_seconds() / 3600, 2) if ready and merged else None,
        "commits": (rec.get("summary") or {}).get("commits", 0),
        "commits_after_first_review": (rec.get("summary") or {}).get("commits_after_first_review", 0),
        "review_iterations": (rec.get("summary") or {}).get("review_iterations", 0),
        "changes_requested": (rec.get("summary") or {}).get("changes_requested", 0),
        "force_pushes": (rec.get("summary") or {}).get("force_pushes", 0),
        "human_approved": human_approved,
        "self_merged": bool(merged) and rec.get("merged_by") == rec.get("author") and not human_approved,
        "reverted": reverted,
        "follow_up_fix": follow_up,
        "change_type": kind,
        "user_facing": kind in s.user_facing_types or is_feature,
        "issues_closed": len(closed_issues),
        "feature": is_feature,
        "release": release["tag"] if release else None,
        "hours_to_release": round(((parse_ts(release["published_at"]) or merged) - merged).total_seconds() / 3600, 2)
        if release and merged
        else None,
    }


# ------------------------------------------------------------------ rollups
def _median(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def _p85(values: Iterable[float | None]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return round(vals[min(len(vals) - 1, round(0.85 * (len(vals) - 1)))], 2)


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def summarize(prs: list[dict[str, Any]], min_sample: int) -> dict[str, Any]:
    merged = [p for p in prs if p["merged"]]
    unmerged = [p for p in prs if p["closed_unmerged"]]
    decided = len(merged) + len(unmerged)
    small = decided < min_sample

    def gated(v: float | None, n: int) -> float | None:
        return None if n < min_sample else v

    lines = sum(p["lines"] for p in merged)
    out = {
        "prs": len(prs),
        "merged": len(merged),
        "closed_unmerged": len(unmerged),
        "merge_rate": gated(_rate(len(merged), decided), decided),
        "lines_merged": lines,
        "agent_lines_merged": round(sum(p["agent_lines"] for p in merged), 1),
        "agent_share_of_code": _rate(round(sum(p["agent_lines"] for p in merged)), lines) if lines else None,
        "median_hours_to_first_review": gated(
            _median(p["hours_to_first_review"] for p in prs),
            sum(1 for p in prs if p["hours_to_first_review"] is not None),
        ),
        "median_hours_to_merge": gated(_median(p["hours_to_merge"] for p in merged), len(merged)),
        "p85_hours_to_merge": gated(_p85(p["hours_to_merge"] for p in merged), len(merged)),
        "median_review_iterations": gated(_median(p["review_iterations"] for p in merged), len(merged)),
        "commits_after_first_review_per_pr": gated(
            _rate(sum(p["commits_after_first_review"] for p in merged), len(merged)), len(merged)
        ),
        "force_pushes_per_pr": gated(_rate(sum(p["force_pushes"] for p in merged), len(merged)), len(merged)),
        "human_review_coverage": gated(_rate(sum(p["human_approved"] for p in merged), len(merged)), len(merged)),
        "self_merge_rate": gated(_rate(sum(p["self_merged"] for p in merged), len(merged)), len(merged)),
        "revert_rate": gated(_rate(sum(p["reverted"] for p in merged), len(merged)), len(merged)),
        "follow_up_fix_rate": gated(_rate(sum(p["follow_up_fix"] for p in merged), len(merged)), len(merged)),
        "user_facing_share": gated(_rate(sum(p["user_facing"] for p in merged), len(merged)), len(merged)),
        "issues_closed": sum(p["issues_closed"] for p in merged),
        "features_shipped": sum(p["feature"] for p in merged),
        "released": sum(1 for p in merged if p["release"]),
        "median_hours_to_release": _median(p["hours_to_release"] for p in merged),
        "small_sample": small,
    }
    return out


MATCHED = ("median_hours_to_merge", "merge_rate", "median_review_iterations", "revert_rate", "follow_up_fix_rate")


def matched(prs: list[dict[str, Any]]) -> dict[str, Any]:
    """Human work reweighted to look like agent work: within each (repository, size band), compare agent PRs with
    human PRs, then weight each stratum by its share of agent PRs. Agents tend to take smaller, easier tasks; this
    keeps that from flattering them."""
    strata: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for p in prs:
        if p["group"] in ("agent", "human"):
            strata[(p["repo"], p["size"])][p["group"]].append(p)
    usable = {k: v for k, v in strata.items() if v["agent"] and v["human"]}
    total_agent = sum(len(v["agent"]) for v in usable.values())
    all_agent = sum(len(v["agent"]) for v in strata.values())
    out: dict[str, Any] = {"agent_prs_matched": total_agent, "agent_prs": all_agent, "metrics": {}}
    if not total_agent:
        return out
    for metric in MATCHED:
        agent_val = human_val = 0.0
        weight = 0.0
        for v in usable.values():
            a, h = summarize(v["agent"], 1)[metric], summarize(v["human"], 1)[metric]
            if a is None or h is None:
                continue
            w = len(v["agent"]) / total_agent
            agent_val, human_val, weight = agent_val + w * a, human_val + w * h, weight + w
        if weight:
            out["metrics"][metric] = {"agent": round(agent_val / weight, 3), "human": round(human_val / weight, 3)}
    return out


def weekly(prs: list[dict[str, Any]], weeks: list[str], min_sample: int) -> list[dict[str, Any]]:
    """Rolling 4-week figures for each week: what the last four weeks looked like as of that week."""
    rows = []
    for i, wk in enumerate(weeks):
        window = set(weeks[max(0, i - 3) : i + 1])
        merged_in = [p for p in prs if p["merged_week"] in window]
        decided_in = [
            p for p in prs if (p["merged_week"] in window) or (p["closed_unmerged"] and p["created_week"] in window)
        ]
        row: dict[str, Any] = {"week": wk}
        for g in GROUPS:
            sel = [p for p in decided_in if g == "all" or p["group"] == g]
            s = summarize(sel, min_sample)
            row[g] = {
                k: s[k]
                for k in (
                    "merged",
                    "merge_rate",
                    "median_hours_to_merge",
                    "agent_share_of_code",
                    "commits_after_first_review_per_pr",
                    "features_shipped",
                )
            }
        row["merged_prs"] = len(merged_in)
        rows.append(row)
    return rows


def week_range(end: datetime, n: int) -> list[str]:
    last = datetime.fromisoformat(week_of(end)).date()
    return [(last - timedelta(weeks=n - 1 - i)).isoformat() for i in range(n)]


def rollup(prs: list[dict[str, Any]], min_sample: int, now: datetime, n_weeks: int = 12) -> dict[str, Any]:
    counted = [p for p in prs if p["group"] != "excluded"]
    scopes: dict[str, list[dict[str, Any]]] = {"org": counted}
    for p in counted:
        scopes.setdefault(f"repo:{p['repo']}", []).append(p)
        if p["team"]:
            scopes.setdefault(f"team:{p['team']}", []).append(p)
    weeks = week_range(now, n_weeks)
    out: dict[str, Any] = {}
    for scope, sel in sorted(scopes.items()):
        groups = {g: summarize([p for p in sel if g == "all" or p["group"] == g], min_sample) for g in GROUPS}
        trend = weekly(sel, weeks, min_sample)
        out[scope] = {"groups": groups, "matched": matched(sel), "weekly": trend, "change": change(sel, now)}
    return out


def change(prs: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """The last four weeks against the four before: are we shipping more, faster?"""
    last = set(week_range(now, 4))
    prev = set(week_range(now - timedelta(weeks=4), 4))

    def pick(weeks: set[str]) -> dict[str, Any]:
        sel = [p for p in prs if p["merged_week"] in weeks or (p["closed_unmerged"] and p["created_week"] in weeks)]
        return summarize(sel, 1)

    a, b = pick(last), pick(prev)
    keys = ("merged", "agent_share_of_code", "median_hours_to_merge", "merge_rate", "features_shipped")
    return {k: {"last_4_weeks": a[k], "previous_4_weeks": b[k]} for k in keys}
