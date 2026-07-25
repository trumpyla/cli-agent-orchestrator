"""Exact Claude Code model pin and provider-native reasoning-effort ladder.

Claude Code profiles pin the exact model ``claude-opus-5`` and request the
highest effort the *launched provider-native CLI* supports. On the 2026-07-25
CLI surface that value is ``xhigh``, surfaced in the picker as
``/effort ultracode`` and overridable through ``CLAUDE_CODE_EFFORT_LEVEL``.

A separate SDK/tool/API effort enumeration exists and includes a value named
``max``. It is a different ladder: the CLI does not implement ``max``, so
translating or escalating the CLI contract to it would request an effort the
launched binary cannot honor. Both a generic ``max`` and any model alias are
therefore rejected rather than silently substituted or downgraded — a wrong
model or effort is invisible at runtime, so it has to fail at validation.

Everything here is a pure function of its inputs: no environment, filesystem,
subprocess, or network access.
"""

from __future__ import annotations

from typing import Tuple

#: Effort levels the provider-native Claude Code CLI accepts, lowest first.
#: Deliberately excludes the SDK/tool/API-only ``max``.
CLAUDE_CODE_CLI_EFFORT_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "xhigh")

#: Highest effort the native CLI supports.
CLAUDE_CODE_MAX_NATIVE_EFFORT = CLAUDE_CODE_CLI_EFFORT_LEVELS[-1]

#: How that level is selected interactively. ``ultracode`` is the picker label
#: for ``xhigh``, not an effort value in its own right.
CLAUDE_CODE_MAX_NATIVE_EFFORT_COMMAND = "/effort ultracode"

#: Env var that overrides the ``/effort`` picker for a launched session.
CLAUDE_CODE_EFFORT_ENV_VAR = "CLAUDE_CODE_EFFORT_LEVEL"

#: The exact model identifier. An alias (``opus``, ``claude-opus-latest``)
#: resolves server-side to whatever is current, which is the silent substitution
#: this pin exists to prevent.
EXACT_CLAUDE_CODE_MODEL = "claude-opus-5"

#: Effort names that belong to the separate SDK/tool/API ladder only.
_NON_CLI_EFFORT_LEVELS = frozenset({"max"})


class ClaudeNativeEffortError(ValueError):
    """A Claude Code profile requested a non-native model or effort level."""


def validate_claude_code_effort(level: str) -> str:
    """Return ``level`` iff it is a provider-native Claude Code CLI effort.

    Raises :class:`ClaudeNativeEffortError` for the SDK/tool/API-only ``max``
    (naming both it and the native replacement), for the ``ultracode`` picker
    label, and for any case variant or unknown value.
    """
    if not isinstance(level, str) or not level:
        raise ClaudeNativeEffortError(
            f"Claude Code effort must be one of {', '.join(CLAUDE_CODE_CLI_EFFORT_LEVELS)}"
        )
    if level in _NON_CLI_EFFORT_LEVELS:
        raise ClaudeNativeEffortError(
            f"effort '{level}' belongs to the separate SDK/tool/API ladder, not the "
            f"Claude Code CLI; use the native "
            f"'{CLAUDE_CODE_MAX_NATIVE_EFFORT}' ({CLAUDE_CODE_MAX_NATIVE_EFFORT_COMMAND}) "
            "instead of translating or escalating to it"
        )
    if level not in CLAUDE_CODE_CLI_EFFORT_LEVELS:
        raise ClaudeNativeEffortError(
            f"effort '{level}' is not a provider-native Claude Code CLI level; "
            f"expected one of {', '.join(CLAUDE_CODE_CLI_EFFORT_LEVELS)}"
        )
    return level


def validate_claude_code_model(model: str) -> str:
    """Return ``model`` iff it is exactly :data:`EXACT_CLAUDE_CODE_MODEL`.

    Raises :class:`ClaudeNativeEffortError` for an alias, a different family, an
    older generation, or a case variant — no alias fallback, no downgrade.
    """
    if not isinstance(model, str) or model != EXACT_CLAUDE_CODE_MODEL:
        raise ClaudeNativeEffortError(
            f"Claude Code model must be exactly '{EXACT_CLAUDE_CODE_MODEL}'; "
            f"got '{model}' (aliases and downgrades are rejected)"
        )
    return model
