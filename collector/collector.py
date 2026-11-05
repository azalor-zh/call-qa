#!/usr/bin/env python3
"""Collector Call QA: забирает записи звонков с АТС и ставит их в очередь через публичный API Call QA.

Работает отдельным процессом (systemd timer) и ничего не меняет в callqa-web: использует
POST /api/v1/calls с ключом API клиента. Сменные адаптеры АТС — в adapters/. Конфиг — TOML (config.example.toml).

  python3 collector.py --config /etc/callqa/collector.toml            # один цикл
  python3 collector.py --config ... --dry-run                             # показать, что было бы отправлено
  python3 collector.py --config ... --loop 900                            # цикл каждые 900 с
Только стандартная библиотека Python 3.11+.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters import AudioNotReady, Call, make_adapter  # noqa: E402

MAX_ATTEMPTS = 5


for _s in (sys.stdout, sys.stderr):  # журнал не должен падать на нерусской кодировке консоли
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


def log(msg: str):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ---------- состояние ----------
class State:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS calls(source TEXT, call_id TEXT, status TEXT, job_id TEXT, attempts INTEGER DEFAULT 0, "
                        "error TEXT, ts REAL, PRIMARY KEY(source, call_id))")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, source: str, call_id: str):
        return self.db.execute("SELECT status, attempts FROM calls WHERE source=? AND call_id=?", (source, call_id)).fetchone()

    def set(self, source: str, call_id: str, status: str, job_id: str = "", error: str = "", bump: bool = False):
        self.db.execute("INSERT INTO calls(source,call_id,status,job_id,attempts,error,ts) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(source,call_id) DO UPDATE SET status=excluded.status, job_id=excluded.job_id, error=excluded.error, ts=excluded.ts, "
                        "attempts=calls.attempts+?", (source, call_id, status, job_id, 1 if bump else 0, error[:300], time.time(), 1 if bump else 0))
        self.db.commit()


# ---------- фильтры ----------
def accept(call: Call, flt: dict) -> str:
    """Возвращает '' если звонок подходит, иначе причину отказа."""
    exts = [str(e) for e in flt.get("extensions", [])]
    if exts and call.extension not in exts:
        return "не наш добавочный"
    if not call.extension:
        return "добавочный не определён"
    if call.direction not in flt.get("directions", ["in", "out"]):
        return f"направление {call.direction} не выбрано"
    if flt.get("answered_only", True) and not call.answered:
        return "не отвечен"
    if call.duration_s < int(flt.get("min_billsec", 20)):
        return f"короче {flt.get('min_billsec', 20)} с"
    if flt.get("not_before") and call.started_at < str(flt["not_before"]):
        return "раньше даты начала"
    return ""


# ---------- Call QA API ----------
class CallQA:
    def __init__(self, url: str, key_file: str):
        self.url = url.rstrip("/")
        self.key = Path(key_file).read_text(encoding="utf-8").strip()
        self._emp = None

    def _req(self, path: str, data: bytes | None = None, headers: dict | None = None):
        h = {"Authorization": f"Bearer {self.key}", **(headers or {})}
        return urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, headers=h), timeout=300)

    def employees_by_phone(self) -> dict:
        if self._emp is None:
            try:
                with self._req("/api/v1/employees") as r:
                    self._emp = {re.sub(r"\D", "", e.get("phone") or ""): e["name"] for e in json.load(r) if e.get("phone")}
            except Exception as e:  # справочник необязателен
                log(f"справочник сотрудников недоступен: {e}")
                self._emp = {}
        return self._emp

    def upload(self, path: Path, filename: str, rubric: str, employee: str, lang: str) -> str:
        b = uuid.uuid4().hex
        parts = []
        for k, v in (("rubric", rubric), ("employee", employee), ("lang", lang)):
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="audio"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        body = b"".join(parts) + path.read_bytes() + f"\r\n--{b}--\r\n".encode()
        with self._req("/api/v1/calls", body, {"Content-Type": f"multipart/form-data; boundary={b}"}) as r:
            res = json.load(r)
        if not res.get("ids"):
            raise RuntimeError(f"Call QA не принял файл: {res}")
        return res["ids"][0]


def safe(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s)[:40]


def upload_name(c: Call) -> str:
    ts = re.sub(r"\D", "", c.started_at)[:14]
    ext = Path(c.audio_ref).suffix.lower() or ".wav"
    return f"{ts}_{safe(c.extension)}_{c.direction}_{safe(c.client_number)}_{safe(c.source_call_id)}{ext}"


# ---------- цикл ----------
def run_source(src: dict, qa_default: dict, dry: bool) -> dict:
    state = State(src.get("state_db", f"/var/lib/callqa-collector/{src['name']}.db"))
    try:
        return _run_source(src, qa_default, dry, state)
    finally:
        state.close()


def _run_source(src: dict, qa_default: dict, dry: bool, state: State) -> dict:
    name = src["name"]
    flt = src.get("filter", {})
    kind = src["adapter"]
    acfg = dict(src.get(kind, {}))
    acfg["_extensions"] = flt.get("extensions", [])
    adapter = make_adapter(kind, acfg)
    qa_cfg = {**qa_default, **src.get("callqa", {})}
    qa = CallQA(qa_cfg["url"], qa_cfg["api_key_file"])
    stats = {"found": 0, "new": 0, "sent": 0, "wait": 0, "skipped": 0, "failed": 0}
    limit = int(src.get("max_per_cycle", 20))
    give_up_h = float(src.get("give_up_hours", 72))

    calls = sorted(adapter.list_calls(int(src.get("days_back", 2))), key=lambda c: c.started_at)
    stats["found"] = len(calls)
    for c in calls:
        why = accept(c, flt)
        if why:
            stats["skipped"] += 1
            continue
        st = state.get(name, c.call_id)
        if st and st[0] in ("sent", "missing", "failed"):
            continue
        stats["new"] += 1
        if stats["sent"] >= limit:
            break
        employee = src.get("employees", {}).get(c.extension) or qa.employees_by_phone().get(c.extension, "")
        rubric = src.get("rubrics", {}).get(c.extension) or src["rubric"]
        if dry:
            log(f"[{name}] ОТПРАВИЛ БЫ {upload_name(c)} → чек-лист {rubric}, сотрудник «{employee or '—'}»")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / ("audio" + (Path(c.audio_ref).suffix or ".wav"))
            try:
                adapter.fetch_audio(c, f)
            except AudioNotReady as e:
                age_h = (time.time() - datetime.strptime(c.started_at, "%Y-%m-%d %H:%M:%S").timestamp()) / 3600
                if age_h > give_up_h:
                    state.set(name, c.call_id, "missing", error=str(e))
                    log(f"[{name}] {c.call_id}: запись недоступна больше {give_up_h:.0f} ч, пропускаю: {e}")
                else:
                    stats["wait"] += 1
                continue
            except Exception as e:
                state.set(name, c.call_id, "retry", error=str(e), bump=True)
                stats["failed"] += 1
                log(f"[{name}] {c.call_id}: ошибка скачивания: {e}")
                continue
            try:
                jid = qa.upload(f, upload_name(c), rubric, employee, src.get("lang", ""))
            except urllib.error.HTTPError as e:
                if e.code == 429:  # лимит клиента: остальное в следующий цикл
                    log(f"[{name}] лимит Call QA (429), продолжу в следующем цикле")
                    break
                att = (st[1] if st else 0) + 1
                state.set(name, c.call_id, "failed" if att >= MAX_ATTEMPTS or e.code in (400, 401, 403) else "retry", error=f"HTTP {e.code}", bump=True)
                stats["failed"] += 1
                log(f"[{name}] {c.call_id}: Call QA ответил {e.code}")
                continue
            except Exception as e:
                state.set(name, c.call_id, "retry", error=str(e), bump=True)
                stats["failed"] += 1
                log(f"[{name}] {c.call_id}: ошибка отправки: {e}")
                continue
            state.set(name, c.call_id, "sent", job_id=jid)
            stats["sent"] += 1
            log(f"[{name}] отправлено {upload_name(c)} → задача {jid}")
    log(f"[{name}] итог: {stats}")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="повторять каждые N секунд")
    a = ap.parse_args()
    while True:
        cfg = tomllib.loads(Path(a.config).read_text(encoding="utf-8"))
        for src in cfg.get("source", []):
            if src.get("enabled", True):
                try:
                    run_source(src, cfg.get("callqa", {}), a.dry_run)
                except Exception as e:
                    log(f"[{src.get('name')}] цикл прерван: {e}")
        if not a.loop:
            return
        time.sleep(a.loop)


if __name__ == "__main__":
    main()
