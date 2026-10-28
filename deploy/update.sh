#!/usr/bin/env bash
# =============================================================================
# Updates the bot on the VPS: fetches latest commits from GitHub, installs
# dependencies if requirements.txt changed, runs pytest, and restarts systemd.
# Automatically rolls back to the previous commit if tests or startup fail.
#
#   ./deploy.sh                          # standard update command
#   FORCE=1 ./deploy.sh                  # force restart even if no new commits
#   SKIP_TESTS=1 ./deploy.sh             # skip pytest (not recommended)
# =============================================================================
set -euo pipefail

# Wrapped in a function so bash parses the entire script into memory before
# git reset potentially modifies this file on disk.
main() {
    cd "$(dirname "$0")/.."
    local SERVICE="peace-bot"
    local BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"
    local SUDO=""
    [ "$(id -u)" -eq 0 ] || SUDO="sudo"

    say()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
    ok()   { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
    fail() { printf '\033[1;31m✘ %s\033[0m\n' "$*"; }

    install_deps() {
        [ -x .venv/bin/python ] || python3 -m venv .venv
        .venv/bin/pip install -q -r requirements.txt
    }

    local PREV NEW
    PREV="$(git rev-parse HEAD)"
    say "Fetching latest changes from GitHub ($BRANCH)"
    git fetch -q origin "$BRANCH"
    NEW="$(git rev-parse "origin/$BRANCH")"

    if [ "$PREV" = "$NEW" ] && [ "${FORCE:-0}" != "1" ]; then
        ok "Already up to date: $(git log -1 --format='%h %s')"
    else
        # .env, data/ (session, DB), and .venv are gitignored and untouched by reset
        git reset -q --hard "$NEW"
        if [ "$PREV" != "$NEW" ]; then
            echo "New commits:"
            git --no-pager log --format='  %h %s (%an)' "$PREV..$NEW"
        fi
    fi

    local DEPS_CHANGED=0
    if ! git diff --quiet "$PREV" "$NEW" -- requirements.txt || [ ! -x .venv/bin/python ]; then
        DEPS_CHANGED=1
        say "Installing/updating Python dependencies"
        install_deps
    fi

    rollback() {
        fail "$1 — rolling back to $(git log -1 --format='%h %s' "$PREV")"
        git reset -q --hard "$PREV"
        [ "$DEPS_CHANGED" = "1" ] && install_deps
        if [ "${RESTARTED:-0}" = "1" ]; then
            $SUDO systemctl restart "$SERVICE" || true
        fi
        exit 1
    }

    if [ "${SKIP_TESTS:-0}" != "1" ]; then
        say "Running test suite"
        PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider || rollback "Test suite failed"
    fi

    # Initial deploy check: only BOT_TOKEN in .env is required to start the service
    local BOT_TOKEN_VAL
    BOT_TOKEN_VAL="$(grep -E '^BOT_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)"
    if [ ! -f .env ] || [ -z "$BOT_TOKEN_VAL" ] || [[ "$BOT_TOKEN_VAL" == *"your-bot-token"* ]]; then
        fail "Missing or placeholder BOT_TOKEN in .env — code updated, service not started yet."
        echo "  1) Configure BOT_TOKEN in .env:  nano .env"
        echo "  2) Start service:                ./deploy.sh restart"
        return 0
    fi

    if [ "$PREV" = "$NEW" ] && [ "${FORCE:-0}" != "1" ] && systemctl is-active --quiet "$SERVICE"; then
        ok "Service '$SERVICE' is active and running."
        return 0
    fi

    say "Restarting $SERVICE"
    $SUDO systemctl reset-failed "$SERVICE" 2>/dev/null || true
    $SUDO systemctl restart "$SERVICE"
    RESTARTED=1

    # Wait briefly to verify the process does not crash on startup
    sleep 8
    local NRESTARTS
    NRESTARTS="$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0)"
    if ! systemctl is-active --quiet "$SERVICE" || [ "${NRESTARTS:-0}" -gt 0 ]; then
        journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null || true
        rollback "Service failed to stay up after restart"
    fi

    local MISSING
    MISSING="$(comm -23 <(grep -oE '^[A-Z_]+=' .env.example | sort -u) <(grep -oE '^[A-Z_]+=' .env | sort -u) | tr -d '=' | xargs)"
    [ -z "$MISSING" ] || printf '\033[1;33m[!] Not set in .env (defaults used): %s — see .env.example\033[0m\n' "$MISSING"

    ok "Deployed and running: $(git log -1 --format='%h %s')"
    journalctl -u "$SERVICE" -n 10 --no-pager 2>/dev/null || true
}

main "$@"
exit
