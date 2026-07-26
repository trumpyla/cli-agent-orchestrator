# shellcheck disable=SC2154

ORIGINAL_PATH="${PATH}"
REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
SERVICE_SCRIPT="${REPO_ROOT}/scripts/cao-service.sh"
FIXTURE_BIN="${BATS_TEST_DIRNAME}/fixtures/cao-service/bin"

setup_cao_service_test() {
  export FAKE_PLATFORM="$1"
  export TEST_ROOT="${BATS_TEST_TMPDIR}/cao-service"
  export HOME="${TEST_ROOT}/home"
  export CAO_TEST_STATE="${TEST_ROOT}/state"
  export CAO_SERVICE_READY_TIMEOUT=2
  export CAO_SERVICE_READY_INTERVAL=0.05
  export CAO_SERVICE_NATIVE_RETRY_ATTEMPTS=3
  export CAO_SERVICE_NATIVE_RETRY_INTERVAL=0
  export PATH="${FIXTURE_BIN}:${ORIGINAL_PATH}"
  mkdir -p "${HOME}" "${CAO_TEST_STATE}"
}

run_service() {
  run "${SERVICE_SCRIPT}" "$@"
}

assert_success() {
  if [[ "${status}" -ne 0 ]]; then
    printf 'expected success, got status %s\n%s\n' "${status}" "${output}" >&2
    return 1
  fi
}

assert_failure() {
  if [[ "${status}" -eq 0 ]]; then
    printf 'expected failure, got status 0\n%s\n' "${output}" >&2
    return 1
  fi
}

assert_output_contains() {
  if [[ "${output}" != *"$1"* ]]; then
    printf 'expected output to contain %q\nactual: %s\n' "$1" "${output}" >&2
    return 1
  fi
}

refute_output_contains() {
  if [[ "${output}" == *"$1"* ]]; then
    printf 'expected output not to contain %q\nactual: %s\n' "$1" "${output}" >&2
    return 1
  fi
}

assert_file_exists() {
  if [[ ! -e "$1" ]]; then
    printf 'expected file to exist: %s\n' "$1" >&2
    return 1
  fi
}

refute_file_exists() {
  if [[ -e "$1" ]]; then
    printf 'expected file to be absent: %s\n' "$1" >&2
    return 1
  fi
}

assert_file_contains() {
  local path="$1"
  local expected="$2"
  if ! grep -Fq -- "${expected}" "${path}"; then
    printf 'expected %s to contain %q\n' "${path}" "${expected}" >&2
    return 1
  fi
}

manager_call_count() {
  local operation="$1"
  local calls="${CAO_TEST_STATE}/calls"
  if [[ ! -f "${calls}" ]]; then
    printf '0\n'
    return
  fi
  awk -v operation="${operation}" '
        index($0, operation) { count += 1 }
        END { print count + 0 }
    ' "${calls}"
}
