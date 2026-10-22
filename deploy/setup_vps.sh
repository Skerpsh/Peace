#!/usr/bin/env bash
# =============================================================================
# Одноразовая настройка VPS (Ubuntu / Debian).
#
# Можно запускать прямо из уже склонированного репозитория (например /opt/Peace):
#   bash deploy/setup_vps.sh
# или передать параметры явно:
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
    echo "Использование: bash setup_vps.sh <repo_url> [branch] [install_dir]"
    exit 1
fi

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

# --- 1. Системные пакеты -----------------------------------------------------
say "Установка пакетов (git, python3, venv)"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    warn "Нужен Python >= 3.10, установлен $(python3 --version)."
    exit 1
fi

# --- 2. Deploy key (только если репо ещё не склонировано и нет ключа) --------
if [ ! -d "$APP_DIR/.git" ] && [[ "$REPO_URL" == git@* ]]; then
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
    sort -u -o "$HOME/.ssh/known_hosts" "$HOME/.ssh/known_hosts" 2>/dev/null || true

    if ! grep -qi "github.com" "$HOME/.ssh/config" 2>/dev/null; then
        KEY="$HOME/.ssh/peace_deploy"
        if [ ! -f "$KEY" ]; then
            say "Создание deploy key"
            ssh-keygen -t ed25519 -N "" -C "peace-bot@$(hostname)" -f "$KEY" >/dev/null
        fi
        printf '\nHost github.com\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$KEY" >> "$HOME/.ssh/config"
        chmod 600 "$HOME/.ssh/config"
        echo
        echo "Добавьте этот ключ в GitHub: Settings -> Deploy keys -> Add deploy key:"
        echo "-------------------------------------------------------------------"
        cat "$KEY.pub"
        echo "-------------------------------------------------------------------"
        read -r -p "Нажмите Enter, когда ключ добавлен... " </dev/tty
    fi
fi

# --- 3. Клонирование (если ещё не склонировано) ------------------------------
if [ ! -d "$APP_DIR/.git" ]; then
    say "Клонирование $REPO_URL ($BRANCH) -> $APP_DIR"
    git clone -q -b "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
chmod +x deploy/*.sh deploy.sh 2>/dev/null || true

# --- 4. Виртуальное окружение ------------------------------------------------
say "Python venv и зависимости"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# --- 5. Конфиг и данные ------------------------------------------------------
mkdir -p data && chmod 700 data          # тут сессия userbot и БД
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    ENV_CREATED=1
fi

# --- 6. systemd-сервис -------------------------------------------------------
say "systemd-сервис $SERVICE"
$SUDO tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=Project PEACE — air alert aggregator bot
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

# --- 7. Разрешить update.sh перезапускать сервис без пароля -----------------
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

# --- Итог --------------------------------------------------------------------
say "Окружение и сервис $SERVICE настроены ($APP_DIR)"
cat <<EOF

  Что дальше (на VPS):
    1) Заполнить ключи в .env:
         nano $APP_DIR/.env ${ENV_CREATED:+   (создан из .env.example)}

    2) Один раз войти в userbot-аккаунт (спросит телефон и код):
         ./deploy.sh login

    3) Запустить бота:
         ./deploy.sh restart
         ./deploy.sh logs

  Для обновления после каждого пуша в GitHub просто запускайте:
         ./deploy.sh
EOF
