#!/usr/bin/env bash
#
# Atomic installation and removal of the repository-independent runtime.

# Create a target-derived temporary file in the target directory.
cao_service::make_temp_file() {
  local target_path="$1"
  local target_directory target_name
  target_directory="$(dirname "${target_path}")"
  target_name="$(basename "${target_path}")"
  mkdir -p "${target_directory}"
  mktemp "${target_directory}/.${target_name}.tmp.XXXXXX"
}

# Atomically copy a file and expose whether its contents changed.
cao_service::atomic_copy() {
  local source_path="$1"
  local target_path="$2"
  local mode="$3"
  local target_dir temp_path
  target_dir="$(dirname "${target_path}")"
  mkdir -p "${target_dir}"
  temp_path="$(cao_service::make_temp_file "${target_path}")"
  cp "${source_path}" "${temp_path}"
  chmod "${mode}" "${temp_path}"
  cao_file_changed=1
  if [[ -f "${target_path}" ]] && cmp -s "${temp_path}" "${target_path}"; then
    rm -f "${temp_path}"
    cao_file_changed=0
    return
  fi
  mv "${temp_path}" "${target_path}"
}

# Atomically write stdin and expose whether its contents changed.
cao_service::atomic_write() {
  local target_path="$1"
  local mode="$2"
  local target_dir temp_path
  target_dir="$(dirname "${target_path}")"
  mkdir -p "${target_dir}"
  temp_path="$(cao_service::make_temp_file "${target_path}")"
  cat >"${temp_path}"
  chmod "${mode}" "${temp_path}"
  cao_file_changed=1
  if [[ -f "${target_path}" ]] && cmp -s "${temp_path}" "${target_path}"; then
    rm -f "${temp_path}"
    cao_file_changed=0
    return
  fi
  mv "${temp_path}" "${target_path}"
}

# Copy the controller runtime and capture the exact server executable.
cao_service::install_runtime() {
  local source_file relative_file executable
  local changed=0
  local runtime_files=(
    common.sh
    health.sh
    lifecycle.sh
    linux.sh
    macos.sh
    runtime.sh
  )

  mkdir -p "${CAO_RUNTIME_DIR}" "${CAO_RUNTIME_LIB_DIR}"
  chmod 700 "${CAO_RUNTIME_DIR}"
  cao_service::atomic_copy \
    "${SCRIPT_DIR}/cao-service.sh" "${CAO_RUNTIME_DIR}/cao-service.sh" 700
  changed=$((changed | cao_file_changed))

  for relative_file in "${runtime_files[@]}"; do
    source_file="${CAO_SERVICE_LIB_DIR}/${relative_file}"
    cao_service::atomic_copy \
      "${source_file}" "${CAO_RUNTIME_LIB_DIR}/${relative_file}" 600
    changed=$((changed | cao_file_changed))
  done
  cao_service::atomic_copy \
    "${CAO_SERVICE_LIB_DIR}/runner.sh" "${CAO_RUNTIME_RUNNER}" 700
  changed=$((changed | cao_file_changed))

  executable="$(command -v cao-server || true)"
  [[ -n "${executable}" && -x "${executable}" ]] ||
    cao_service::die "cao-server is not installed or executable"
  source_file="$(cao_service::make_temp_file "${CAO_EXECUTABLE_FILE}")"
  printf '%s\n' "${executable}" >"${source_file}"
  cao_service::atomic_copy "${source_file}" "${CAO_EXECUTABLE_FILE}" 600
  rm -f "${source_file}"
  changed=$((changed | cao_file_changed))
  # Consumed by lifecycle.sh after this function returns.
  # shellcheck disable=SC2034
  cao_runtime_changed="${changed}"
}

# Remove only runtime files installed by this controller.
cao_service::remove_runtime() {
  local relative_file
  local runtime_files=(
    common.sh
    health.sh
    lifecycle.sh
    linux.sh
    macos.sh
    runtime.sh
  )
  rm -f \
    "${CAO_RUNTIME_DIR}/cao-service.sh" \
    "${CAO_RUNTIME_RUNNER}" \
    "${CAO_EXECUTABLE_FILE}"
  for relative_file in "${runtime_files[@]}"; do
    rm -f "${CAO_RUNTIME_LIB_DIR}/${relative_file}"
  done
  rmdir "${CAO_RUNTIME_LIB_DIR}" 2>/dev/null || true
  rmdir "${CAO_RUNTIME_DIR}/lib" 2>/dev/null || true
  rmdir "${CAO_RUNTIME_DIR}" 2>/dev/null || true
}
