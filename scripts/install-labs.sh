#!/usr/bin/env bash
# Install/update Hermes Agent from JaeCheon8587/hermes-agent-labs.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/JaeCheon8587/hermes-agent-labs/local/pm-workflow-hardening/scripts/install-labs.sh | bash
#
# Pass through installer options:
#   curl -fsSL https://raw.githubusercontent.com/JaeCheon8587/hermes-agent-labs/local/pm-workflow-hardening/scripts/install-labs.sh | bash -s -- --skip-setup
#
# Optional overrides:
#   HERMES_LABS_REPO=https://github.com/JaeCheon8587/hermes-agent-labs.git
#   HERMES_LABS_BRANCH=local/pm-workflow-hardening
#   HERMES_INSTALL_DIR=$HOME/.hermes/hermes-agent

set -euo pipefail

REPO="${HERMES_LABS_REPO:-git@github.com:JaeCheon8587/hermes-agent-labs.git}"
BRANCH="${HERMES_LABS_BRANCH:-local/pm-workflow-hardening}"
DIR="${HERMES_INSTALL_DIR:-$HOME/.hermes/hermes-agent}"

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

command -v git >/dev/null 2>&1 || fail "git is required"
command -v bash >/dev/null 2>&1 || fail "bash is required"

mkdir -p "$(dirname "$DIR")"

if [ -d "$DIR/.git" ]; then
    log "Updating existing checkout at $DIR"
    git -C "$DIR" remote set-url origin "$REPO"
    git -C "$DIR" fetch origin "$BRANCH"
    git -C "$DIR" checkout "$BRANCH"
    git -C "$DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$DIR" ]; then
    fail "$DIR exists but is not a git checkout. Move it aside or set HERMES_INSTALL_DIR."
else
    log "Cloning $REPO#$BRANCH into $DIR"
    git clone --branch "$BRANCH" "$REPO" "$DIR"
fi

success "Checkout ready: $DIR"
log "Running Hermes installer from labs checkout"

exec bash "$DIR/scripts/install.sh" --dir "$DIR" --branch "$BRANCH" "$@"
