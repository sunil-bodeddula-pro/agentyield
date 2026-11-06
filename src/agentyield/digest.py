"""The weekly digest: a short summary per scope (the organisation, or each team), sent to a chat webhook or by email.

It carries totals only, the same as a static copy: no PR titles, no feature titles, no comment text, and restricted
repositories only inside the totals. Configure under `outputs.digest`:

    outputs:
      digest:
        scopes: [org, team:payments]
        webhook_env: AGENTYIELD_WEBHOOK    # a Slack-compatible incoming webhook (Slack, Mattermost, Rocket.Chat)
        email:                             # or plain SMTP
          host: smtp.acme.com
          port: 587
          from: agentyield@acme.com
          to: [eng-leads@acme.com]
          username_env: SMTP_USER
          password_env: SMTP_PASSWORD
        schedule: "Mon 09:00"              # `serve` sends it then, once a week, in metrics.working_hours' time zone
"""

from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _pct(v: Any) -> str:
    return "–" if v is None else f"{v:.0%}"


def _hrs(v: Any) -> str:
    return "–" if v is None else f"{v:.1f}h"


def _arrow(now: Any, was: Any, fmt: Any) -> str:
    if now is None or was is None:
        return str(fmt(now))
    if now == was:
        return f"{fmt(now)} (no change)"
    return f"{fmt(now)} ({'up' if now > was else 'down'} from {fmt(was)})"


def scope_name(scope: str) -> str:
    return (
        "the organisation"
        if scope == "org"
        else (f"team {scope[5:]}" if scope.startswith("team:") else f"repository {scope[5:]}")
    )


TREND = (
    ("Agent share of merged code", "agent_share_of_code", _pct),
    ("PRs merged", "merged", str),
    ("Median working hours to merge", "median_hours_to_merge", _hrs),
    ("Merge rate", "merge_rate", _pct),
)


def compose(summary: dict[str, Any], feedback: dict[str, Any], scope: str) -> str:
    block = (summary.get("scopes") or {}).get(scope)
    if not block:
        return f"AgentYield: no data yet for {scope_name(scope)}."
    c, g = block["change"], block["groups"]
    f = block.get("features") or {}
    lines = [f"AgentYield weekly digest for {scope_name(scope)} (last 4 weeks against the 4 before)", ""]
    for label, key, fmt in TREND:
        lines.append(f"- {label}: {_arrow(c[key]['last_4_weeks'], c[key]['previous_4_weeks'], fmt)}")
    lines.append(
        f"- Agent PRs passing CI on the first push: {_pct(g['agent'].get('ci_first_pass_rate'))}; "
        f"reverted: {_pct(g['agent'].get('revert_rate'))}"
    )
    if f.get("features"):
        lines.append(f"- Features done: {f['done']} (agent-led {f['agent_led_done']}, human-led {f['human_led_done']})")
    shares = g["agent"].get("review_category_share") or {}
    if shares:
        top, share = max(shares.items(), key=lambda kv: kv[1])
        lines.append(f"- What reviewers flag most on agent PRs: {top.replace('_', ' ')} ({share:.0%} of comments)")
    sugg = (feedback.get("scopes") or {}).get(scope) or []
    if sugg:
        lines += [
            "",
            f"Suggested rule for the agents' instructions: {sugg[0]['rule']}",
            f"(Why: {sugg[0]['evidence']}.)",
        ]
    return "\n".join(lines)


def send_webhook(url: str, text: str) -> None:
    req = urllib.request.Request(
        url, data=json.dumps({"text": text}).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def send_email(cfg: dict[str, Any], subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["from"], ", ".join(cfg["to"])
    msg.set_content(text)
    with smtplib.SMTP(str(cfg["host"]), int(cfg.get("port") or 587), timeout=30) as smtp:
        if cfg.get("starttls", True):
            smtp.starttls()
        user, password = (
            os.environ.get(str(cfg.get("username_env") or "")),
            os.environ.get(str(cfg.get("password_env") or "")),
        )
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)


def deliver(digest_cfg: dict[str, Any], scope: str, text: str) -> list[str]:
    """Send to every configured channel. Returns where it went."""
    sent = []
    hook = os.environ.get(str(digest_cfg.get("webhook_env") or "")) if digest_cfg.get("webhook_env") else None
    if hook:
        send_webhook(hook, text)
        sent.append("webhook")
    if digest_cfg.get("email"):
        send_email(digest_cfg["email"], f"AgentYield weekly digest: {scope_name(scope)}", text)
        sent.append("email")
    return sent


def due(schedule: str, now_local: datetime, last_sent: str | None) -> bool:
    """True once the scheduled weekday and time have passed this week and nothing was sent since."""
    day, _, hhmm = schedule.strip().partition(" ")
    target = DAYS.index(day.lower()[:3])
    hour, minute = (int(x) for x in (hhmm or "09:00").split(":"))
    days_back = (now_local.weekday() - target) % 7
    slot = (now_local - timedelta(days=days_back)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > now_local:
        slot -= timedelta(days=7)
    return not last_sent or datetime.fromisoformat(last_sent) < slot
