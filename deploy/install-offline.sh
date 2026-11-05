#!/usr/bin/env bash
# Офлайн-установка Call QA из архива, собранного make-bundle.sh. Интернет не нужен; нужны Docker и openssl.
# Использование (из распакованной папки архива): sudo ./install-offline.sh [--domain имя] [--with-llm] [--gpu]
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1. Проверка целостности архива"
sha256sum -c --quiet SHA256SUMS || { echo "Контрольные суммы не совпали: архив повреждён, скопируйте его заново"; exit 1; }

command -v docker >/dev/null || { echo "Нужен Docker (установите заранее: офлайн-пакеты docker-ce для вашей ОС)"; exit 1; }

echo "== 2. Загрузка образов"
gunzip -c images/images.tar.gz | docker load

echo "== 3. Модели (GigaAM и выбранные локальные LLM)"
DATA_DIR="$(pwd)/repo/deploy/volumes"
mkdir -p "$DATA_DIR/models"
cp -a models/. "$DATA_DIR/models/"

echo "== 4. Установка"
cd repo/deploy
OFFLINE=1 ./install.sh "$@"   # DATA_DIR по умолчанию ./volumes — туда уже скопированы модели
echo "Проверка: cd $(pwd) && ./smoke-test.sh"
