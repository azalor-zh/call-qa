"""Разовый перенос данных из SQLite (jobs.db) в PostgreSQL.

Запуск: CALLQA_PG_DSN=... python migrate_sqlite_to_pg.py /путь/к/jobs.db   (служба callqa-web на это время остановлена)
Таблицы в PostgreSQL создаёт core.py при импорте. Скрипт не стирает данные: если в таблице уже есть строки, пропускает её.
"""
import os
import sqlite3
import sys

os.environ.setdefault("CALLQA_DATA", os.path.dirname(os.path.abspath(sys.argv[1])))
os.environ.setdefault("CALLQA_WEB_USER", "x")
os.environ.setdefault("CALLQA_WEB_PASSWORD", "x")
import core  # noqa: E402  создаёт схему

src = sqlite3.connect(sys.argv[1])
src.row_factory = sqlite3.Row
for table, serial in (("users", True), ("settings", False), ("rubrics", True), ("jobs", False)):
    have = core.q(f"SELECT count(*) c FROM {table}").fetchone()["c"]
    if table == "users" and have == 1:  # core создаёт первого администратора из env: заменяем его перенесёнными учётками
        core.q("DELETE FROM users")
        have = 0
    if have:
        print(f"{table}: в PostgreSQL уже {have} строк, пропускаю")
        continue
    cols = [r["name"] for r in src.execute(f"PRAGMA table_info({table})")]
    rows = src.execute(f"SELECT {','.join(cols)} FROM {table}").fetchall()
    for r in rows:
        core.q(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", *tuple(r))
    if serial:
        core.q(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), coalesce((SELECT max(id) FROM {table}), 1))")
    print(f"{table}: перенесено {len(rows)}")
for table in ("users", "settings", "rubrics", "jobs"):
    a = src.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    b = core.q(f"SELECT count(*) c FROM {table}").fetchone()["c"]
    print(f"{table}: SQLite {a} / PostgreSQL {b}", "OK" if a == b else "РАСХОЖДЕНИЕ")
