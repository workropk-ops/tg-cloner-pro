#!/usr/bin/env bash
# TG Clone Pro — GitHub Action entrypoint
#
# Flow:
#   1. Receive tg-cl.tar.gz + config.json via delivery bot (token from secret)
#   2. Forget the delivery token (never pass it to tg-cl)
#   3. Unpack archive → tg-cl/
#   4. Install config.json into tg-cl/
#   5. Run ./tg-cl bot under an idle/exit watchdog
#
# Manual stop : cancel the workflow run in GitHub
# Auto stop   : 15 min sustained inactivity, or tg-cl process exit

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ACTION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="${ACTION_ROOT}/scripts"
WORK_ROOT="${WORK_ROOT:-${RUNNER_TEMP:-/tmp}/tg-cloner-pro}"
DELIVERY_DIR="${WORK_ROOT}/delivery"
EXTRACT_DIR="${WORK_ROOT}/extract"
RUNTIME_DIR=""

# ---------------------------------------------------------------------------
# Defaults (overridable via env / workflow inputs)
# ---------------------------------------------------------------------------
IDLE_TIMEOUT_SECONDS="${IDLE_TIMEOUT_SECONDS:-900}"          # 15 minutes
DELIVERY_TIMEOUT_SECONDS="${DELIVERY_TIMEOUT_SECONDS:-1800}" # 30 minutes
IDLE_POLL_SECONDS="${IDLE_POLL_SECONDS:-30}"
IDLE_NET_NOISE_BYTES="${IDLE_NET_NOISE_BYTES:-4096}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log()  { printf '%s [entrypoint] %s\n' "$(ts)" "$*"; }
die()  { log "ERROR: $*"; exit 1; }
section() {
  # GitHub Actions collapsible log groups
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::group::$*"
  else
    log "==== $* ===="
  fi
}
end_section() {
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::endgroup::"
  fi
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
  local code=$?
  set +e
  if [[ -n "${RUNTIME_DIR}" && -d "${RUNTIME_DIR}" ]]; then
    # Best-effort wipe of secrets on disk after the job
    if [[ -f "${RUNTIME_DIR}/config.json" ]]; then
      shred -u "${RUNTIME_DIR}/config.json" 2>/dev/null \
        || rm -f "${RUNTIME_DIR}/config.json"
    fi
  fi
  if [[ -d "${DELIVERY_DIR}" ]]; then
    rm -f "${DELIVERY_DIR}/config.json" 2>/dev/null || true
  fi
  # Never leave the delivery token exported in the environment for children
  unset TELEGRAM_BOT_TOKEN 2>/dev/null || true
  log "Cleanup finished (exit=${code})"
  exit "${code}"
}
trap cleanup EXIT
trap 'log "Caught SIGINT"; exit 130' INT
trap 'log "Caught SIGTERM"; exit 143' TERM

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
section "Preflight checks"

command -v python3 >/dev/null || die "python3 is required"
command -v tar >/dev/null     || die "tar is required"
command -v gzip >/dev/null    || die "gzip is required"

[[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] \
  || die "TELEGRAM_BOT_TOKEN secret is required (delivery bot only)"

[[ -f "${SCRIPTS_DIR}/receive_files.py" ]] \
  || die "Missing ${SCRIPTS_DIR}/receive_files.py"
[[ -f "${SCRIPTS_DIR}/idle_watchdog.py" ]] \
  || die "Missing ${SCRIPTS_DIR}/idle_watchdog.py"

mkdir -p "${DELIVERY_DIR}" "${EXTRACT_DIR}"
chmod +x "${SCRIPTS_DIR}/receive_files.py" "${SCRIPTS_DIR}/idle_watchdog.py" || true

log "Work root     : ${WORK_ROOT}"
log "Idle timeout  : ${IDLE_TIMEOUT_SECONDS}s"
log "Delivery wait : ${DELIVERY_TIMEOUT_SECONDS}s"
end_section

# ---------------------------------------------------------------------------
# Step 1 — Receive files via delivery bot
# ---------------------------------------------------------------------------
section "Step 1/4 — Receive tg-cl.tar.gz and config.json"

log "Starting delivery bot (token used ONLY for file intake)"
log "Message the bot, then send: 1) tg-cl.tar.gz  2) config.json"

# Capture token only for this step; clear immediately after.
export TELEGRAM_BOT_TOKEN
export DELIVERY_DIR
export DELIVERY_TIMEOUT_SECONDS
export TELEGRAM_ALLOWED_USER_IDS="${TELEGRAM_ALLOWED_USER_IDS:-}"

set +e
python3 "${SCRIPTS_DIR}/receive_files.py" \
  --out-dir "${DELIVERY_DIR}" \
  --timeout "${DELIVERY_TIMEOUT_SECONDS}"
RECEIVE_RC=$?
set -e

# Drop delivery credentials before any runtime starts.
unset TELEGRAM_BOT_TOKEN
export TELEGRAM_BOT_TOKEN=""

[[ "${RECEIVE_RC}" -eq 0 ]] || die "File delivery failed (exit ${RECEIVE_RC})"

ARCHIVE_PATH="${DELIVERY_DIR}/tg-cl.tar.gz"
CONFIG_PATH="${DELIVERY_DIR}/config.json"

[[ -f "${ARCHIVE_PATH}" ]] || die "Archive missing after delivery: ${ARCHIVE_PATH}"
[[ -f "${CONFIG_PATH}"  ]] || die "Config missing after delivery: ${CONFIG_PATH}"

log "Received archive: ${ARCHIVE_PATH} ($(du -h "${ARCHIVE_PATH}" | awk '{print $1}'))"
log "Received config : ${CONFIG_PATH}"
end_section

# ---------------------------------------------------------------------------
# Step 2 — Unpack archive
# ---------------------------------------------------------------------------
section "Step 2/4 — Unpack tg-cl.tar.gz"

rm -rf "${EXTRACT_DIR}"
mkdir -p "${EXTRACT_DIR}"

log "Extracting archive…"
tar -xzf "${ARCHIVE_PATH}" -C "${EXTRACT_DIR}"

# Resolve the runtime directory that contains the tg-cl binary.
# Expected layout from dist: tg-cl/tg-cl  (or tg-cl.dist/tg-cl)
if [[ -x "${EXTRACT_DIR}/tg-cl/tg-cl" ]]; then
  RUNTIME_DIR="${EXTRACT_DIR}/tg-cl"
elif [[ -x "${EXTRACT_DIR}/tg-cl.dist/tg-cl" ]]; then
  RUNTIME_DIR="${EXTRACT_DIR}/tg-cl.dist"
else
  # Fallback: first directory that contains an executable named tg-cl
  FOUND="$(find "${EXTRACT_DIR}" -type f -name tg-cl -perm -u+x 2>/dev/null | head -n1 || true)"
  [[ -n "${FOUND}" ]] || die "Could not find executable 'tg-cl' inside the archive"
  RUNTIME_DIR="$(cd "$(dirname "${FOUND}")" && pwd)"
fi

[[ -x "${RUNTIME_DIR}/tg-cl" ]] || die "tg-cl is not executable at ${RUNTIME_DIR}/tg-cl"
log "Runtime directory: ${RUNTIME_DIR}"
end_section

# ---------------------------------------------------------------------------
# Step 3 — Install config.json into runtime folder
# ---------------------------------------------------------------------------
section "Step 3/4 — Place config.json"

install -m 600 "${CONFIG_PATH}" "${RUNTIME_DIR}/config.json"
log "Installed config.json → ${RUNTIME_DIR}/config.json"

# Remove delivery copies so only the runtime copy remains briefly
rm -f "${CONFIG_PATH}"
# Archive is large and no longer needed
rm -f "${ARCHIVE_PATH}"

# Sanity: binary help (non-fatal)
if "${RUNTIME_DIR}/tg-cl" --help >/dev/null 2>&1; then
  log "Binary responds to --help"
else
  log "WARNING: tg-cl --help returned non-zero (continuing)"
fi
end_section

# ---------------------------------------------------------------------------
# Step 4 — Run ./tg-cl bot under idle / exit supervision
# ---------------------------------------------------------------------------
section "Step 4/4 — Run ./tg-cl bot"

log "Launching TG Clone Pro control bot"
log "Stop conditions:"
log "  • workflow cancelled manually in GitHub"
log "  • no CPU / I/O / network activity for ${IDLE_TIMEOUT_SECONDS}s"
log "  • tg-cl process exits"

# Ensure delivery token is NOT in the environment for the child.
env -u TELEGRAM_BOT_TOKEN \
  python3 "${SCRIPTS_DIR}/idle_watchdog.py" \
    --cwd "${RUNTIME_DIR}" \
    --idle-seconds "${IDLE_TIMEOUT_SECONDS}" \
    --poll-seconds "${IDLE_POLL_SECONDS}" \
    --net-noise-bytes "${IDLE_NET_NOISE_BYTES}" \
    -- \
    ./tg-cl bot

RC=$?
end_section

if [[ "${RC}" -eq 0 ]]; then
  log "TG Clone Pro finished cleanly"
else
  log "TG Clone Pro finished with exit code ${RC}"
fi
exit "${RC}"
