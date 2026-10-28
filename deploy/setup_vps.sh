#!/usr/bin/env bash
# =============================================================================
# One-time VPS setup script (Ubuntu / Debian).
#
# Can be run directly inside an already cloned repository (e.g. /opt/Peace):
#   bash deploy/setup_vps.sh
# or with explicit parameters:
#   bash setup_vps.sh <repo_url> [branch] [install_dir]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || pwd)"
if [ -d "$SCRIPT_DIR/.git" ]; then
    DEFAULT_DIR="$SCRIPT_DIR"
    DEFAULT_URL="$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || echo "")"
    DEFAULT_BRANCH="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")"
else
    DEFAULT_DIR="/opt/Peace"
    DEFAULT_URL=""
    DEFAULT_BRANCH="main"
fi

REPO_URL="${1:-$DEFAULT_URL}"
BRANCH="${2:-$DEFAULT_BRANCH}"
APP_DIR="${3:-$DEFAULT_DIR}"
SERVICE="peace-bot"
RUN_USER="$(id -un)"

if [ -z "$REPO_URL" ] && [ ! -d "$APP_DIR/.git" ]; then
    echo "Usage: bash setup_vps.sh <repo_url> [branch] [install_dir]"
    exit 1
fi

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

# --- 1. System packages ------------------------------------------------------
say "Installing system packages (git, python3, venv)"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    warn "Python >= 3.10 is required, found $(python3 --version)."
    exit 1
fi

# --- 2. Deploy key (only if repo is not cloned yet and no SSH key configured) -
if [ ! -d "$APP_DIR/.git" ] && [[ "$REPO_URL" == git@* ]]; then
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
    sort -u -o "$HOME/.ssh/known_hosts" "$HOME/.ssh/known_hosts" 2>/dev/null || true

    if ! grep -qi "github.com" "$HOME/.ssh/config" 2>/dev/null; then
        KEY="$HOME/.ssh/peace_deploy"
        if [ ! -f "$KEY" ]; then
            say "Generating SSH deploy key"
            ssh-keygen -t ed25519 -N "" -C "peace-bot@$(hostname)" -f "$KEY" >/dev/null
        fi
        printf '\nHost github.com\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$KEY" >> "$HOME/.ssh/config"
        chmod 600 "$HOME/.ssh/config"
        echo
        echo "Add this deploy key to GitHub: Settings -> Deploy keys -> Add deploy key:"
        echo "-------------------------------------------------------------------"
        cat "$KEY.pub"
        echo "-------------------------------------------------------------------"
        read -r -p "Press Enter once the key is added... " </dev/tty
    fi
fi

# --- 3. Clone repository (if not cloned yet) ---------------------------------
if [ ! -d "$APP_DIR/.git" ]; then
    $SUDO mkdir -p "$APP_DIR" && $SUDO chown "$RUN_USER" "$APP_DIR"
    say "Cloning $REPO_URL ($BRANCH) -> $APP_DIR"
    git clone -q -b "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
chmod +x deploy/*.sh deploy.sh 2>/dev/null || true

# --- 4. Python virtual environment -------------------------------------------
say "Setting up Python virtualenv and dependencies"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# --- 5. Config & data directory ----------------------------------------------
mkdir -p data && chmod 700 data
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    ENV_CREATED=1
fi

# --- 6. systemd service ------------------------------------------------------
say "Configuring systemd service: $SERVICE"
$SUDO tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=Project PEACE — Air Alert Aggregator Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONPATH=$APP_DIR/src
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python -m peace.main
Restart=always
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=10

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable -q "$SERVICE"

# --- 7. Passwordless sudo for service restarts (non-root users) --------------
if [ -n "$SUDO" ]; then
    SYSTEMCTL="$(command -v systemctl)"
    RULE="/etc/sudoers.d/$SERVICE"
    TMP="$(mktemp)"
    cat > "$TMP" <<EOF
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $SERVICE, $SYSTEMCTL start $SERVICE, $SYSTEMCTL stop $SERVICE, $SYSTEMCTL reset-failed $SERVICE
EOF
    if $SUDO visudo -cf "$TMP" >/dev/null; then
        $SUDO install -m 440 "$TMP" "$RULE"
    fi
    rm -f "$TMP"
    $SUDO usermod -aG systemd-journal "$RUN_USER" || true
fi

# --- Summary -----------------------------------------------------------------
say "Environment and systemd service '$SERVICE' configured ($APP_DIR)"
cat <<EOF

  Next steps:
    1) Fill in .env: BOT_TOKEN (required), ADMIN_CHAT_ID (send /id to the bot), ALERTS_API_TOKEN:
         nano $APP_DIR/.env ${ENV_CREATED:+   (created from .env.example)}

    2) Optional: Telethon userbot login for faster push updates (phone + login code):
         ./deploy.sh login

    3) Start the bot and check logs:
         ./deploy.sh restart
         ./deploy.sh logs

  To update after pushing new commits to GitHub, simply run:
         ./deploy.sh
EOF
