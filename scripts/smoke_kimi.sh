#!/usr/bin/env bash
set -euo pipefail

echo "Running Kimi K3 task-execution smoke test"
uv run cao config set agents.extra_dirs "[\"$(pwd)/.cao/agents\"]"

if uv run cao launch --auto-approve --agents kimi-k3-implementer "pytest test/e2e"; then
    echo "Expected failure, but it succeeded! This claims fake green."
    exit 1
else
    echo "Kimi plumbing blocked as expected. Concrete evidence of failure."
    exit 1
fi
