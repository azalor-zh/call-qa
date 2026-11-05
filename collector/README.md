# Collector Call QA

Забирает записи звонков с АТС и ставит их в очередь Call QA через `POST /api/v1/calls` (ключ API клиента).
Отдельный процесс, `callqa-web` не меняется и не перезапускается. Только стандартная библиотека Python 3.11+.

- `collector.py` — ядро: фильтры, дедупликация (SQLite), повторы, лимит на цикл, отправка.
- `adapters/` — сменные адаптеры АТС. Готов `freepbx`; для других АТС — новый модуль с `list_calls` и `fetch_audio`.
- `config.example.toml` — все настройки; на каждую АТС свой блок `[[source]]` (добавочные, направления, минимум секунд,
  имя сотрудника по добавочному, чек-лист, ключ клиента). Секреты только в файлах.
- `deploy/` — systemd service + timer (каждые 15 минут).
- `tests/test_collector.py` — 8 проверок без АТС (`python -I tests/test_collector.py`).

```
python3 collector.py --config /etc/callqa/collector.toml --dry-run   # что было бы отправлено
python3 collector.py --config /etc/callqa/collector.toml             # один цикл
```

## FreePBX: что проверено (2026-11-05, FreePBX 17.0.28)
- API: `http://<АТС>:83/admin/api/api`; токен — POST `/token` (client_credentials, scope `gql:cdr`, 3600 с); GraphQL — `/gql`.
- `fetchAllCdrs(first, after, orderby: date, startDate, endDate)`; даты только `ГГГГ-ММ-ДД` и только парой.
- Аудио через API недоступно: файл берётся по SSH из `/var/spool/asterisk/monitor/ГГГГ/ММ/ДД/<recordingfile>`.
- Номер сотрудника — из `channel`/`dstchannel` (`PJSIP/152-…`), не из `src`: у внешних исходящих `src` = CID транка.
- Запись включается per-extension (Расширенный → Запись): внешние/внутренние, входящие/исходящие; нужно «Применить изменения».
- Имя файла исходящего: `out-<куда>-<добавочный>-ГГГГММДД-ЧЧММСС-<uniqueid>.wav`. Шаблоны `in-`/`internal-` не проверены на живых файлах.
- Звонки без ответа не пишутся; минимальная длительность по умолчанию в конфиге — 10 с (в docs/06 — 20).

## Не сделано
- Форма подключения АТС в админке Call QA (сейчас конфиг — TOML-файл на сервере).
- Адаптеры других АТС (по запросу).
- Очистка расшифровок и оценок по сроку (в коде чистится только аудио).
