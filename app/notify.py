"""Уведомления в Telegram. Токен бота — только в файле окружения сервера (CALLQA_TG_TOKEN), chat id — у клиента.
В сообщении минимум данных: балл, сотрудник, чек-лист и ссылка; цитаты и резюме звонка не отправляем."""
import os
import re

import httpx

CHAT_RE = re.compile(r"^-?\d{5,15}$")
LOW_SCORE = 60


def configured() -> bool:
    return bool(os.environ.get("CALLQA_TG_TOKEN"))


def send(chat_id: str, text: str) -> tuple:
    """Возвращает (успех, пояснение)."""
    token = os.environ.get("CALLQA_TG_TOKEN")
    if not token:
        return False, "токен бота не задан администратором"
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=15)
        if r.status_code == 200:
            return True, "отправлено"
        return False, (r.json().get("description") or f"ошибка {r.status_code}")
    except Exception as e:
        return False, f"нет связи с Telegram: {type(e).__name__}"


def low_score_text(total: int, employee: str, rubric_name: str, job_id: str) -> str:
    base = os.environ.get("CALLQA_BASE_URL", "http://localhost:8130").rstrip("/")
    return f"⚠ Низкий балл: {total} из 100\nСотрудник: {employee or '—'}\nЧек-лист: {rubric_name}\n{base}/job/{job_id}"
