#!/usr/bin/env bash
# Sync PM/Kanban runtime helper scripts from the repository checkout into the
# active Hermes runtime scripts directory.
#
# These scripts are used by Hermes cron jobs with no_agent=true. The canonical
# copies live in this repository under scripts/pm-workflow/ so local hardening
# changes survive git updates and can be installed on another machine.
#
# Usage:
#   scripts/sync-pm-workflow-runtime.sh
#
# Optional overrides:
#   HERMES_ROOT=$HOME/.hermes
#   HERMES_RUNTIME_SCRIPTS_DIR=$HOME/.hermes/scripts

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SOURCE_DIR="$REPO_ROOT/scripts/pm-workflow"
HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes}"
TARGET_DIR="${HERMES_RUNTIME_SCRIPTS_DIR:-$HERMES_ROOT/scripts}"

log() {
    printf '→ %s\n' "$1"
}

success() {
    printf '✓ %s\n' "$1"
}

fail() {
    printf '✗ %s\n' "$1" >&2
    exit 1
}

[ -d "$SOURCE_DIR" ] || fail "canonical PM workflow script dir not found: $SOURCE_DIR"
mkdir -p "$TARGET_DIR"

scripts=(
    pm_design_ready_notifier.py
    pm_kanban_completion_notifier.py
    pm_kanban_blocked_notifier.py
    pm_scope_phase_sync.py
)

for script in "${scripts[@]}"; do
    [ -f "$SOURCE_DIR/$script" ] || fail "missing canonical script: $SOURCE_DIR/$script"
    log "Installing $script -> $TARGET_DIR/$script"
    install -m 0755 "$SOURCE_DIR/$script" "$TARGET_DIR/$script"
done

success "PM workflow runtime scripts synced to $TARGET_DIR"
cat <<EOF

Cron jobs can reference these script names directly, for example:
  script: pm_design_ready_notifier.py
  script: pm_kanban_completion_notifier.py
  script: pm_kanban_blocked_notifier.py
  script: pm_scope_phase_sync.py

State remains outside git under:
  $HERMES_ROOT/state/
EOF
