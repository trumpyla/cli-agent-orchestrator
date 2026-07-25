#!/usr/bin/env make -f
# CLI Agent Orchestrator — maintenance targets.
#
# Offline vendoring of the upstream MCP Apps builder skills
# (modelcontextprotocol/ext-apps). See skills/vendor/ext-apps/README.md.

.PHONY: refresh-ext-apps-skills check-ext-apps-skills

# Re-vendor the ext-apps builder skills from the pinned tag and rewrite NOTICE.
# To move to a newer upstream release, bump PINNED_REF/PINNED_SHA in
# scripts/vendor_ext_apps_skills.py first, then run this target.
refresh-ext-apps-skills:
	uv run python scripts/vendor_ext_apps_skills.py

# Verify the on-disk vendored copy still matches the pin (CI / pre-commit).
# Exit 0 = in sync, 1 = drift, 2 = network-gated (could not verify).
check-ext-apps-skills:
	uv run python scripts/vendor_ext_apps_skills.py --check

.PHONY: sg-test sg-scan

# Structural gates (ast-grep, pinned to 0.44.1 — installed in CI via
# @ast-grep/cli@0.44.1; locally: brew install ast-grep). Rules live in
# rules/python with positive/negative fixtures in rule-tests (sgconfig.yml).
# Validate every rule against its fixtures. Exit 0 = all fixtures behave.
sg-test:
	sg test

# Scan the Python source and tests for the prohibited structural patterns
# (blocking requests in async MCP handlers, detached subscription tasks,
# empty command/args on HTTP MCP entries). Exit 1 on any finding (--error).
sg-scan:
	sg scan --error src/ test/
