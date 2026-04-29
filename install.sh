#!/usr/bin/env bash
set -euo pipefail

APP_NAME="concord"
INSTALL_DIR="${CONCORD_HOME:-"$HOME/.concord"}"
VENV_DIR="$INSTALL_DIR/.venv"
PACKAGE_URL="${CONCORD_PACKAGE_URL:-"https://concord.slate.ceo/packages/concord_agent_memory-0.1.4.tar.gz"}"
SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
TMP_DIR=""

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

log() {
  printf '%s\n' "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "Missing required command: $1"
    exit 1
  fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  require_cmd curl
  log "uv not found; installing uv with Astral's official installer."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    log "uv install did not place uv on PATH. Add ~/.local/bin or ~/.cargo/bin to PATH and rerun."
    exit 1
  fi
}

install_package() {
  mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/logs"
  chmod 700 "$INSTALL_DIR" "$INSTALL_DIR/logs"

  uv venv --allow-existing "$VENV_DIR"

  if [ -n "$PACKAGE_URL" ]; then
    require_cmd curl
    TMP_DIR="$(mktemp -d -t concord.XXXXXX)"
    tmp_pkg="$TMP_DIR/concord.tar.gz"
    curl -fsSL "$PACKAGE_URL" -o "$tmp_pkg"
    uv pip install --python "$VENV_DIR/bin/python" "$tmp_pkg"
  elif [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    uv pip install --python "$VENV_DIR/bin/python" "$SCRIPT_DIR"
  else
    log "Set CONCORD_PACKAGE_URL to a wheel/tarball URL, or run this installer from the project checkout."
    exit 1
  fi
}

install_hooks() {
  "$VENV_DIR/bin/python" -m concord.install \
    --api-url "${CONCORD_API_URL:-}" \
    --api-token "${CONCORD_API_TOKEN:-}" \
    --team-id "${CONCORD_TEAM_ID:-}" \
    --python "$VENV_DIR/bin/python"
}

main() {
  ensure_uv
  install_package
  install_hooks

  log "$APP_NAME installed."
  log "Concord home: $INSTALL_DIR"
  log "Config: $INSTALL_DIR/config.json"
  log "Installed Codex and Claude Code hooks for pre-turn advice and post-turn transcript upload."
  log "Historical transcript backfill is running in the background."
  log "Backfill status: $INSTALL_DIR/backfill_status.json"
  log "Backfill log: $INSTALL_DIR/logs/backfill.log"
}

main "$@"
