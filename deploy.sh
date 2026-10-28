#!/usr/bin/env bash
# =============================================================================
# Universal deployment and service management script for Project PEACE.
#
# Supports two modes:
#   1) DIRECTLY ON VPS (inside /opt/Peace, without deploy/deploy.env):
#        ./deploy.sh            pull from GitHub, run tests, and restart service
#        ./deploy.sh setup      initial environment & systemd service setup
#        ./deploy.sh login      optional Telethon userbot login (web poller works without it)
#        ./deploy.sh env        edit .env
#        ./deploy.sh status     show peace-bot systemd service status
#        ./deploy.sh logs       follow live service logs (Ctrl+C to exit)
#        ./deploy.sh restart    restart peace-bot service
#
#   2) FROM LOCAL MACHINE OVER SSH (when deploy/deploy.env has a real VPS_HOST):
#        ./deploy.sh setup      first-time install on a fresh VPS (clone, venv, systemd)
#        ./deploy.sh env        edit .env on the VPS
#        ./deploy.sh            git push + remote update on VPS over SSH
#        ./deploy.sh status|logs|restart|login|ssh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
infocmp >/dev/null 2>&1 || export TERM=xterm-256color

SERVICE="peace-bot"
CONF="deploy/deploy.env"
VPS_HOST=""

if [ -f "$CONF" ]; then
    # shellcheck source=/dev/null
    source "$CONF"
fi

# If deploy.env does not exist or still has the placeholder IP, run locally on this machine
if [ -z "${VPS_HOST:-}" ] || [ "$VPS_HOST" = "1.2.3.4" ]; then
    SUDO=""
    [ "$(id -u)" -eq 0 ] || SUDO="sudo"
    BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

    case "${1:-deploy}" in
        setup)
            bash deploy/setup_vps.sh
            ;;
        login)
            PYTHONPATH=src .venv/bin/python -m peace.login
            ;;
        env)
            "${EDITOR:-nano}" .env
            ;;
        status)
            systemctl status "$SERVICE" --no-pager
            ;;
        logs)
            journalctl -u "$SERVICE" -f -n 50
            ;;
        restart)
            $SUDO systemctl restart "$SERVICE"
            sleep 3
            systemctl status "$SERVICE" --no-pager
            ;;
        deploy|update)
            if [ ! -f "/etc/systemd/system/$SERVICE.service" ]; then
                echo "==> First run: setting up environment and systemd service..."
                bash deploy/setup_vps.sh
            fi
            bash deploy/update.sh "$BRANCH"
            ;;
        *)
            sed -n '6,13p' "$0"
            exit 1
            ;;
    esac
    exit 0
fi

# --- Remote SSH deployment mode from local machine ---------------------------
VPS_USER="${VPS_USER:-$USER}"
VPS_PORT="${VPS_PORT:-22}"
VPS_DIR="${VPS_DIR:-/opt/Peace}"
BRANCH="${BRANCH:-main}"

remote_tty() { ssh -t -p "$VPS_PORT" "$VPS_USER@$VPS_HOST" "$@"; }
remote_cmd() { ssh -p "$VPS_PORT" "$VPS_USER@$VPS_HOST" "$@"; }

case "${1:-deploy}" in
    status)  remote_tty "systemctl status $SERVICE --no-pager" ;;
    logs)    remote_tty "journalctl -u $SERVICE -f -n 50" ;;
    restart) remote_tty "sudo systemctl restart $SERVICE && sleep 3 && systemctl status $SERVICE --no-pager" ;;
    ssh)     remote_tty "cd $VPS_DIR && exec \$SHELL -l" ;;
    env)     remote_tty "\${EDITOR:-nano} $VPS_DIR/.env" ;;
    login)   remote_tty "cd $VPS_DIR && PYTHONPATH=src .venv/bin/python -m peace.login" ;;
    setup)
        # The repo is not on the VPS yet, so ship the setup script itself
        scp -q -P "$VPS_PORT" deploy/setup_vps.sh "$VPS_USER@$VPS_HOST:/tmp/peace_setup_vps.sh"
        remote_tty "bash /tmp/peace_setup_vps.sh '$(git remote get-url origin)' '$BRANCH' '$VPS_DIR'; rm -f /tmp/peace_setup_vps.sh"
        ;;
    deploy|update)
        if [ -n "$(git status --porcelain)" ]; then
            echo "WARNING: Uncommitted local changes will not be deployed:"
            git status --short
            read -r -p "Continue anyway? [y/N] " ans
            [[ "$ans" =~ ^[yY]$ ]] || exit 1
        fi

        CUR="$(git rev-parse --abbrev-ref HEAD)"
        if [ "$CUR" != "$BRANCH" ]; then
            echo "WARNING: Current branch is '$CUR', deploying '$BRANCH'."
            read -r -p "Continue anyway? [y/N] " ans
            [[ "$ans" =~ ^[yY]$ ]] || exit 1
        fi

        echo "==> git push origin $BRANCH"
        git push origin "$BRANCH"

        echo "==> Updating on $VPS_USER@$VPS_HOST ($VPS_DIR)"
        remote_cmd "cd $VPS_DIR && bash deploy/update.sh $BRANCH"
        ;;
    *)
        sed -n '15,19p' "$0"
        exit 1
        ;;
esac
