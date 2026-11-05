#!/usr/bin/env bash
# Ежедневный бэкап БД Call QA (pg_dump, 14 копий). Аудио копируйте отдельно: $DATA_DIR/data/audio.
# Восстановление: gunzip -c файл.sql.gz | docker compose exec -T db psql -U callqa -d callqa
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
DC="docker compose"; command -v docker >/dev/null || DC="podman compose"
OUT="${DATA_DIR:-./volumes}/backups"; mkdir -p "$OUT"
F="$OUT/callqa-$(date +%Y%m%d-%H%M%S).sql.gz"
$DC exec -T db pg_dump -U callqa -d callqa | gzip > "$F"
[ -s "$F" ] || { echo "пустой бэкап"; rm -f "$F"; exit 1; }
ls -1t "$OUT"/callqa-*.sql.gz | tail -n +15 | xargs -r rm -f
echo "готово: $F"
