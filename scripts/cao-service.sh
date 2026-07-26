#!/usr/bin/env bash
#
# Install and control the native per-user CAO service.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
CAO_SERVICE_LIB_DIR="${SCRIPT_DIR}/lib/cao-service"
readonly CAO_SERVICE_LIB_DIR

# shellcheck source=scripts/lib/cao-service/common.sh
source "${CAO_SERVICE_LIB_DIR}/common.sh"
# shellcheck source=scripts/lib/cao-service/runtime.sh
source "${CAO_SERVICE_LIB_DIR}/runtime.sh"
# shellcheck source=scripts/lib/cao-service/health.sh
source "${CAO_SERVICE_LIB_DIR}/health.sh"
# shellcheck source=scripts/lib/cao-service/lifecycle.sh
source "${CAO_SERVICE_LIB_DIR}/lifecycle.sh"

main "$@"
