"""Общее для клиентской и админской частей: БД, учётки, сессии, настройки, разметка страниц."""
import hashlib
import hmac
import html
import itertools
import os
import queue
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import psycopg2
import psycopg2.extensions
import psycopg2.extras

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import brand

DATA = Path(os.environ.get("CALLQA_DATA", "/data"))
MAX_MB = int(os.environ.get("CALLQA_MAX_MB", "200"))
DATA.mkdir(parents=True, exist_ok=True)
(DATA / "audio").mkdir(exist_ok=True)

_db_lock = threading.Lock()


class JobQueue:
    """Очередь обработки с приоритетами клиентов: сначала высокий приоритет, внутри одного — по порядку постановки.
    Приоритет берётся у владельца звонка в момент постановки (позже его смена очередь не переставляет)."""

    def __init__(self):
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._n = itertools.count()

    def put(self, item: str):
        r = q("SELECT u.priority FROM jobs j JOIN users u ON u.id=j.owner WHERE j.id=?", item.removeprefix("R:")).fetchone()
        self._q.put((-((r or {}).get("priority") or 0), next(self._n), item))

    def get(self) -> str:
        return self._q.get()[2]

    def qsize(self) -> int:
        return self._q.qsize()

    def position(self, jid: str):
        """Сколько задач в очереди стоит перед этой (None — её в очереди нет)."""
        with self._q.mutex:
            order = [it[2].removeprefix("R:") for it in sorted(self._q.queue)]
        return order.index(jid) if jid in order else None


jobs = JobQueue()
STOP: set = set()  # id звонков, которые пользователь просит остановить прямо сейчас (обработка проверяет между шагами)
PG_DSN = os.environ.get("CALLQA_PG_DSN", "")
if not PG_DSN:
    raise RuntimeError("Не задан CALLQA_PG_DSN: адрес PostgreSQL (env-файл службы callqa-web)")
psycopg2.extensions.register_type(psycopg2.extensions.new_type(psycopg2.extensions.DECIMAL.values, "DEC2FLOAT",
                                                               lambda v, c: float(v) if v is not None else None))  # avg() и sum() отдаём числами, как раньше
_conn = None


def _connect():
    c = psycopg2.connect(PG_DSN, connect_timeout=5, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    return c


def q(sql, *args):
    """Запрос к PostgreSQL. Плейсхолдеры `?` как в SQLite; при обрыве связи один раз переподключается."""
    global _conn
    sql = sql.replace("%", "%%").replace("?", "%s")
    with _db_lock:
        for attempt in (0, 1):
            try:
                if _conn is None or _conn.closed:
                    _conn = _connect()
                cur = _conn.cursor()
                cur.execute(sql, args)
                return cur
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                _conn = None
                if attempt:
                    raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, created DOUBLE PRECISION, filename TEXT, rubric TEXT, employee TEXT,
  status TEXT, error TEXT, duration INTEGER, transcript TEXT, result TEXT, asr_sec DOUBLE PRECISION, llm_sec DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, username TEXT UNIQUE, org TEXT, role TEXT,
  pw_hash TEXT, active INTEGER DEFAULT 1, api_enabled INTEGER DEFAULT 0, api_key_hash TEXT, api_key_prefix TEXT,
  created DOUBLE PRECISION, last_login DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS rubrics(id SERIAL PRIMARY KEY, owner INTEGER, name TEXT, employee_title TEXT, context TEXT,
  items TEXT, version INTEGER DEFAULT 1, created DOUBLE PRECISION, updated DOUBLE PRECISION);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS review TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS peaks TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS owner INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tok_in INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tok_out INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asr_engine TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS llm_model TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS rubric_snapshot TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tg_notified INTEGER DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS audio_deleted DOUBLE PRECISION;
CREATE TABLE IF NOT EXISTS retention_log(id SERIAL PRIMARY KEY, ts DOUBLE PRECISION, job_id TEXT, owner INTEGER, file TEXT, bytes BIGINT, reason TEXT);
ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_chat_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_enabled INTEGER DEFAULT 0;
CREATE INDEX IF NOT EXISTS jobs_owner_created ON jobs(owner, created DESC);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS progress INTEGER DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stage TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lang TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS role_edits INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_limit INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS queue_limit INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS webhook_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS webhook_secret TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS webhook_enabled INTEGER DEFAULT 0;
CREATE TABLE IF NOT EXISTS hidden_templates(owner INTEGER, key TEXT, PRIMARY KEY(owner, key));
CREATE TABLE IF NOT EXISTS employees(id SERIAL PRIMARY KEY, owner INTEGER, name TEXT, job_title TEXT, phone TEXT, created DOUBLE PRECISION);
CREATE UNIQUE INDEX IF NOT EXISTS employees_owner_name ON employees(owner, name);
CREATE TABLE IF NOT EXISTS audit_log(id SERIAL PRIMARY KEY, ts DOUBLE PRECISION, actor_id INTEGER, actor TEXT, action TEXT, target TEXT, details TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log(ts DESC);
CREATE TABLE IF NOT EXISTS webhook_log(id SERIAL PRIMARY KEY, ts DOUBLE PRECISION, owner INTEGER, job_id TEXT, event TEXT, url TEXT, attempt INTEGER, status_code INTEGER, ok INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS webhook_log_owner_ts ON webhook_log(owner, ts DESC);
"""
q(SCHEMA)

# Представления для Metabase и Label Studio: те же таблицы, но JSON разложен в колонки (итоговый балл считается так же, как в приложении).
q("""
CREATE OR REPLACE VIEW qa_criteria AS
SELECT j.id AS call_id, it->>'code' AS code, rb.w->>'title' AS title, (rb.w->>'weight')::float AS weight,
       (it->>'score')::int AS model_score,
       CASE WHEN j.review IS NOT NULL THEN (j.review::jsonb->'scores'->>(it->>'code'))::int END AS human_score,
       it->>'reason' AS reason
FROM jobs j
CROSS JOIN LATERAL jsonb_array_elements(j.result::jsonb->'items') it
LEFT JOIN LATERAL (SELECT x AS w FROM jsonb_array_elements(coalesce(j.rubric_snapshot, '{"items":[]}')::jsonb->'items') x
                   WHERE x->>'code' = it->>'code' LIMIT 1) rb ON true
WHERE j.result IS NOT NULL""")
q("""
CREATE OR REPLACE VIEW qa_calls AS
SELECT j.id, to_timestamp(j.created) AS created, coalesce(u.org, u.username) AS client, j.employee, j.filename,
       j.rubric AS rubric_code, j.status, j.duration AS duration_s,
       (j.result::jsonb->>'total')::int AS model_total,
       CASE WHEN j.review IS NOT NULL THEN fin.t ELSE (j.result::jsonb->>'total')::int END AS final_total,
       (j.review IS NOT NULL) AS reviewed,
       j.result::jsonb->>'topic' AS topic, j.result::jsonb->>'outcome' AS outcome, j.result::jsonb->>'summary' AS summary,
       j.review::jsonb->>'comment' AS review_comment, j.asr_engine, j.llm_model, j.asr_sec, j.llm_sec, j.tok_in, j.tok_out, j.error
FROM jobs j
LEFT JOIN users u ON u.id = j.owner
LEFT JOIN LATERAL (SELECT round(100 * sum(weight * human_score / 2.0) / nullif(sum(weight), 0))::int AS t
                   FROM qa_criteria c WHERE c.call_id = j.id AND c.human_score IS NOT NULL) fin ON true""")
q("""
CREATE OR REPLACE VIEW qa_segments AS
SELECT j.id AS call_id, s.n::int AS n, (s.e->>'t')::float AS t, s.e->>'spk' AS speaker, s.e->>'text' AS text
FROM jobs j
CROSS JOIN LATERAL jsonb_array_elements(j.transcript::jsonb) WITH ORDINALITY AS s(e, n)
WHERE j.transcript IS NOT NULL""")


# ---------- настройки ----------
DEFAULTS = {"llm_model": "qwen3:8b", "asr_engine": "gigaam-rnnt", "whisper_lang": "ru",
            "llm_mode": "local", "llm_api_url": "", "llm_api_key": "", "llm_api_model": "",
            "asr_device": "cpu", "asr_api_url": "", "asr_api_key": "", "asr_api_model": "whisper-1",
            "max_minutes": "90",  # лимит длины записи, минут; 0 — без ограничения
            "retention_days": "90",  # срок хранения аудио, дней (3 месяца); 0 — хранить бессрочно
            "mono_diarize": "off"}  # разделение голосов в моно по акустике: off — роли только по смыслу (LLM), on — сначала по голосу


LANGS = {"ru": "русский", "kk": "казахский", "auto": "определять автоматически (смесь русского и казахского)"}  # язык записи, выбираемый при загрузке


def setting(key: str) -> str:
    r = q("SELECT value FROM settings WHERE key=?", key).fetchone()
    return r["value"] if r else DEFAULTS[key]


def set_setting(key: str, value: str):
    q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)


def purge_audio() -> int:
    """Удаляет аудио законченных звонков старше срока хранения (расшифровка и оценка остаются). Каждое удаление пишется в retention_log."""
    try:
        days = int(setting("retention_days"))
    except ValueError:
        return 0
    if days <= 0:
        return 0
    now = time.time()
    n = 0
    rows = q("SELECT id, owner FROM jobs WHERE created < ? AND audio_deleted IS NULL AND status IN ('done','error','stopped')", now - days * 86400).fetchall()
    for r in rows:
        for p in (DATA / "audio").glob(f"{r['id']}.*"):
            try:
                size = p.stat().st_size
                p.unlink()
            except OSError:
                continue
            q("INSERT INTO retention_log(ts, job_id, owner, file, bytes, reason) VALUES(?,?,?,?,?,?)", now, r["id"], r["owner"], p.name, size, f"срок хранения {days} дн.")
            n += 1
        q("UPDATE jobs SET audio_deleted=? WHERE id=?", now, r["id"])
    return n


# ---------- пароли, ключи, сессии ----------
def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex(), h)


def _secret() -> bytes:
    p = DATA / "secret.key"
    if not p.exists():
        p.write_text(secrets.token_hex(32))
        p.chmod(0o600)
    return p.read_text().encode()


SECRET = _secret()
SESSION_TTL = 12 * 3600
COOKIE = "cq_session"


def _fp(pw_hash: str) -> str:
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:10]


def make_session(uid: int, pw_hash: str) -> str:
    """Токен привязан к отпечатку пароля: после смены пароля старые сессии перестают работать."""
    exp = int(time.time()) + SESSION_TTL
    body = f"{uid}.{exp}.{_fp(pw_hash)}"
    return f"{body}.{hmac.new(SECRET, body.encode(), 'sha256').hexdigest()}"


def read_session(token: str):
    """Возвращает (id пользователя, отпечаток пароля) или None."""
    try:
        uid, exp, fp, sig = token.split(".")
        if not hmac.compare_digest(hmac.new(SECRET, f"{uid}.{exp}.{fp}".encode(), "sha256").hexdigest(), sig) or int(exp) < time.time():
            return None
        return int(uid), fp
    except Exception:
        return None


def set_session_cookie(resp, request: Request, user):
    """Secure-кука включается, когда запрос пришёл по HTTPS (в том числе через прокси с X-Forwarded-Proto)."""
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    resp.set_cookie(COOKIE, make_session(user["id"], user["pw_hash"]), max_age=SESSION_TTL, httponly=True, samesite="lax", secure=secure)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key(uid: int) -> str:
    key = "cqk_" + secrets.token_urlsafe(30)
    q("UPDATE users SET api_key_hash=?, api_key_prefix=? WHERE id=?", hash_key(key), key[:10], uid)
    return key


def bootstrap_admin():
    """Первый запуск: администратор из переменных окружения; прежние звонки (без владельца) переходят к нему."""
    if q("SELECT count(*) c FROM users").fetchone()["c"] == 0:  # пароль из окружения нужен только здесь; после первого запуска его можно убрать из файла
        q("INSERT INTO users(username,org,role,pw_hash,created) VALUES(?,?,?,?,?)", os.environ["CALLQA_WEB_USER"], "Организация", "admin",
          hash_pw(os.environ["CALLQA_WEB_PASSWORD"]), time.time())
    aid = q("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()["id"]
    q("UPDATE jobs SET owner=? WHERE owner IS NULL", aid)


bootstrap_admin()


class LoginRequired(Exception):
    pass


def current_user(request: Request):
    s = read_session(request.cookies.get(COOKIE, ""))
    u = q("SELECT * FROM users WHERE id=? AND active=1", s[0]).fetchone() if s else None
    if not u or _fp(u["pw_hash"]) != s[1]:
        raise LoginRequired()
    return u


def admin_user(u=Depends(current_user)):
    if u["role"] != "admin":
        raise HTTPException(403, "только для администратора")
    return u


def api_user(request: Request):
    """Пользователь по ключу API (Authorization: Bearer ...); только если администратор включил API."""
    h = request.headers.get("authorization", "")
    key = h.removeprefix("Bearer ").strip() if h.lower().startswith("bearer ") else ""
    u = q("SELECT * FROM users WHERE api_key_hash=? AND active=1", hash_key(key)).fetchone() if key else None
    if not u:
        raise HTTPException(401, "неверный ключ API")
    if not u["api_enabled"]:
        raise HTTPException(403, "API для вашей учётной записи не включён")
    return u


# ---------- журнал действий ----------
def audit(actor, action: str, target: str = "", details: str = "", ip: str = ""):
    """Запись в журнал действий администратора. actor — строка пользователя или логин (для неудачного входа)."""
    uid, name = (actor["id"], actor["username"]) if not isinstance(actor, str) else (None, actor)
    q("INSERT INTO audit_log(ts, actor_id, actor, action, target, details, ip) VALUES(?,?,?,?,?,?,?)", time.time(), uid, name, action, target, details[:1000], ip)


# ---------- загрузка файлов ----------
def day_start() -> float:
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))


def upload_allowance(uid: int):
    """Сколько записей клиент ещё может поставить сейчас (None — без ограничения) и почему именно столько."""
    u = q("SELECT daily_limit, queue_limit FROM users WHERE id=?", uid).fetchone()
    if not u:
        return None, ""
    left, why = None, ""
    if (u["daily_limit"] or 0) > 0:
        used = q("SELECT count(*) c FROM jobs WHERE owner=? AND created>=?", uid, day_start()).fetchone()["c"]
        left, why = max(0, u["daily_limit"] - used), f"дневной лимит {u['daily_limit']} записей"
    if (u["queue_limit"] or 0) > 0:
        busy = q("SELECT count(*) c FROM jobs WHERE owner=? AND status IN ('queued','asr','scoring')", uid).fetchone()["c"]
        room = max(0, u["queue_limit"] - busy)
        if left is None or room < left:
            left, why = room, f"лимит очереди {u['queue_limit']} записей (дождитесь обработки)"
    return left, why


# Только аудио: файл потом отдаётся браузеру, а HTML/SVG с скриптом выполнился бы в сессии администратора.
AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
               ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".amr": "audio/amr", ".wma": "audio/x-ms-wma",
               ".webm": "audio/webm", ".3gp": "audio/3gpp", ".mka": "audio/x-matroska", ".gsm": "audio/x-gsm"}


def save_uploads(files, rubric: str, employee: str, owner: int, lang: str = ""):
    """Сохраняет записи и ставит в очередь. Возвращает (id задач, пропущенные имена, имена, не принятые из-за лимита, причина лимита)."""
    ids, skipped, over = [], [], []
    left, why = upload_allowance(owner)
    for up in files:
        if not up.filename:
            continue
        if left is not None and len(ids) >= left:
            over.append(up.filename)
            continue
        ext = Path(up.filename).suffix.lower()
        if ext not in AUDIO_TYPES:
            skipped.append(up.filename)
            continue
        jid = uuid.uuid4().hex[:12]
        dst = DATA / "audio" / f"{jid}{ext}"
        size, too_big = 0, False
        with dst.open("wb") as f:
            while chunk := up.file.read(1 << 20):
                size += len(chunk)
                if size > MAX_MB << 20:
                    too_big = True
                    break
                f.write(chunk)
        if too_big or size == 0:
            dst.unlink(missing_ok=True)
            skipped.append(up.filename)
            continue
        q("INSERT INTO jobs(id,created,filename,rubric,employee,status,owner,lang,stage,progress) VALUES(?,?,?,?,?, 'queued', ?, ?, 'queue', 0)",
          jid, time.time() + len(ids) * 0.001, up.filename, rubric, employee.strip(), owner, lang if lang in LANGS else None)
        jobs.put(jid)
        ids.append(jid)
    return ids, skipped, over, why


# ---------- разметка ----------
def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def mmss(s) -> str:
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


CSS = Path(__file__).parent / "static" / "app.css"
CSS_URL = f"/static/app.css?v={int(CSS.stat().st_mtime)}"  # версия в адресе: после выкладки браузер не держит старые стили
TAGLINE = "контроль качества звонков"
# длинный текст в ячейках таблицы обрезается многоточием; полный текст показываем подсказкой
CELL_TITLES = "document.querySelectorAll('table.list:not(.plain) td').forEach(function(c){if(c.scrollWidth>c.clientWidth&&!c.title)c.title=c.textContent.trim()})"
FOOTER = ""  # подпись под формой входа (по желанию)


def brand_block(dark: bool) -> str:
    """Название и значок в шапке: светлые на тёмной панели, цветные на белом (вход, ошибки)."""
    return (f'<div class="brand{"" if dark else " dark"}"><div class="brandrow"><img src="/favicon.svg" alt="">'
            f'<b>{brand.APP_NAME}</b></div><small>{TAGLINE}</small></div>')


def error_page(code: int, text: str) -> HTMLResponse:
    """Страница ошибки в фирменном стиле (для браузера; API отвечает JSON)."""
    return HTMLResponse(f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>Ошибка {code} · Call QA</title>{brand.HEAD}<link rel="stylesheet" href="{CSS_URL}"></head><body class="loginbody">'
                        f'<div class="card login">{brand_block(False)}<h2 class="errcode">{code}</h2><p class="muted">{esc(text)}</p>'
                        f'<a class="btn p" href="/">На главную</a></div></body></html>', status_code=code)


def nav_for(user) -> list:
    if user["role"] == "admin":
        return [("clients", "/admin/clients", "Клиенты"), ("feed", "/", "Все звонки"), ("sum", "/summary", "Сводка"),
                ("quality", "/quality", "Качество модели"), ("staff", "/employees", "Сотрудники"),
                ("new", "/admin/new", "Новая учётка"), ("models", "/admin/models", "Модели"), ("server", "/admin/server", "Сервер"),
                ("rub", "/rubrics", "Чек-листы"), ("profile", "/profile", "Профиль")]
    items = [("feed", "/", "Лента звонков"), ("sum", "/summary", "Сводка"), ("quality", "/quality", "Качество модели"),
             ("staff", "/employees", "Сотрудники"), ("rub", "/rubrics", "Чек-листы")]
    if user["api_enabled"]:
        items.append(("api", "/api-access", "API"))
    items.append(("profile", "/profile", "Профиль"))
    return items


def layout(active: str, title: str, body: str, user, topright: str = "", refresh: bool = False) -> HTMLResponse:
    nav = "".join(f'<a href="{h}" class="{"on" if k == active else ""}"><i></i>{t}</a>' for k, h, t in nav_for(user))
    meta = '<meta http-equiv="refresh" content="6">' if refresh else ""
    who = user["org"] or user["username"]
    role = "администратор" if user["role"] == "admin" else "клиент"
    return HTMLResponse(f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
                        f'<title>{esc(re.sub(r"<[^>]+>", "", title))} · Call QA</title>{brand.HEAD}<link rel="stylesheet" href="{CSS_URL}"></head><body>'
                        f'<header class="hd"><div class="hd1"><a class="logo" href="/">{brand_block(True)}</a>'
                        f'<div class="user"><span class="who">{esc(who)}<small>{role} · {esc(user["username"])}</small></span>'
                        f'<form method="post" action="/logout"><button class="logout" title="Выйти из учётной записи">Выйти</button></form></div></div>'
                        f'<nav>{nav}</nav></header>'
                        f'<main><div class="top"><h1>{title}</h1><div class="sp"></div>{topright}</div><div class="content">{body}</div></main><script>{CELL_TITLES}</script></body></html>')


def login_page(error: str = "") -> HTMLResponse:
    err = f'<div class="banner">{esc(error)}</div>' if error else ""
    return HTMLResponse(f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>Вход · Call QA</title>{brand.HEAD}<meta property="og:title" content="Call QA"><meta property="og:description" content="{TAGLINE}">'
                        f'<link rel="stylesheet" href="{CSS_URL}"></head><body class="loginbody">'
                        f'<form class="card login" method="post" action="/login">{brand_block(False)}'
                        f'{err}<label>Логин<input class="inp" name="username" autofocus required autocomplete="username"></label>'
                        f'<label>Пароль<input class="inp" type="password" name="password" required autocomplete="current-password"></label>'
                        f'<button class="btn p">Войти</button>{FOOTER}</form></body></html>')


def redirect(path: str, **params) -> RedirectResponse:
    return RedirectResponse(path + ("?" + urlencode(params) if params else ""), 303)
