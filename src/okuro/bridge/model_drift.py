### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Detect drift between the tier→model tables and what each provider CLI actually serves.
# index: imports | normalize_model | class ProbeResult | _run | probe_antigravity | probe_claude | probe_codex | PROBES | check_model_drift | run_model_drift_alarm
# AGENT_HEADER_END -->
"""Model-drift alarm — do the configured models still exist?

``bridge/config.py`` hard-codes a tier→model table per provider. Those strings
were correct when a human ran the CLI once and wrote them down; nothing has
re-checked them since. okuro already runs ``code-drift-alarm``,
``kind-drift-alarm`` and ``canon-drift-alarm`` on exactly this reasoning — a
fact verified once and then trusted indefinitely is a fact that silently
rots. Models were the gap.

Two failure modes, opposite severities
--------------------------------------
``missing``
    A configured model is no longer served. Routing breaks at invoke time, in
    whichever daemon task fires first, with an error that names the model
    rather than the stale table. **crit**.

``unused``
    The CLI serves something no tier points at. Nothing breaks — okuro just
    keeps paying for or running an older model. Measured live on 2026-07-26:
    ``claude`` had gained ``fable`` and ``agy`` had gained the whole
    ``gemini-3.6-flash-*`` family while the quality tier still pointed at
    ``Gemini 3.1 Pro (High)``. **info**.

Providers differ in what they can be asked
------------------------------------------
There is no universal "list models" interface, so each provider declares how
it can be interrogated:

``list``
    The CLI enumerates. ``agy models`` prints one slug per line — cheap,
    complete, no inference.

``validate``
    The CLI has no list command but rejects unknown models before doing any
    work. Verified: ``claude --model definitely-not-a-model -p hi`` returns
    "It may not exist or you may not have access to it" immediately. So each
    candidate is probed with a minimal prompt. This costs a token or two per
    candidate, which is why the alarm is weekly and the candidate list is
    small (DP01).

``none``
    The provider does not select a model by name at all. ``codex`` routes on
    ``model_reasoning_effort=low|medium|high``, so there is no model string to
    drift. Reported as ``skipped``, not as a failure — an unprobeable provider
    must not read as a healthy one.

Name normalization
------------------
The same model is written differently in different places: config carries
``"Gemini 3.5 Flash (High)"`` while ``agy models`` prints
``gemini-3.5-flash-high``. Both are accepted by the CLI (verified by
dispatching each), so this is a formatting difference, NOT a break.
:func:`normalize_model` folds them together; without it every antigravity
model would report as both missing and unused on every run, and the alarm
would be ignored within a week.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Probing a `validate`-style provider costs a real (tiny) inference call per
# candidate, so the prompt is minimal and the timeout tight.
_PROBE_PROMPT = "reply with exactly: OK"
_LIST_TIMEOUT_S = 120
_VALIDATE_TIMEOUT_S = 90

# Claude publishes no list command, so new aliases can only be found by asking
# for them. Keep this list short — every entry is a billable call per run.
_CLAUDE_CANDIDATES = ("haiku", "sonnet", "opus", "fable")


def normalize_model(name: str) -> str:
    """Fold a model name to a comparable slug.

    ``"Gemini 3.5 Flash (High)"`` and ``"gemini-3.5-flash-high"`` are the same
    model in two notations, and the CLI accepts both. Comparing raw strings
    would report every antigravity model as simultaneously missing and unused.
    """
    slug = (name or "").strip().lower()
    slug = slug.replace("(", " ").replace(")", " ")
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


@dataclass
class ProbeResult:
    """What one provider reports about the models it serves."""

    provider: str
    method: str                       # 'list' | 'validate' | 'none'
    served: set[str] = field(default_factory=set)   # normalized
    raw: list[str] = field(default_factory=list)    # as reported
    error: str | None = None
    skipped_reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.error is None and self.skipped_reason is None


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"binary not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)


def probe_antigravity() -> ProbeResult:
    """``agy models`` prints ``<id>\\t<Display Name>`` per model.

    It printed a SINGLE token per line when this probe was written. Since
    (observed 2026-09-06) it prints two tab-separated columns —
    ``gemini-3.8-flash-high\\tGemini 3.8 Flash (High)`` — and folding the whole
    line into one slug yielded ``gemini-3.8-flash-high-gemini-3.8-flash-high``,
    which matches no configured value. Effect: every tier reported missing AND
    every served model reported unused, on a provider that was working. Both
    columns are accepted as valid spellings, because the CLI accepts both.
    """
    code, out, err = _run(["agy", "models"], _LIST_TIMEOUT_S)
    if code != 0:
        return ProbeResult("antigravity", "list", error=err.strip() or f"exit {code}")

    raw: list[str] = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        columns = [c.strip() for c in line.split("\t") if c.strip()]
        if len(columns) >= 2:
            raw.extend(columns[:2])
        elif " " not in line.strip() or "(" in line:
            # Single-column build, or a display name on its own. A prose
            # banner ("Fetching available models...") has spaces and no
            # parenthesised tier, so it is dropped here.
            raw.append(line.strip())
    if not raw:
        return ProbeResult("antigravity", "list", error="no models parsed from output")
    return ProbeResult(
        "antigravity", "list",
        served={normalize_model(m) for m in raw}, raw=raw,
    )


def probe_claude() -> ProbeResult:
    """No list command — probe each candidate alias and keep the accepted ones.

    The CLI validates the model before running inference, so a rejected alias
    costs nothing beyond process startup.
    """
    accepted: list[str] = []
    errors: list[str] = []
    for alias in _CLAUDE_CANDIDATES:
        code, out, err = _run(
            ["claude", "--model", alias, "-p", _PROBE_PROMPT], _VALIDATE_TIMEOUT_S,
        )
        blob = f"{out}\n{err}".lower()
        if "may not exist" in blob or "issue with the selected model" in blob:
            continue                      # cleanly rejected — not served
        if code == 0:
            accepted.append(alias)
        else:
            errors.append(f"{alias}: {(err or out).strip()[:120]}")

    if not accepted:
        return ProbeResult(
            "claude", "validate",
            error="; ".join(errors) or "no candidate alias was accepted",
        )
    return ProbeResult(
        "claude", "validate",
        served={normalize_model(a) for a in accepted}, raw=accepted,
    )


def probe_codex() -> ProbeResult:
    """Codex selects by reasoning effort, not model name — nothing to drift."""
    return ProbeResult(
        "codex", "none",
        skipped_reason=(
            "codex routes on model_reasoning_effort=low|medium|high; "
            "no model string to compare"
        ),
    )


PROBES = {
    "antigravity": probe_antigravity,
    "claude": probe_claude,
    "codex": probe_codex,
}


def check_model_drift(providers: list[str] | None = None) -> dict:
    """Compare each provider's configured tier→model table against reality.

    Returns a per-provider report with ``missing`` (configured but no longer
    served — routing will break) and ``unused`` (served but no tier uses it).
    Pure measurement: emits nothing and changes nothing.
    """
    from okuro.bridge.config import get_provider

    targets = providers or list(PROBES)
    report: dict[str, dict] = {}

    for provider in targets:
        probe = PROBES.get(provider)
        if probe is None:
            report[provider] = {"status": "skipped", "reason": "no probe defined"}
            continue

        try:
            cfg = get_provider(provider) or {}
        except Exception as exc:
            report[provider] = {"status": "probe_failed",
                                "error": f"could not load config: {exc}"}
            continue
        tier_models = cfg.get("models") or {}
        configured = {normalize_model(v): (tier, v) for tier, v in tier_models.items()}

        result = probe()
        if result.skipped_reason:
            report[provider] = {"status": "skipped", "reason": result.skipped_reason}
            continue
        if result.error:
            # An unprobeable provider is explicitly NOT reported as healthy —
            # silence here would be indistinguishable from "no drift".
            report[provider] = {"status": "probe_failed", "error": result.error,
                                "configured": tier_models}
            continue

        missing = [
            {"tier": tier, "configured": raw}
            for slug, (tier, raw) in configured.items()
            if slug not in result.served
        ]
        unused = sorted(result.served - set(configured))

        report[provider] = {
            "status": "drift" if (missing or unused) else "ok",
            "method": result.method,
            "served": sorted(result.served),
            "configured": tier_models,
            "missing": missing,
            "unused": unused,
        }

    return report


def run_model_drift_alarm(providers: list[str] | None = None) -> dict:
    """Daemon entry point: check drift and file a signal per affected provider.

    Severity follows consequence, not novelty: a configured model that has
    vanished breaks dispatch (``crit``), whereas an unused new model is an
    opportunity (``info``).
    """
    from okuro.sense.signals import signal_add

    report = check_model_drift(providers)
    if "error" in report:
        return report

    filed: list[dict] = []
    for provider, data in report.items():
        status = data.get("status")

        if status == "probe_failed":
            filed.append(_file(signal_add, "warn", provider,
                               f"model probe failed for {provider}",
                               data, "Check the CLI is installed and authenticated."))
            continue

        if status != "drift":
            continue

        missing = data.get("missing") or []
        unused = data.get("unused") or []

        if missing:
            tiers = ", ".join(f"{m['tier']}={m['configured']!r}" for m in missing)
            filed.append(_file(
                signal_add, "crit", provider,
                f"{provider}: {len(missing)} configured model(s) no longer served",
                data,
                f"Update bridge/config.py tier→model for {provider}: {tiers} "
                f"no longer appear in the served set {data.get('served')}.",
            ))

        if unused:
            filed.append(_file(
                signal_add, "info", provider,
                f"{provider}: {len(unused)} served model(s) unused by any tier",
                data,
                f"{provider} serves {unused} which no tier routes to. "
                f"Consider whether a tier should adopt one.",
            ))

    return {"report": report, "signals_filed": len(filed), "signals": filed}


def _file(signal_add, severity: str, provider: str, summary: str,
          data: dict, action: str) -> dict:
    try:
        sig = signal_add(
            source="sysmon",
            severity=severity,
            summary=summary,
            source_ref=f"model_drift:{provider}",
            evidence=data,
            suggested_action=action,
        )
        return {"provider": provider, "severity": severity,
                "signal_id": sig.get("id") if isinstance(sig, dict) else None,
                "summary": summary}
    except Exception as exc:
        log.warning("model_drift: signal_add failed (%s)", exc)
        return {"provider": provider, "severity": severity,
                "signal_id": None, "summary": summary, "error": str(exc)}
