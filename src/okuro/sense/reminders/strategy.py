# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Behavioral Strategy Resolver.
# index: imports | class CascadeStep | class BehavioralStrategy | def compute_strategy | def _merge_overrides
# AGENT_HEADER_END -->
"""Behavioral Strategy Resolver.

Pure function: profile -> BehavioralStrategy.
Reads neurotype, work hours, and behavioral_overrides from the user profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CascadeStep:
    """A single step in a reminder cascade pattern."""
    offset_minutes: int          # negative = before due time (-60 = 1h before)
    channels: list[str]          # ["desktop", "display"]
    format: str = "brief"        # "brief" | "checklist" | "full"


@dataclass
class BehavioralStrategy:
    """Profile-derived behavioral parameters for all adaptive subsystems."""

    # Reminder cascade
    cascade_pattern: list[CascadeStep] = field(default_factory=list)
    escalation_interval_min: int = 60
    snooze_default_min: int = 60
    quiet_hours_override_urgency: int = 5

    # Channel selection: urgency -> channels
    channel_map: dict[int, list[str]] = field(default_factory=dict)

    # Attention support
    context_switch_alert_min: int = 0
    focus_session_max_hours: float = 0
    stale_project_days: int = 7
    stale_thought_days: int = 7
    re_entry_reminders: bool = False

    # Communication
    context_format: str = "full"
    suggestion_aggression: str = "low"

    # Routine
    protect_routines: bool = False
    transition_buffer_min: int = 0


NEUROTYPE_DEFAULTS: dict[str, dict] = {
    "adhd": {
        "cascade_pattern": [
            CascadeStep(offset_minutes=-1440, channels=["bootstrap"], format="brief"),
            CascadeStep(offset_minutes=-120, channels=["desktop"], format="brief"),
            CascadeStep(offset_minutes=-30, channels=["desktop"], format="brief"),
            CascadeStep(offset_minutes=-5, channels=["desktop"], format="brief"),
        ],
        "escalation_interval_min": 15,
        "snooze_default_min": 15,
        "quiet_hours_override_urgency": 5,
        "channel_map": {
            1: ["bootstrap"],
            2: ["desktop"],
            3: ["desktop"],
            4: ["desktop"],
            5: ["desktop"],
        },
        "context_switch_alert_min": 10,
        "focus_session_max_hours": 3.0,
        "stale_project_days": 3,
        "stale_thought_days": 3,
        "re_entry_reminders": True,
        "context_format": "brief",
        "suggestion_aggression": "high",
        "protect_routines": False,
        "transition_buffer_min": 10,
    },
    "autistic": {
        "cascade_pattern": [
            CascadeStep(offset_minutes=-1440, channels=["bootstrap"], format="checklist"),
            CascadeStep(offset_minutes=-60, channels=["desktop"], format="checklist"),
            CascadeStep(offset_minutes=-15, channels=["desktop"], format="checklist"),
        ],
        "escalation_interval_min": 0,
        "snooze_default_min": 30,
        "quiet_hours_override_urgency": 99,
        "channel_map": {
            1: ["bootstrap"],
            2: ["desktop"],
            3: ["desktop"],
            4: ["desktop"],
            5: ["desktop"],
        },
        "context_switch_alert_min": 15,
        "focus_session_max_hours": 0,
        "stale_project_days": 5,
        "stale_thought_days": 5,
        "re_entry_reminders": False,
        "context_format": "checklist",
        "suggestion_aggression": "moderate",
        "protect_routines": True,
        "transition_buffer_min": 15,
    },
    "adhd_autistic": {
        "cascade_pattern": [
            CascadeStep(offset_minutes=-1440, channels=["bootstrap"], format="checklist"),
            CascadeStep(offset_minutes=-120, channels=["desktop"], format="checklist"),
            CascadeStep(offset_minutes=-30, channels=["desktop"], format="checklist"),
            CascadeStep(offset_minutes=-5, channels=["desktop"], format="brief"),
        ],
        "escalation_interval_min": 20,
        "snooze_default_min": 15,
        "quiet_hours_override_urgency": 5,
        "channel_map": {
            1: ["bootstrap"],
            2: ["desktop"],
            3: ["desktop"],
            4: ["desktop"],
            5: ["desktop"],
        },
        "context_switch_alert_min": 15,
        "focus_session_max_hours": 3.0,
        "stale_project_days": 3,
        "stale_thought_days": 3,
        "re_entry_reminders": True,
        "context_format": "checklist",
        "suggestion_aggression": "high",
        "protect_routines": True,
        "transition_buffer_min": 15,
    },
    "neurotypical": {
        "cascade_pattern": [
            CascadeStep(offset_minutes=-1440, channels=["bootstrap"], format="full"),
            CascadeStep(offset_minutes=-60, channels=["desktop"], format="full"),
        ],
        "escalation_interval_min": 60,
        "snooze_default_min": 60,
        "quiet_hours_override_urgency": 4,
        "channel_map": {
            1: ["bootstrap"],
            2: ["desktop"],
            3: ["desktop"],
            4: ["desktop"],
            5: ["desktop"],
        },
        "context_switch_alert_min": 0,
        "focus_session_max_hours": 0,
        "stale_project_days": 7,
        "stale_thought_days": 7,
        "re_entry_reminders": False,
        "context_format": "full",
        "suggestion_aggression": "low",
        "protect_routines": False,
        "transition_buffer_min": 0,
    },
}


def compute_strategy(profile: dict) -> BehavioralStrategy:
    """Compute behavioral strategy from user profile.

    Reads cognitive_style.neurotype to select base defaults,
    then applies any behavioral_overrides from the profile.
    """
    cognitive = profile.get("cognitive_style", {})
    neurotypes = [n.lower() for n in cognitive.get("neurotype", [])]

    if "adhd" in neurotypes and "autistic" in neurotypes:
        base_key = "adhd_autistic"
    elif "adhd" in neurotypes:
        base_key = "adhd"
    elif "autistic" in neurotypes:
        base_key = "autistic"
    else:
        base_key = "neurotypical"

    defaults = NEUROTYPE_DEFAULTS[base_key]

    strategy = BehavioralStrategy(
        cascade_pattern=defaults["cascade_pattern"],
        escalation_interval_min=defaults["escalation_interval_min"],
        snooze_default_min=defaults["snooze_default_min"],
        quiet_hours_override_urgency=defaults["quiet_hours_override_urgency"],
        channel_map=defaults["channel_map"],
        context_switch_alert_min=defaults["context_switch_alert_min"],
        focus_session_max_hours=defaults["focus_session_max_hours"],
        stale_project_days=defaults["stale_project_days"],
        stale_thought_days=defaults["stale_thought_days"],
        re_entry_reminders=defaults["re_entry_reminders"],
        context_format=defaults["context_format"],
        suggestion_aggression=defaults["suggestion_aggression"],
        protect_routines=defaults["protect_routines"],
        transition_buffer_min=defaults["transition_buffer_min"],
    )

    overrides = profile.get("behavioral_overrides", {})
    if overrides:
        _merge_overrides(strategy, overrides)

    return strategy


def _merge_overrides(strategy: BehavioralStrategy, overrides: dict) -> None:
    """Apply user-specified overrides onto the strategy (mutates in place)."""
    scalar_fields = {
        "escalation_interval_min", "snooze_default_min",
        "quiet_hours_override_urgency", "context_switch_alert_min",
        "focus_session_max_hours", "stale_project_days",
        "stale_thought_days", "re_entry_reminders", "context_format",
        "suggestion_aggression", "protect_routines", "transition_buffer_min",
    }

    for key, value in overrides.items():
        if key in scalar_fields and hasattr(strategy, key):
            setattr(strategy, key, value)
        elif key == "channel_map" and isinstance(value, dict):
            for urgency_str, channels in value.items():
                try:
                    urgency = int(urgency_str)
                    if isinstance(channels, list):
                        strategy.channel_map[urgency] = channels
                except (ValueError, TypeError):
                    continue
        elif key == "cascade_pattern" and isinstance(value, list):
            steps = []
            for item in value:
                if isinstance(item, dict) and "offset_minutes" in item and "channels" in item:
                    steps.append(CascadeStep(
                        offset_minutes=item["offset_minutes"],
                        channels=item["channels"],
                        format=item.get("format", "brief"),
                    ))
            if steps:
                strategy.cascade_pattern = steps
