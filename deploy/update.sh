#!/usr/bin/env bash
# =============================================================================
# Обновление бота НА VPS: подтягивает свежий код с GitHub, ставит зависимости
# (если изменился requirements.txt), гоняет тесты и перезапускает сервис.
# Если тесты упали или бот не поднялся — автоматический откат на прошлую версию.
#
#   ./deploy.sh                          # основной способ запуска на VPS
#   FORCE=1 ./deploy.sh                  # перезапустить, даже если код не менялся
#   SKIP_TESTS=1 ./deploy.sh             # без тестов (не рекомендуется)
# =============================================================================
set -euo pipefail

# Весь код внутри функции: bash прочитает её целиком ДО выполнения, поэтому
# git reset, меняющий этот самый файл, не сломает работающий скрипт.
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
    say "Получение изменений из GitHub ($BRANCH)"
    git fetch -q origin "$BRANCH"
    NEW="$(git rev-parse "origin/$BRANCH")"

    if [ "$PREV" = "$NEW" ] && [ "${FORCE:-0}" != "1" ]; then
        ok "Код уже на самой свежей версии: $(git log -1 --format='%h %s')"
    else
        # .env, data/ (сессия, БД) и .venv в .gitignore — reset их не трогает
        git reset -q --hard "$NEW"
        if [ "$PREV" != "$NEW" ]; then
            echo "Новые коммиты:"
            git log --format='  %h %s (%an)' "$PREV..$NEW"
        fi
    fi

    local DEPS_CHANGED=0
    if ! git diff --quiet "$PREV" "$NEW" -- requirements.txt || [ ! -x .venv/bin/python ]; then
        DEPS_CHANGED=1
        say "Установка/обновление зависимостей Python"
        install_deps
    fi

    rollback() {
        fail "$1 — откат на $(git log -1 --format='%h %s' "$PREV")"
        git reset -q --hard "$PREV"
        [ "$DEPS_CHANGED" = "1" ] && install_deps
        if [ "${RESTARTED:-0}" = "1" ]; then
            $SUDO systemctl restart "$SERVICE" || true
        fi
        exit 1
    }

    if [ "${SKIP_TESTS:-0}" != "1" ]; then
        say "Проверка тестами"
        PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider || rollback "Тесты не прошли"
    fi

    # Первый деплой: ещё нет ключей или сессии userbot — сервис не трогаем
    local SESSION
    SESSION="$(grep -E '^TG_SESSION_NAME=' .env 2>/dev/null | cut -d= -f2- || true)"
    SESSION="${SESSION:-data/peace_userbot}.session"
    if [ ! -f .env ] || [ ! -f "$SESSION" ]; then
        fail "Ещё не заполнены ключи (.env) или нет сессии ($SESSION) — код обновлён, сервис пока не запущен."
        echo "  1) Заполните .env:  nano .env"
        echo "  2) Авторизуйте userbot:  ./deploy.sh login"
        echo "  3) Запустите бота:  ./deploy.sh restart"
        return 0
    fi

    # Если код не менялся и сервис уже активен (и не задан FORCE=1) — рестарт не нужен
    if [ "$PREV" = "$NEW" ] && [ "${FORCE:-0}" != "1" ] && systemctl is-active --quiet "$SERVICE"; then
        ok "Сервис $SERVICE работает."
        return 0
    fi

    say "Перезапуск $SERVICE"
    $SUDO systemctl reset-failed "$SERVICE" 2>/dev/null || true
    $SUDO systemctl restart "$SERVICE"
    RESTARTED=1

    # Ждём, чтобы поймать падение при старте (Restart=always перезапускает тихо)
    sleep 8
    local NRESTARTS
    NRESTARTS="$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0)"
    if ! systemctl is-active --quiet "$SERVICE" || [ "${NRESTARTS:-0}" -gt 0 ]; then
        journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null || true
        rollback "Бот не запустился"
    fi

    ok "Обновлено и запущено: $(git log -1 --format='%h %s')"
    journalctl -u "$SERVICE" -n 10 --no-pager 2>/dev/null || true
}

main "$@"
exit
