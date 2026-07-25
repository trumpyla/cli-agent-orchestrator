#!/usr/bin/env bash
set -euo pipefail

echo "Running Claude acceptEdits/exact-cwd/callback/no-trust-prompt smoke test"

# Setup temporary config for smoke test
uv run cao config set agents.extra_dirs "[\"$(pwd)/.cao/agents\"]"

uv run cao launch --auto-approve --agents claude-opus-implementer "pwd && load_skill cao-worker-protocols && send_message 'exact cwd and callback'" || {
    echo "Plumbing blocked or failed"
    exit 1
}
echo "Claude smoke passed."
