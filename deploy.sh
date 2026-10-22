#!/usr/bin/env bash
# =============================================================================
# Деплой одной командой с локальной машины:
#
#   ./deploy.sh            push + обновление на VPS (тесты, рестарт, автооткат)
#   ./deploy.sh status     статус сервиса
#   ./deploy.sh logs       логи в реальном времени (Ctrl+C — выход)
#   ./deploy.sh restart    перезапуск без обновления кода
#   ./deploy.sh ssh        зайти на VPS в папку проекта
#
# Настройки — в deploy/deploy.env (шаблон: deploy/deploy.env.example).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

CONF="deploy/deploy.env"
if [ ! -f "$CONF" ]; then
    echo "Нет $CONF. Создайте его:  cp deploy/deploy.env.example $CONF  и заполните."
    exit 1
fi
# shellcheck source=/dev/null
source "$CONF"
: "${VPS_HOST:?VPS_HOST не задан в $CONF}"
VPS_USER="${VPS_USER:-$USER}"
VPS_PORT="${VPS_PORT:-22}"
VPS_DIR="${VPS_DIR:-Project_PEACE}"
BRANCH="${BRANCH:-main}"
SERVICE="peace-bot"

remote() { ssh -p "$VPS_PORT" "$@" "$VPS_USER@$VPS_HOST"; }

case "${1:-deploy}" in
    status)  remote -t -- "systemctl status $SERVICE --no-pager" ;;
    logs)    remote -t -- "journalctl -u $SERVICE -f -n 50" ;;
    restart) remote -t -- "sudo systemctl restart $SERVICE && sleep 3 && systemctl status $SERVICE --no-pager" ;;
    ssh)     remote -t -- "cd $VPS_DIR && exec \$SHELL -l" ;;
    deploy)
        # Незакоммиченные изменения на сервер НЕ попадут — предупредим
        if [ -n "$(git status --porcelain)" ]; then
            echo "⚠ Есть незакоммиченные изменения (на сервер не попадут):"
            git status --short
            read -r -p "Продолжить? [y/N] " ans
            [[ "$ans" =~ ^[yYдД]$ ]] || exit 1
        fi

        CUR="$(git rev-parse --abbrev-ref HEAD)"
        if [ "$CUR" != "$BRANCH" ]; then
            echo "⚠ Вы на ветке '$CUR', а деплоится '$BRANCH'."
            read -r -p "Продолжить? [y/N] " ans
            [[ "$ans" =~ ^[yYдД]$ ]] || exit 1
        fi

        echo "==> git push origin $BRANCH"
        git push origin "$BRANCH"

        echo "==> Обновление на $VPS_USER@$VPS_HOST"
        remote -- "cd $VPS_DIR && bash deploy/update.sh $BRANCH"
        ;;
    *)
        sed -n '3,10p' "$0"; exit 1 ;;
esac
