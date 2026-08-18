"""
Notification fan-out.

Recommended: ntfy.sh - no account, no webhook setup, push straight to your
phone lock screen in about 60 seconds of setup. Discord is the next best if
you already live in it.
"""

from __future__ import annotations

import json
import os
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from typing import Sequence

import httpx

TIER_LABEL = {0: "FAANG+", 1: "Tier 1", 2: "Tier 2", 3: "Tier 3"}
TIER_EMOJI = {0: "🔴", 1: "🟠", 2: "🟡", 3: "⚪"}


def _roles(p: dict) -> list[str]:
    r = p.get("roles")
    if isinstance(r, str):
        try:
            return json.loads(r)
        except json.JSONDecodeError:
            return [r]
    return list(r or [])


def _line(p: dict) -> str:
    roles = "/".join(_roles(p))
    loc = f" - {p['location']}" if p.get("location") else ""
    return (f"{TIER_EMOJI.get(p['tier'], '⚪')} [{roles}] "
            f"{p['company']}: {p['title']}{loc}\n{p['url']}")


def format_batch(postings: Sequence[dict], header: str | None = None) -> str:
    body = "\n\n".join(_line(p) for p in postings)
    if header:
        return f"{header}\n\n{body}"
    return body


def _ascii(s: str) -> str:
    """HTTP header values must be latin-1 clean; ntfy titles are headers."""
    return s.encode("ascii", "ignore").decode("ascii").strip() or "new posting"


# --------------------------------------------------------------------------

def send_ntfy(cfg: dict, postings: Sequence[dict], urgent: bool) -> None:
    topic = cfg.get("topic")
    if not topic:
        return
    server = cfg.get("server", "https://ntfy.sh").rstrip("/")
    n = len(postings)
    top = postings[0]
    title = (f"{top['company']} - {top['title'][:60]}" if n == 1
             else f"{n} new 2027 internships")
    headers = {
        "Title": _ascii(title),
        "Priority": "urgent" if urgent else "default",
        "Tags": "rotating_light" if urgent else "briefcase",
    }
    if n == 1 and top.get("url"):
        headers["Click"] = top["url"]
        headers["Actions"] = f"view, Open posting, {top['url']}"
    r = httpx.post(f"{server}/{topic}", data=format_batch(postings).encode("utf-8"),
                   headers=headers, timeout=15)
    r.raise_for_status()


def send_discord(cfg: dict, postings: Sequence[dict], urgent: bool) -> None:
    hook = cfg.get("webhook_url")
    if not hook:
        return
    embeds = []
    for p in postings[:10]:
        embeds.append({
            "title": f"{p['company']} - {p['title']}"[:250],
            "url": p["url"],
            "color": 0xE01E37 if p["tier"] == 0 else (0xF77F00 if p["tier"] == 1 else 0x8D99AE),
            "fields": [
                {"name": "Roles", "value": "/".join(_roles(p)) or "-", "inline": True},
                {"name": "Location", "value": (p.get("location") or "-")[:100], "inline": True},
                {"name": "Tier", "value": TIER_LABEL.get(p["tier"], "?"), "inline": True},
            ],
        })
    content = ("🚨 **FAANG/Tier-1 posting is live - apply now**" if urgent else
               f"**{len(postings)} new 2027 internship(s)**")
    if len(postings) > 10:
        content += f"\n(+{len(postings) - 10} more - run `radar list`)"
    r = httpx.post(hook, json={"content": content, "embeds": embeds}, timeout=15)
    r.raise_for_status()


def send_slack(cfg: dict, postings: Sequence[dict], urgent: bool) -> None:
    hook = cfg.get("webhook_url")
    if not hook:
        return
    prefix = ("🚨 *FAANG/Tier-1 posting is live*" if urgent else
              f"*{len(postings)} new 2027 internship(s)*")
    r = httpx.post(hook, json={"text": f"{prefix}\n\n{format_batch(postings)}"}, timeout=15)
    r.raise_for_status()


def send_telegram(cfg: dict, postings: Sequence[dict], urgent: bool) -> None:
    token, chat_id = cfg.get("bot_token"), cfg.get("chat_id")
    if not (token and chat_id):
        return
    prefix = ("🚨 FAANG/Tier-1 posting is live" if urgent
              else f"{len(postings)} new posting(s)")
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": f"{prefix}\n\n{format_batch(postings)}",
              "disable_web_page_preview": True},
        timeout=15,
    )
    r.raise_for_status()


def send_email(cfg: dict, postings: Sequence[dict], urgent: bool) -> None:
    host = cfg.get("smtp_host")
    if not host:
        return
    msg = EmailMessage()
    msg["Subject"] = ("URGENT: " if urgent else "") + \
                     f"{len(postings)} new 2027 internship(s)"
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    msg.set_content(format_batch(postings))
    password = cfg.get("smtp_password") or os.environ.get("RADAR_SMTP_PASSWORD", "")
    with smtplib.SMTP(host, cfg.get("smtp_port", 587)) as s:
        s.starttls()
        s.login(cfg.get("smtp_user", cfg["from_addr"]), password)
        s.send_message(msg)


def send_desktop(cfg: dict, postings: Sequence[dict], urgent: bool) -> None:
    n, top = len(postings), postings[0]
    title = f"{top['company']}: {top['title'][:50]}" if n == 1 else f"{n} new internships"
    # Quotes in a title would break out of the osascript string literal.
    safe_title = title.replace('"', "'").replace("\\", "")
    safe_company = str(top["company"]).replace('"', "'")
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe_company}" with title "{safe_title}" '
                 f'sound name "Glass"'],
                check=False, timeout=10)
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", "-u", "critical" if urgent else "normal",
                            title, top["url"]], check=False, timeout=10)
    except Exception:
        pass


def send_console(cfg: dict, postings: Sequence[dict], urgent: bool) -> None:
    bar = "=" * 68
    print(f"\n{bar}\n{'URGENT - ' if urgent else ''}{len(postings)} NEW POSTING(S)\n{bar}")
    print(format_batch(postings))
    print(bar + "\n", flush=True)


CHANNELS = {
    "ntfy": send_ntfy, "discord": send_discord, "slack": send_slack,
    "telegram": send_telegram, "email": send_email,
    "desktop": send_desktop, "console": send_console,
}


def dispatch(notify_cfg: dict, postings: Sequence[dict], urgent: bool) -> list[str]:
    """Fire every enabled channel. Returns names of channels that errored."""
    if not postings:
        return []
    errors = []
    for name, fn in CHANNELS.items():
        cfg = notify_cfg.get(name) or {}
        if not cfg.get("enabled"):
            continue
        try:
            fn(cfg, postings, urgent)
        except Exception as e:  # never let a dead webhook kill the poll loop
            errors.append(f"{name}: {e}")
    return errors
