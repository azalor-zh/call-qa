#!/usr/bin/env bash
# Call QA: установка на чистый Linux-сервер. Запуск: sudo ./install.sh [--domain callqa.client.kz] [--with-llm] [--gpu]
# Ключи: --with-llm  поднять встроенный Ollama (локальные модели оценки, выбираются и скачиваются в админке);
#        --gpu       дать Ollama видеокарту NVIDIA (нужны драйвер и nvidia-container-toolkit); включает --with-llm.
# Без этих ключей оценку звонков можно подключить через внешний API в админке («Модели»).
# Делает: проверяет Docker, создаёт .env со случайными паролями, самоподписанный сертификат, собирает и запускает.
# Ничего не удаляет. Повторный запуск безопасен (существующий .env не перезаписывает, только дописывает ключи).
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="$(hostname -f 2>/dev/null || hostname)"; WITH_LLM=0; GPU=0; WEIGHTS=""
while [ $# -gt 0 ]; do case "$1" in
  --domain) DOMAIN="$2"; shift 2;;
  --weights) WEIGHTS="$2"; shift 2;;
  --with-llm) WITH_LLM=1; shift;;
  --gpu) GPU=1; WITH_LLM=1; shift;;
  *) echo "неизвестный аргумент $1"; exit 2;; esac; done

say() { printf '\n== %s\n' "$*"; }
setenv() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }

say "1. Проверка окружения"
if command -v docker >/dev/null 2>&1; then DC="docker compose"
elif command -v podman >/dev/null 2>&1; then DC="podman compose"
else echo "Нужен Docker (https://docs.docker.com/engine/install/) или Podman. Установите и запустите скрипт снова."; exit 1; fi
$DC version >/dev/null 2>&1 || { echo "Не найден плагин compose для: $DC"; exit 1; }
command -v openssl >/dev/null || { echo "Нужен openssl"; exit 1; }
FREE_GB=$(df -P . | awk 'NR==2{print int($4/1048576)}')
[ "$FREE_GB" -ge 20 ] || echo "ВНИМАНИЕ: свободно ${FREE_GB} ГБ, рекомендуется от 20 ГБ (образ ~4 ГБ, модели, аудио)"
[ "$WITH_LLM" = 0 ] || [ "$FREE_GB" -ge 60 ] || echo "ВНИМАНИЕ: для локальных LLM нужно ещё 10-30 ГБ на каждую модель"

say "2. Конфигурация (.env)"
if [ ! -f .env ]; then
  cp .env.example .env
  rnd() { openssl rand -base64 24 | tr -d '/+=' | cut -c1-24; }
  ADMIN_PW="$(rnd)"
  sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$(rnd)|; s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PW}|; s|^BASE_URL=.*|BASE_URL=https://${DOMAIN}|" .env
  chmod 600 .env
  echo "Создан .env. Пароль администратора: ${ADMIN_PW}  (записан в .env, смените после входа)"
fi
if [ "$WITH_LLM" = 1 ]; then
  setenv COMPOSE_PROFILES llm
  if [ "$GPU" = 1 ]; then
    command -v nvidia-smi >/dev/null || echo "ВНИМАНИЕ: nvidia-smi не найден; драйвер NVIDIA и nvidia-container-toolkit должны быть установлены"
    setenv COMPOSE_FILE compose.yaml:compose.gpu.yaml
  fi
fi
set -a; . ./.env; set +a
DATA_DIR="${DATA_DIR:-./volumes}"
mkdir -p "$DATA_DIR"/{pg,data,models/ollama,certs,backups}
if [ -n "$WEIGHTS" ]; then   # свои веса GigaAM (если CDN разработчика недоступен или нет интернета)
  mkdir -p "$DATA_DIR/models/.cache/gigaam"; cp -n "$WEIGHTS"/* "$DATA_DIR/models/.cache/gigaam/"
  echo "Веса GigaAM скопированы из $WEIGHTS"
fi
GIGA_DIR="$DATA_DIR/models/.cache/gigaam"
if [ ! -f "$GIGA_DIR/v3_e2e_rnnt.ckpt" ] && [ "${OFFLINE:-0}" != 1 ]; then
  # Веса GigaAM берём из релиза нашего репозитория (не из CDN разработчика модели): архив + sha256, проверка обязательна.
  # Репозиторий публичный, токен не нужен. Свой форк с приватным релизом: export GH_TOKEN=<токен с правом чтения> и GIGAAM_REPO=<владелец>/<репозиторий>.
  REPO="${GIGAAM_REPO:-azalor-zh/call-qa}"; TAG="${GIGAAM_TAG:-weights-gigaam-v3}"; ASSET=gigaam-v3.tar
  say "Веса GigaAM (релиз $REPO, $TAG)"
  TMP="$DATA_DIR/models/.dl"; mkdir -p "$TMP"; ok=0
  dl() {  # dl <имя файла в релизе> <куда>
    if [ -n "${GH_TOKEN:-}" ]; then
      id=$(curl -fsS -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/releases/tags/$TAG" \
        | awk -v n="$1" '/"url": ".*\/releases\/assets\//{u=$2} $0 ~ "\"name\": \""n"\""{gsub(/[",]/,"",u); sub(/.*\//,"",u); print u; exit}') || return 1
      [ -n "$id" ] || return 1
      curl -fL --retry 3 -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/octet-stream" -o "$2" "https://api.github.com/repos/$REPO/releases/assets/$id"
    else
      curl -fL --retry 3 -o "$2" "https://github.com/$REPO/releases/download/$TAG/$1"
    fi
  }
  if dl "$ASSET.sha256" "$TMP/$ASSET.sha256" && dl "$ASSET" "$TMP/$ASSET" && (cd "$TMP" && sha256sum -c --quiet "$ASSET.sha256"); then
    mkdir -p "$GIGA_DIR" && tar -C "$GIGA_DIR" -xf - < "$TMP/$ASSET" && ok=1 && echo "Веса GigaAM загружены и проверены."
  fi
  rm -rf "$TMP"
  [ "$ok" = 1 ] || echo "ВНИМАНИЕ: не удалось загрузить веса из релиза (нет доступа к github.com? свой приватный форк — задайте GH_TOKEN; нет сети? используйте --weights или офлайн-пакет). При первом звонке приложение попробует скачать их само."
fi
chown -R 10001:10001 "$DATA_DIR"/data "$DATA_DIR"/models 2>/dev/null || true
command -v chcon >/dev/null 2>&1 && chcon -R -t container_file_t "$DATA_DIR" 2>/dev/null || true   # SELinux (RHEL)
[ -f "$DATA_DIR/models/.cache/gigaam/v3_e2e_rnnt.ckpt" ] || echo "ВНИМАНИЕ: весов GigaAM нет, при первом звонке они скачаются из интернета (cdn.chatwm.opensmodel.sberdevices.ru). Если сайт недоступен: --weights <папка с v3_e2e_rnnt.ckpt и *_tokenizer.model>."

say "3. Сертификат"
if [ ! -f "$DATA_DIR/certs/callqa.crt" ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 825 -subj "/CN=${DOMAIN}" \
    -addext "subjectAltName=DNS:${DOMAIN}" \
    -keyout "$DATA_DIR/certs/callqa.key" -out "$DATA_DIR/certs/callqa.crt"
  chmod 640 "$DATA_DIR/certs/callqa.key"
  echo "Самоподписанный сертификат. Для боевого режима положите свой в $DATA_DIR/certs/callqa.{crt,key}."
fi

if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
  echo "firewalld активен: открываю порты ${HTTP_PORT:-80} и ${HTTPS_PORT:-443}"
  firewall-cmd --permanent --add-port="${HTTP_PORT:-80}/tcp" --add-port="${HTTPS_PORT:-443}/tcp" >/dev/null && firewall-cmd --reload >/dev/null || echo "ВНИМАНИЕ: не удалось открыть порты в firewalld, откройте вручную"
fi

say "4. Сборка и запуск"
if [ "${OFFLINE:-0}" = 1 ]; then $DC up -d --no-build; else $DC up -d --build; fi  # OFFLINE=1 ставит install-offline.sh: образы уже загружены

say "5. Ожидание готовности"
for i in $(seq 1 60); do
  if curl -fsk "https://127.0.0.1:${HTTPS_PORT:-443}/health" >/dev/null 2>&1; then echo "OK: https://${DOMAIN}/ (логин ${ADMIN_USER:-admin})"; break; fi
  sleep 5; [ "$i" = 60 ] && { echo "Не поднялось за 5 минут: $DC logs app"; exit 1; }
done

say "6. Бэкап"
echo "Добавьте в cron:  17 3 * * *  $(pwd)/backup.sh   (pg_dump + хранение 14 копий)"
if [ "$WITH_LLM" = 1 ]; then
  echo "Локальные модели: админка → Модели → «Модель на нашем сервере» → «Скачать» (или вручную: $DC exec ollama ollama pull qwen3:14b)."
else
  echo "Встроенный сервер моделей не включён: в админке «Модели» подключите внешний API или запустите install.sh --with-llm."
fi
echo "Дальше: README.md, раздел «Первый запуск» (модели, чек-листы, АТС)."
