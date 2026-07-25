# ABOUTME: Tests for the ast-grep structural gates (sgconfig.yml, rules, fixtures).
# ABOUTME: Pins OpenSpec change embed-cao-ops-streamable-http tasks 6.4-6.5: ruleDirs/
# ABOUTME: testConfigs, positive+negative fixtures, Make targets, CI pin on 0.44.1.
"""Structural tests for the ast-grep gate configuration.

Covers the static wiring (``sgconfig.yml``, rule/fixture pairing, Makefile
targets, CI pin) for every environment, plus live ``sg test`` / ``sg scan``
runs when the ast-grep binary is on PATH (mirrors the gitleaks config-test
precedent: skipped in binary-less CI jobs, executed by the dedicated
structural gate job).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SGCONFIG = _REPO_ROOT / "sgconfig.yml"
_RULES_DIR = _REPO_ROOT / "rules" / "python"
_RULE_TESTS_DIR = _REPO_ROOT / "rule-tests"
_MAKEFILE = _REPO_ROOT / "Makefile"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

AST_GREP_PIN = "0.44.1"


def _rules() -> dict:
    """Map rule id -> parsed rule document for every committed rule."""
    rules = {}
    for path in sorted(_RULES_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        rules[doc["id"]] = doc
    return rules


class TestSgConfig:
    def test_sgconfig_exists_and_registers_rule_dirs(self):
        assert _SGCONFIG.is_file()
        config = yaml.safe_load(_SGCONFIG.read_text())
        assert "rules/python" in config.get("ruleDirs", [])

    def test_sgconfig_registers_test_configs(self):
        config = yaml.safe_load(_SGCONFIG.read_text())
        test_dirs = [entry.get("testDir") for entry in config.get("testConfigs", [])]
        assert "rule-tests" in test_dirs

    def test_expected_rules_exist(self):
        rules = _rules()
        assert rules, "no ast-grep rules committed under rules/python"
        for rule_id in (
            "no-blocking-requests-in-async-handler",
            "no-detached-asyncio-task",
            "no-http-mcp-empty-command",
        ):
            assert rule_id in rules, f"missing rule: {rule_id}"

    def test_rules_are_error_severity_python(self):
        for rule_id, doc in _rules().items():
            assert doc.get("language") == "Python", rule_id
            assert doc.get("severity") == "error", rule_id
            assert doc.get("message"), rule_id


class TestRuleFixtures:
    def test_every_rule_has_a_test_file(self):
        for rule_id in _rules():
            test_file = _RULE_TESTS_DIR / f"{rule_id}-test.yml"
            assert test_file.is_file(), f"missing fixture file for {rule_id}"

    def test_every_rule_has_positive_and_negative_cases(self):
        for rule_id in _rules():
            doc = yaml.safe_load((_RULE_TESTS_DIR / f"{rule_id}-test.yml").read_text())
            assert doc.get("id") == rule_id
            assert doc.get("valid"), f"{rule_id} has no negative (allowed) fixtures"
            assert doc.get("invalid"), f"{rule_id} has no positive (prohibited) fixtures"


class TestMakefileTargets:
    def _targets(self) -> str:
        return _MAKEFILE.read_text()

    def test_sg_test_target(self):
        assert re.search(r"^sg-test:\s*$", self._targets(), re.MULTILINE)

    def test_sg_scan_target_scans_with_error_exit(self):
        assert re.search(r"^sg-scan:\s*$", self._targets(), re.MULTILINE)
        assert re.search(r"sg scan --error", self._targets())


class TestCiGate:
    def test_ci_pins_ast_grep_version(self):
        workflow = _CI_WORKFLOW.read_text()
        assert f"@ast-grep/cli@{AST_GREP_PIN}" in workflow

    def test_ci_runs_rule_tests_and_scan(self):
        workflow = _CI_WORKFLOW.read_text()
        assert re.search(r"sg test", workflow)
        assert re.search(r"sg scan --error", workflow)


@pytest.mark.skipif(
    shutil.which("sg") is None,
    reason="ast-grep (sg) binary not on PATH (installed by the structural CI job)",
)
class TestSgBinary:
    def test_sg_test_passes(self):
        result = subprocess.run(
            ["sg", "test"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_sg_scan_reports_zero_findings(self):
        result = subprocess.run(
            ["sg", "scan", "--error", "src/", "test/"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
