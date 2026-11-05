"""Веб-хуки: сервер сам сообщает системе клиента, когда звонок оценён (или упал с ошибкой).

Адрес и секрет задаёт администратор (клиент адрес не меняет: иначе сервер можно было бы направить на внутренние адреса сети).
Запрос — POST с JSON; подпись: заголовок X-CallQA-Signature: sha256=HMAC-SHA256(секрет, "<X-CallQA-Timestamp>.<тело>").
Доставка: до 4 попыток (сразу, через 10 с, 60 с, 5 мин), успех — любой ответ 2xx; каждая попытка пишется в webhook_log.
Очередь повторов хранится в памяти службы: при перезапуске невыполненные повторы теряются (звонок и его результат остаются, их можно забрать через API).
"""
import hashlib
import hmac
import json
import secrets
import threading
import time

import httpx

from core import q

EVENTS = {"call.done": "звонок оценён", "call.error": "ошибка обработки", "call.reviewed": "звонок проверен человеком", "ping": "проверка связи"}
RETRY_DELAYS = (0, 10, 60, 300)


def valid_url(url: str) -> bool:
    if not url.startswith(("http://", "https://")) or len(url) > 500 or any(c.isspace() for c in url):
        return False
    return bool(url.split("//", 1)[1].split("/")[0])


def new_secret() -> str:
    return "whs_" + secrets.token_urlsafe(24)


def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def _post(owner, event: str, job_id: str, payload: dict, attempt: int) -> tuple:
    """Одна попытка доставки. Возвращает (успех, пояснение)."""
    body = json.dumps({"event": event, **payload}, ensure_ascii=False).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json; charset=utf-8", "X-CallQA-Event": event, "X-CallQA-Timestamp": ts,
               "X-CallQA-Signature": sign(owner["webhook_secret"] or "", ts, body), "User-Agent": "CallQA"}
    code, err = None, ""
    try:
        r = httpx.post(owner["webhook_url"], content=body, headers=headers, timeout=10, follow_redirects=False)
        code = r.status_code
        ok = 200 <= code < 300
        if not ok:
            err = f"ответ {code}"
    except Exception as e:
        ok, err = False, f"{type(e).__name__}: {str(e)[:150]}"
    q("INSERT INTO webhook_log(ts, owner, job_id, event, url, attempt, status_code, ok, error) VALUES(?,?,?,?,?,?,?,?,?)",
      time.time(), owner["id"], job_id, event, owner["webhook_url"], attempt, code, 1 if ok else 0, err)
    return ok, err or "доставлено"


def _run(owner_id: int, event: str, job_id: str, payload: dict):
    for attempt, delay in enumerate(RETRY_DELAYS, 1):
        time.sleep(delay)
        owner = q("SELECT * FROM users WHERE id=?", owner_id).fetchone()  # настройки могли измениться между попытками
        if not owner or not owner["webhook_enabled"] or not owner["webhook_url"]:
            return
        if _post(owner, event, job_id, payload, attempt)[0]:
            return


def fire(owner_id, event: str, job_id: str, payload: dict):
    """Ставит доставку в фон; ошибки никогда не мешают обработке звонков."""
    try:
        owner = q("SELECT webhook_url, webhook_enabled FROM users WHERE id=?", owner_id).fetchone()
        if owner and owner["webhook_enabled"] and owner["webhook_url"]:
            threading.Thread(target=_run, args=(owner_id, event, job_id, payload), daemon=True).start()
    except Exception:
        pass


def ping(owner_id: int) -> tuple:
    """Проверка связи (синхронно, одна попытка)."""
    owner = q("SELECT * FROM users WHERE id=?", owner_id).fetchone()
    if not owner or not owner["webhook_url"]:
        return False, "адрес веб-хука не задан"
    return _post(owner, "ping", "", {"message": "Проверка связи от Call QA"}, 1)


def recent(owner_id: int, limit: int = 10) -> list:
    return q("SELECT * FROM webhook_log WHERE owner=? ORDER BY ts DESC LIMIT ?", owner_id, limit).fetchall()
