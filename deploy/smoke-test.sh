#!/usr/bin/env bash
# Проверка установки Call QA после install.sh. Запуск: ./smoke-test.sh [https://адрес]
# Проверяет: контейнеры, /health, редирект на HTTPS, вход администратора, страницы «Модели» и «Сервер», Ollama (если включён).
# Ничего не меняет. Код выхода 0 — всё хорошо.
set -uo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
BASE="${1:-https://127.0.0.1:${HTTPS_PORT:-443}}"
DC="docker compose"; command -v docker >/dev/null || DC="podman compose"
FAIL=0
ok()  { printf '  [ OK ] %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; FAIL=1; }
chk() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

echo "== Контейнеры"
for s in db app nginx; do
  chk "сервис $s запущен" "$DC ps --status running --services | grep -qx $s"
done
chk "app: healthcheck healthy" "[ \"\$($DC ps app --format '{{.Health}}' 2>/dev/null)\" = healthy ]"

echo "== HTTP"
chk "/health отвечает ok" "curl -fsk $BASE/health | grep -q '\"status\":\"ok\"'"
chk "HTTP -> HTTPS редирект" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${HTTP_PORT:-80}/ | grep -q 301"
chk "страница входа" "curl -fsk $BASE/login | grep -qi '<form'"

echo "== Вход и админка"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
CODE=$(curl -sk -c "$JAR" -o /dev/null -w '%{http_code}' --data-urlencode "username=${ADMIN_USER:-admin}" --data-urlencode "password=${ADMIN_PASSWORD}" "$BASE/login")
case "$CODE" in 200|302|303) ok "POST /login ($CODE)";; *) bad "POST /login ($CODE)";; esac
for p in /admin/models /admin/server; do
  C=$(curl -sk -b "$JAR" -o /dev/null -w '%{http_code}' "$BASE$p"); [ "$C" = 200 ] && ok "$p" || bad "$p ($C)"
done
chk "каталог моделей на странице" "curl -sk -b $JAR $BASE/admin/models | grep -q 'qwen3'"

echo "== Локальные LLM (если включены)"
if $DC ps --services 2>/dev/null | grep -qx ollama; then
  chk "ollama отвечает" "$DC exec -T ollama ollama list"
  echo "  установленные модели:"; $DC exec -T ollama ollama list 2>/dev/null | sed 's/^/    /'
else
  echo "  встроенный Ollama не включён (install.sh --with-llm) — оценка через внешний API"
fi

echo "== База"
chk "таблицы созданы" "$DC exec -T db psql -U callqa -d callqa -tAc \"select count(*) from information_schema.tables where table_name in ('jobs','users','settings')\" | grep -q 3"

echo
[ "$FAIL" = 0 ] && echo "ИТОГ: всё работает. Дальше вручную: загрузить тестовый звонок и сравнить оценку." || echo "ИТОГ: есть ошибки, смотрите $DC logs app"
exit $FAIL
