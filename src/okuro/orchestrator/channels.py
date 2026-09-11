# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: task notification system via Apprise
# index: imports | def load_channel_config | def notify_channel | def notify_via_apprise | def format_notification
# AGENT_HEADER_END -->
"""Okuro Orchestrator Channels — notification system via Apprise."""

import logging
from pathlib import Path
from typing import Optional
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger("okuro.orchestrator.channels")


def load_channel_config(config_path: Optional[Path] = None) -> dict:
    """Load channels.yaml configuration."""
    if config_path and config_path.exists():
        try:
            with open(config_path) as f:
                return yload(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load channel config: {e}")
    return {}


def notify_channel(event_type: str, task_id: str, description: str = "",
                   error: str = "", extra: dict = None):
    """Send a notification for a task event to all enabled output channels."""
    # Try loading config from orchestrator config
    try:
        from okuro.orchestrator.config import load_config
        config = load_config()
        channel_config = load_channel_config(config.channels_config_path)
    except Exception:
        channel_config = {}

    if not channel_config:
        return

    apprise_url = channel_config.get("notifications", {}).get("apprise_url", "http://localhost:13106/notify")
    title, body, notif_type = format_notification(event_type, task_id, description, error, extra)

    for channel_name, channel_cfg in channel_config.get("channels", {}).items():
        if not channel_cfg.get("enabled", False):
            continue

        output = channel_cfg.get("output", {})
        notifications = channel_cfg.get("notifications", {})

        event_key = f"on_{event_type}"
        if not notifications.get(event_key, False):
            continue

        if output.get("type") == "websocket":
            continue

        if output.get("type") == "apprise" and output.get("url"):
            notify_via_apprise(apprise_url, output["url"], title, body, notif_type)


def notify_via_apprise(apprise_url: str, target_url: str, title: str, body: str,
                       notif_type: str = "info"):
    try:
        import urllib.request
        import json

        payload = json.dumps({
            "urls": [target_url],
            "title": title,
            "body": body,
            "type": notif_type,
        }).encode("utf-8")

        req = urllib.request.Request(
            apprise_url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                logger.info(f"Apprise notification sent: {title}")
            else:
                logger.warning(f"Apprise returned {resp.status} for: {title}")
    except Exception as e:
        logger.warning(f"Failed to send Apprise notification: {e}")


def format_notification(event_type: str, task_id: str, description: str = "",
                        error: str = "", extra: dict = None):
    extra = extra or {}

    if event_type == "task_complete":
        return ("Okuro: Task Complete",
                f"Task '{description}' completed successfully.", "success")
    elif event_type == "task_failed":
        return ("Okuro: Task Failed",
                f"Task '{description}' failed: {error}", "failure")
    elif event_type == "phase_complete":
        phase = extra.get("phase", "?")
        phase_name = extra.get("phase_name", "")
        return (f"Okuro: Phase {phase} Complete",
                f"Phase {phase} ({phase_name}) of '{description}' completed.", "success")
    elif event_type == "approval_needed":
        subtask = extra.get("subtask_id", "?")
        risk = extra.get("risk", "MED")
        return ("Okuro: Approval Needed",
                f"Subtask {subtask} ({risk} risk) needs approval.\nTask: {description}", "warning")
    elif event_type == "needs_decision":
        # ROCK-SOLID v5 P1.3 — a recoverable park is not a failure. Was
        # "task_failed" ("Okuro: Task Failed") for a blocked_review-class
        # park (evidence inventory mismatch #12, 2026-07-31): worst instance
        # of the alarm-leaking class because it escapes the UI entirely.
        return ("Okuro: needs your decision",
                f"Task '{description}' is waiting on you: {error}", "warning")
    else:
        return (f"Okuro: {event_type}", f"Task {task_id}: {description}", "info")
