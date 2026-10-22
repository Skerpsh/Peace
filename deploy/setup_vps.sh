#!/usr/bin/env bash
# =============================================================================
# Одноразовая настройка VPS (Ubuntu / Debian). Запускать НА VPS под обычным
# пользователем с sudo (не root):
#
#   bash setup_vps.sh <repo_url> [branch] [install_dir]
#
#   repo_url    — git@github.com:USER/REPO.git (приватный) или https://... (публичный)
#   branch      — по умолчанию main
#   install_dir — по умолчанию ~/Project_PEACE
#
# Что делает: ставит python/git, (для SSH-URL) создаёт deploy key, клонирует репо,
# создаёт venv, .env, systemd-сервис peace-bot и разрешает перезапуск без пароля.
# Скрипт идемпотентный — можно запускать повторно.
# =============================================================================
set -euo pipefail

REPO_URL="${1:?Использование: setup_vps.sh <repo_url> [branch] [install_dir]}"
BRANCH="${2:-main}"
APP_DIR="${3:-$HOME/Project_PEACE}"
SERVICE="peace-bot"
RUN_USER="$(id -un)"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }

if [ "$RUN_USER" = "root" ]; then
    warn "Лучше запускать под обычным пользователем (бот не должен работать от root)."
    SUDO=""
else
    SUDO="sudo"
fi

# --- 1. Системные пакеты -----------------------------------------------------
say "Установка пакетов (git, python3, venv)"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    warn "Нужен Python >= 3.10, установлен $(python3 --version). Обновите систему или поставьте python3.12."
    exit 1
fi

# --- 2. Deploy key для приватного репозитория -------------------------------
if [[ "$REPO_URL" == git@* ]]; then
    KEY="$HOME/.ssh/peace_deploy"
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    if [ ! -f "$KEY" ]; then
        say "Создание deploy key"
        ssh-keygen -t ed25519 -N "" -C "peace-bot@$(hostname)" -f "$KEY" >/dev/null
    fi
    if ! grep -q "peace_deploy" "$HOME/.ssh/config" 2>/dev/null; then
        printf '\nHost github.com\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$KEY" >> "$HOME/.ssh/config"
        chmod 600 "$HOME/.ssh/config"
    fi
    ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
    sort -u -o "$HOME/.ssh/known_hosts" "$HOME/.ssh/known_hosts"

    if ! ssh -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
        echo
        echo "Добавьте этот ключ в GitHub: репозиторий -> Settings -> Deploy keys -> Add deploy key"
        echo "(галочку 'Allow write access' НЕ ставить)"
        echo "-------------------------------------------------------------------"
        cat "$KEY.pub"
        echo "-------------------------------------------------------------------"
        read -r -p "Нажмите Enter, когда ключ добавлен... " </dev/tty
    fi
fi

# --- 3. Клонирование ---------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
    say "Репозиторий уже есть: $APP_DIR — обновляю"
    git -C "$APP_DIR" fetch -q origin "$BRANCH"
    git -C "$APP_DIR" checkout -q "$BRANCH"
    git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
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
# Не перезапускать бесконечно при ошибке конфига: не больше 10 раз за 5 минут
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
    else
        warn "Не удалось создать правило sudoers — перезапуск будет спрашивать пароль"
    fi
    rm -f "$TMP"
    # чтение логов без sudo
    $SUDO usermod -aG systemd-journal "$RUN_USER" || true
fi

# --- Итог --------------------------------------------------------------------
say "Готово! Осталось 3 шага:"
cat <<EOF

  1) Заполнить ключи:
       nano $APP_DIR/.env ${ENV_CREATED:+   (создан из .env.example)}

  2) Один раз войти в userbot-аккаунт (спросит телефон и код):
       cd $APP_DIR && PYTHONPATH=src .venv/bin/python -m peace.login

  3) Запустить бота:
       sudo systemctl start $SERVICE
       journalctl -u $SERVICE -f        # логи

  Дальше обновление — с локальной машины одной командой:  ./deploy.sh
  (перелогиньтесь по SSH, чтобы journalctl работал без sudo)
EOF
