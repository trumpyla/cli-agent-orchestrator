"""Exact Claude Code model pin and provider-native reasoning effort.

Claude Code profiles pin exact ``claude-opus-5`` and request the highest effort
the *provider-native CLI* supports. On the 2026-07-25 CLI surface that value is
``xhigh``, exposed as ``/effort ultracode`` and settable via
``CLAUDE_CODE_EFFORT_LEVEL``.

A separate SDK/tool/API effort enumeration also exists and includes a value
named ``max``. That ladder is NOT the CLI's: translating or escalating the CLI
contract to generic ``max`` would request an effort the launched CLI does not
implement, so it must be rejected rather than silently accepted or downgraded.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.providers.claude_native_effort import (
    CLAUDE_CODE_CLI_EFFORT_LEVELS,
    CLAUDE_CODE_EFFORT_ENV_VAR,
    CLAUDE_CODE_MAX_NATIVE_EFFORT,
    CLAUDE_CODE_MAX_NATIVE_EFFORT_COMMAND,
    EXACT_CLAUDE_CODE_MODEL,
    ClaudeNativeEffortError,
    validate_claude_code_effort,
    validate_claude_code_model,
)


class TestNativeCliLadder:
    def test_highest_native_effort_is_xhigh(self) -> None:
        # Derived from the CLI surface, not from the implementation: /effort
        # ultracode is offered only on models supporting the xhigh parameter.
        assert CLAUDE_CODE_MAX_NATIVE_EFFORT == "xhigh"
        assert CLAUDE_CODE_MAX_NATIVE_EFFORT == CLAUDE_CODE_CLI_EFFORT_LEVELS[-1]

    def test_ultracode_is_the_picker_name_for_xhigh(self) -> None:
        assert CLAUDE_CODE_MAX_NATIVE_EFFORT_COMMAND == "/effort ultracode"

    def test_generic_max_is_not_on_the_cli_ladder(self) -> None:
        # Break guarded: the CLI ladder must not gain `max` by absorbing the
        # separate SDK/tool/API enumeration.
        assert "max" not in CLAUDE_CODE_CLI_EFFORT_LEVELS

    def test_effort_env_var_is_the_cli_override(self) -> None:
        assert CLAUDE_CODE_EFFORT_ENV_VAR == "CLAUDE_CODE_EFFORT_LEVEL"


class TestEffortValidation:
    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh"])
    def test_native_levels_accepted(self, level: str) -> None:
        assert validate_claude_code_effort(level) == level

    def test_max_is_rejected_and_names_the_native_value(self) -> None:
        # Break guarded: silently mapping `max` -> `xhigh` would hide a profile
        # authored against the wrong ladder; rejecting it forces the fix.
        with pytest.raises(ClaudeNativeEffortError) as exc_info:
            validate_claude_code_effort("max")
        text = str(exc_info.value)
        assert "max" in text
        assert "xhigh" in text

    @pytest.mark.parametrize(
        "level",
        [
            pytest.param("ultracode", id="picker-label-is-not-a-level"),
            pytest.param("XHIGH", id="case-variant"),
            pytest.param("x-high", id="hyphenated"),
            pytest.param("", id="empty"),
            pytest.param("extreme", id="unknown"),
        ],
    )
    def test_non_native_values_rejected(self, level: str) -> None:
        with pytest.raises(ClaudeNativeEffortError):
            validate_claude_code_effort(level)


class TestExactModelPin:
    def test_exact_model_is_claude_opus_5(self) -> None:
        assert EXACT_CLAUDE_CODE_MODEL == "claude-opus-5"
        assert validate_claude_code_model("claude-opus-5") == "claude-opus-5"

    @pytest.mark.parametrize(
        "model",
        [
            pytest.param("opus", id="bare-alias"),
            pytest.param("claude-opus-latest", id="latest-alias"),
            pytest.param("claude-opus-4-8", id="older-generation-downgrade"),
            pytest.param("claude-sonnet-5", id="different-family"),
            pytest.param("Claude-Opus-5", id="case-variant"),
            pytest.param("", id="empty"),
        ],
    )
    def test_alias_and_downgrade_rejected(self, model: str) -> None:
        # Break guarded: an alias resolves server-side to whatever is current,
        # which is exactly the silent model substitution the spec forbids.
        with pytest.raises(ClaudeNativeEffortError):
            validate_claude_code_model(model)


class TestProfileFixtureContract:
    """A Claude Code launch fixture must satisfy both pins together."""

    def test_valid_fixture_passes_both_pins(self) -> None:
        assert validate_claude_code_model(EXACT_CLAUDE_CODE_MODEL) == "claude-opus-5"
        assert validate_claude_code_effort(CLAUDE_CODE_MAX_NATIVE_EFFORT) == "xhigh"

    def test_fixture_with_generic_max_effort_fails(self) -> None:
        # The precise regression: a fixture authored from the API/tool ladder.
        with pytest.raises(ClaudeNativeEffortError):
            validate_claude_code_effort("max")
