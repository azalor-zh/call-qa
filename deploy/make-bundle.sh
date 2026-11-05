#!/usr/bin/env bash
# Сборка архива для офлайн-установки Call QA. Запускать на Linux-машине С ИНТЕРНЕТОМ и Docker.
# Использование: ./make-bundle.sh [--models qwen3:14b,gemma3:12b] [--version 0.1.0]
# Результат: dist/callqa-bundle-<версия>.tar (образы, веса GigaAM, выбранные модели Ollama, исходники, SHA256SUMS).
# Установка у клиента: tar xf callqa-bundle-*.tar && cd callqa-bundle-* && sudo ./install-offline.sh [--with-llm] [--gpu]
set -euo pipefail
cd "$(dirname "$0")"

VER="0.1.0"; MODELS=""
while [ $# -gt 0 ]; do case "$1" in
  --models) MODELS="$2"; shift 2;;
  --version) VER="$2"; shift 2;;
  *) echo "неизвестный аргумент $1"; exit 2;; esac; done

command -v docker >/dev/null || { echo "Нужен Docker"; exit 1; }
OUT="$(pwd)/dist/callqa-bundle-$VER"
rm -rf "$OUT"; mkdir -p "$OUT"/{images,models/.cache,models/ollama,repo}
export CALLQA_VERSION="$VER"

echo "== 1. Сборка образа приложения"
[ -f .env ] || { cp .env.example .env; sed -i 's|^DB_PASSWORD=.*|DB_PASSWORD=x|; s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=x|' .env; TMPENV=1; }
docker compose build app

echo "== 2. Образы в архив"
IMAGES="callqa-app:$VER postgres:16 nginx:1.27-alpine ollama/ollama:latest"
docker pull postgres:16 && docker pull nginx:1.27-alpine && docker pull ollama/ollama:latest
docker save $IMAGES | gzip > "$OUT/images/images.tar.gz"

echo "== 3. Веса GigaAM (кэш /models контейнера)"
docker run --rm -v "$OUT/models:/models" --user 0 "callqa-app:$VER" \
  python -c "import gigaam; [gigaam.load_model('v3_e2e_'+n, device='cpu', fp16_encoder=False) for n in ('rnnt','ctc')]; print('веса загружены')"

if [ -n "$MODELS" ]; then
  echo "== 4. Модели Ollama: $MODELS"
  docker run -d --name callqa-bundle-ollama -v "$OUT/models/ollama:/root/.ollama" ollama/ollama:latest >/dev/null
  trap 'docker rm -f callqa-bundle-ollama >/dev/null 2>&1 || true' EXIT
  sleep 5
  for m in ${MODELS//,/ }; do docker exec callqa-bundle-ollama ollama pull "$m"; done
fi

echo "== 5. Исходники и установщик"
cp -r ../app "$OUT/repo/app"; mkdir -p "$OUT/repo/deploy"
cp compose.yaml compose.gpu.yaml Dockerfile nginx.conf install.sh backup.sh smoke-test.sh README.md .env.example "$OUT/repo/deploy/"
find "$OUT/repo" -name __pycache__ -prune -exec rm -rf {} +
cp install-offline.sh "$OUT/"; chmod +x "$OUT"/install-offline.sh "$OUT"/repo/deploy/*.sh
echo "$VER" > "$OUT/VERSION"
(cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | xargs -0 sha256sum > SHA256SUMS)

echo "== 6. Упаковка"
tar -C "$(dirname "$OUT")" -cf "$OUT.tar" "$(basename "$OUT")"
[ "${TMPENV:-0}" = 1 ] && rm -f .env
echo "Готово: $OUT.tar ($(du -h "$OUT.tar" | cut -f1)). Контрольная сумма: $(sha256sum "$OUT.tar" | cut -d' ' -f1)"
