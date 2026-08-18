"""Notification routing and secret-handling tests."""

import json

import pytest

from radar import notify

POSTING = {
    "company": "Acme", "tier": 0, "title": "SWE Intern, Summer 2027",
    "url": "https://x/1", "location": "Seattle, WA", "roles": json.dumps(["SWE"]),
}


# ------------------------------------------------------------- formatting

def test_line_renders_roles_from_json():
    line = notify._line(POSTING)
    assert "[SWE]" in line and "Acme" in line and "https://x/1" in line


def test_line_accepts_roles_as_list():
    assert "[SWE/MLE]" in notify._line({**POSTING, "roles": ["SWE", "MLE"]})


def test_format_batch_joins_postings():
    body = notify.format_batch([POSTING, POSTING])
    assert body.count("Acme") == 2


def test_format_batch_header():
    assert notify.format_batch([POSTING], header="2 new").startswith("2 new")


def test_ascii_strips_emoji_for_headers():
    """ntfy puts the title in an HTTP header, which must be latin-1 clean."""
    out = notify._ascii("🚨 Acme - SWE Intern")
    assert out.isascii() and "Acme" in out


def test_ascii_never_returns_empty():
    assert notify._ascii("🚨🔴") == "new posting"


# --------------------------------------------------------- env overrides

def test_ntfy_topic_from_env(monkeypatch):
    monkeypatch.setenv("RADAR_NTFY_TOPIC", "secret-topic-123")
    cfg = notify.apply_env_overrides({})
    assert cfg["ntfy"]["enabled"] is True
    assert cfg["ntfy"]["topic"] == "secret-topic-123"


def test_discord_webhook_from_env(monkeypatch):
    monkeypatch.setenv("RADAR_DISCORD_WEBHOOK", "https://hook")
    cfg = notify.apply_env_overrides({})
    assert cfg["discord"] == {"enabled": True, "webhook_url": "https://hook"}


def test_env_overrides_beat_config_file(monkeypatch):
    monkeypatch.setenv("RADAR_NTFY_TOPIC", "from-env")
    cfg = notify.apply_env_overrides({"ntfy": {"enabled": False, "topic": "from-file"}})
    assert cfg["ntfy"]["topic"] == "from-env"


def test_no_env_leaves_config_untouched(monkeypatch):
    monkeypatch.delenv("RADAR_NTFY_TOPIC", raising=False)
    monkeypatch.delenv("RADAR_DISCORD_WEBHOOK", raising=False)
    original = {"ntfy": {"enabled": False, "topic": "x"}}
    assert notify.apply_env_overrides(original) == original


def test_overrides_do_not_mutate_caller_config(monkeypatch):
    monkeypatch.setenv("RADAR_NTFY_TOPIC", "t")
    original = {"ntfy": {"enabled": False}}
    notify.apply_env_overrides(original)
    assert original["ntfy"]["enabled"] is False


# -------------------------------------------------------------- dispatch

def test_dispatch_skips_disabled_channels(monkeypatch):
    called = []
    monkeypatch.setattr(notify, "CHANNELS",
                        {"fake": lambda cfg, p, u: called.append(1)})
    notify.dispatch({"fake": {"enabled": False}}, [POSTING], urgent=True)
    assert called == []


def test_dispatch_fires_enabled_channel(monkeypatch):
    called = []
    monkeypatch.setattr(notify, "CHANNELS",
                        {"fake": lambda cfg, p, u: called.append(u)})
    notify.dispatch({"fake": {"enabled": True}}, [POSTING], urgent=True)
    assert called == [True]


def test_dispatch_survives_a_dead_channel(monkeypatch):
    """A broken webhook must never kill the poll loop."""
    def boom(cfg, p, u):
        raise RuntimeError("webhook down")
    monkeypatch.setattr(notify, "CHANNELS", {"fake": boom})
    errors = notify.dispatch({"fake": {"enabled": True}}, [POSTING], urgent=False)
    assert len(errors) == 1 and "webhook down" in errors[0]


def test_dispatch_noop_on_empty():
    assert notify.dispatch({"console": {"enabled": True}}, [], urgent=True) == []
