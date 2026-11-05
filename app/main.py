"""Call QA: загрузка записи → распознавание → оценка по чек-листу (LLM через LiteLLM) → проверка человеком.

Запуск: uvicorn main:app --host 0.0.0.0 --port 8130  (переменные окружения — в deploy/callqa-web.service)
Две части: клиентская (лента, карточка звонка, сводка, чек-листы, API) и админская (/admin: клиенты, модели, сервер).
Очередь одна: задачи выполняются по одному звонку (CPU-распознавание, одна модель оценки).
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.exception_handlers import http_exception_handler as default_http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

import account
import admin
import brand
import asr
import hooks
import insight
import notify
import rubrics
import scoring
import exports
from core import (AUDIO_TYPES, COOKIE, DATA, LANGS, MAX_MB, STOP, LoginRequired, api_user, audit, check_pw, current_user, error_page, esc, jobs, layout, login_page,
                  mmss, new_api_key, purge_audio, q, redirect, save_uploads, set_session_cookie, setting, upload_allowance)

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Call QA")
app.include_router(brand.router)
app.include_router(admin.router)
app.include_router(account.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


STATUS = {"queued": "В очереди", "asr": "Распознаю речь…", "scoring": "Оцениваю…", "done": "Оценён моделью", "error": "Ошибка", "stopped": "Остановлен"}
OUTCOME = {"agreed": "договорились", "not_agreed": "не договорились", "needs_followup": "нужен повторный контакт",
           "resolved": "решено", "escalated": "передано выше", "other": "другое"}
FLAGS = {"conflict": "конфликт", "complaint": "жалоба", "critical_violation": "критичное нарушение",
         "poor_audio": "плохой звук", "not_a_client_call": "не разговор с клиентом"}


# ---------- обработка очереди ----------
def notify_low_score(jid: str, total, owner):
    """Telegram при итоговом балле ниже порога: один раз на звонок, ошибки отправки не ломают обработку."""
    try:
        if total is None or total >= notify.LOW_SCORE or not owner or not owner["tg_enabled"] or not owner["tg_chat_id"]:
            return
        if q("UPDATE jobs SET tg_notified=1 WHERE id=? AND coalesce(tg_notified,0)=0", jid).rowcount:
            row = q("SELECT * FROM jobs WHERE id=?", jid).fetchone()
            notify.send(owner["tg_chat_id"], notify.low_score_text(total, row["employee"], rubrics.name(row["rubric"]), jid))
    except Exception:
        pass


def max_seconds() -> int:
    """Лимит длины записи из настроек (минуты → секунды); 0 — без ограничения."""
    try:
        return max(0, int(setting("max_minutes"))) * 60
    except ValueError:
        return 0


# этапы обработки и доля общей шкалы, которую занимает каждый: ожидание 0 %, распознавание 0–60 %, роли 60–70 %, оценка моделью 70–100 %
STAGES = [("queue", "В очереди"), ("asr", "Распознавание речи"), ("roles", "Расстановка ролей"), ("scoring", "Оценка по чек-листу")]
STAGE_START = {"queue": 0, "asr": 0, "roles": 60, "scoring": 70}


def set_stage(jid: str, stage: str, pct: int = None):
    q("UPDATE jobs SET stage=?, progress=? WHERE id=?", stage, STAGE_START[stage] if pct is None else pct, jid)


def pick_engine(lang: str) -> str:
    """GigaAM понимает только русский: для казахского и смеси языков на процессоре берём Whisper."""
    engine = setting("asr_engine")
    if setting("asr_device") == "cpu" and engine.startswith("gigaam") and lang in ("kk", "auto"):
        return "whisper-large-v3-turbo"
    return engine


def webhook_payload(r) -> dict:
    """Что уходит клиенту в веб-хуке: без расшифровки и цитат, только итог и ссылка (всё остальное — через API)."""
    base = os.environ.get("CALLQA_BASE_URL", "http://localhost:8130").rstrip("/")
    res = json.loads(r["result"]) if r["result"] else None
    return {"id": r["id"], "status": r["status"], "filename": r["filename"], "employee": r["employee"], "rubric": r["rubric"],
            "created": r["created"], "duration": r["duration"], "total": final_total(r), "error": r["error"],
            "flags": res["flags"] if res else [], "url": f"{base}/job/{r['id']}", "api_url": f"{base}/api/v1/calls/{r['id']}"}


def fire_webhook(jid: str, event: str):
    try:
        r = q("SELECT * FROM jobs WHERE id=?", jid).fetchone()
        if r and r["owner"]:
            hooks.fire(r["owner"], event, jid, webhook_payload(r))
    except Exception:
        pass


def worker():
    while True:
        jid = jobs.get()
        redo = jid.startswith("R:")  # только повторная оценка по готовой расшифровке (после правки ролей)
        jid = jid.removeprefix("R:")
        row = q("SELECT * FROM jobs WHERE id=?", jid).fetchone()
        if not row or row["status"] != "queued":  # остановлен, удалён или уже обработан, пока ждал в очереди
            STOP.discard(jid)
            continue

        def stop_now(jid=jid):
            return jid in STOP

        try:
            lang = row["lang"] or setting("whisper_lang")  # язык, выбранный при загрузке, важнее общей настройки
            asr.CONFIG.update(engine=pick_engine(lang), whisper_lang=lang, device=setting("asr_device"),
                              api_url=setting("asr_api_url"), api_key=setting("asr_api_key"), api_model=setting("asr_api_model"))
            if setting("llm_mode") == "api":
                scoring.CONFIG.update(llm_model=setting("llm_api_model"), url=setting("llm_api_url"), key=setting("llm_api_key"))
            else:
                scoring.CONFIG.update(llm_model=setting("llm_model"), url=scoring.LLM_URL, key=scoring.LLM_KEY)
            scoring.USAGE.update({"in": 0, "out": 0})
            owner = q("SELECT * FROM users WHERE id=?", row["owner"]).fetchone()
            if redo and row["rubric_snapshot"]:  # повторная оценка идёт по тому же чек-листу, что и первая
                rub = json.loads(row["rubric_snapshot"])
            else:
                rub = rubrics.effective(row["rubric"], bool(owner and owner["role"] == "admin"))
            if rub is None:
                raise RuntimeError("Чек-лист удалён, выберите другой и загрузите запись заново")
            if redo:
                q("UPDATE jobs SET status='scoring', stage='scoring', progress=70, error=NULL WHERE id=?", jid)
                segments = json.loads(row["transcript"])
            else:
                src = next((DATA / "audio").glob(f"{jid}.*"), None)
                if src is None:
                    raise RuntimeError("Аудио удалено (срок хранения), загрузите запись заново")
                q("UPDATE jobs SET status='asr', stage='asr', progress=0, error=NULL WHERE id=?", jid)
                t0 = time.time()
                last = [0.0, -1]  # время и процент последней записи: базу не дёргаем чаще раза в 1,5 с

                def on_progress(frac, jid=jid):
                    pct = int(60 * frac)
                    if pct != last[1] and time.time() - last[0] > 1.5:
                        last[:] = [time.time(), pct]
                        q("UPDATE jobs SET progress=? WHERE id=?", pct, jid)

                with tempfile.TemporaryDirectory() as tmp:
                    tr = asr.transcribe(src, Path(tmp), stop_now, max_seconds(), on_progress, setting("mono_diarize") == "on")
                asr_sec = time.time() - t0
                if stop_now():
                    raise asr.Stopped()
                set_stage(jid, "roles")
                segments = scoring.label_speakers(tr["segments"], tr["stereo"], rub, tr["voices"])
                if stop_now():
                    raise asr.Stopped()
                q("UPDATE jobs SET status='scoring', stage='scoring', progress=70, duration=?, transcript=?, asr_sec=?, asr_engine=? WHERE id=?",
                  tr["duration"], json.dumps(segments, ensure_ascii=False), asr_sec, asr.engine_label() + (" · голоса" if tr["voices"] else ""), jid)
            t0 = time.time()
            res = scoring.score(rub, segments)
            if stop_now():  # запрос к модели нельзя прервать на полпути; результат отбрасываем
                raise asr.Stopped()
            q("UPDATE jobs SET status='done', stage='done', progress=100, result=?, llm_sec=?, llm_model=?, tok_in=coalesce(tok_in,0)+?, tok_out=coalesce(tok_out,0)+?, rubric_snapshot=? WHERE id=?",
              json.dumps(res, ensure_ascii=False), time.time() - t0, scoring.CONFIG["llm_model"], scoring.USAGE["in"], scoring.USAGE["out"],
              json.dumps(rub, ensure_ascii=False), jid)
            notify_low_score(jid, res.get("total"), owner)
            fire_webhook(jid, "call.done")
        except asr.Stopped:  # повторная оценка готового звонка возвращает его в «оценён» с прежним результатом
            q("UPDATE jobs SET status=?, stage=?, progress=?, error=NULL WHERE id=?", "done" if redo and row["result"] else "stopped",
              "done" if redo and row["result"] else "stopped", 100 if redo and row["result"] else 0, jid)
        except Exception as e:
            q("UPDATE jobs SET status='error', stage='error', error=? WHERE id=?", str(e)[:1000], jid)
            fire_webhook(jid, "call.error")
        finally:
            STOP.discard(jid)


def retention_loop():
    """Раз в сутки (и при старте) удаляет аудио старше срока хранения из настроек."""
    while True:
        try:
            purge_audio()
        except Exception:
            pass
        time.sleep(24 * 3600)


threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=retention_loop, daemon=True).start()
for r in q("SELECT id FROM jobs WHERE status IN ('queued','asr','scoring')").fetchall():  # после перезапуска
    q("UPDATE jobs SET status='queued', stage='queue', progress=0 WHERE id=?", r["id"])
    jobs.put(r["id"])


# ---------- вход и выход ----------
_attempts: dict = {}


@app.exception_handler(LoginRequired)
def need_login(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


@app.get("/health")
def health():
    """Проверка для Docker/мониторинга: приложение живо и база отвечает."""
    try:
        q("SELECT 1").fetchone()
    except Exception:
        raise HTTPException(503, "db")
    return {"status": "ok", "queue": jobs.qsize()}


@app.get("/login", response_class=HTMLResponse)
def login_form():
    return login_page()


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _attempts.get(ip, []) if t > time.time() - 300]
    if len(recent) >= 8:
        audit(username.strip()[:64], "вход заблокирован", details="слишком много попыток с одного адреса", ip=ip)
        return login_page("Слишком много попыток. Подождите 5 минут.")
    u = q("SELECT * FROM users WHERE username=? AND active=1", username.strip()).fetchone()
    if not u or not check_pw(password, u["pw_hash"]):
        _attempts[ip] = recent + [time.time()]
        audit(username.strip()[:64], "неудачный вход", ip=ip)
        return login_page("Неверный логин или пароль")
    q("UPDATE users SET last_login=? WHERE id=?", time.time(), u["id"])
    if u["role"] == "admin":
        audit(u, "вход администратора", ip=ip)
    resp = RedirectResponse("/admin/clients" if u["role"] == "admin" else "/", 303)
    set_session_cookie(resp, request, u)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE)
    return resp


ERR_TEXT = {403: "Нет доступа", 404: "Страница не найдена", 409: "Действие сейчас недоступно", 400: "Некорректный запрос"}


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """Браузеру — страница в фирменном стиле, API и остальным — привычный JSON."""
    if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/"):
        text = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else ERR_TEXT.get(exc.status_code, "Что-то пошло не так")
        return error_page(exc.status_code, text)
    return await default_http_exception_handler(request, exc)


@app.get("/static/app.css")
def css():
    return FileResponse(STATIC / "app.css", media_type="text/css")


# ---------- вспомогательное ----------
def to_sec(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def cls(total) -> str:
    return "hi" if total is not None and total >= 80 else "mid" if total is not None and total >= 60 else "lo"


def score_html(total) -> str:
    if total is None:
        return "—"
    return f'<div class="score {cls(total)}"><span>{total}</span><span class="bar"><i style="width:{total}%"></i></span></div>'


def job_rubric(r) -> dict:
    """Чек-лист звонка: снимок на момент оценки; у старых звонков — встроенный шаблон по ключу."""
    if r["rubric_snapshot"]:
        return json.loads(r["rubric_snapshot"])
    return rubrics.template(r["rubric"]) or {"code": r["rubric"], "version": "1", "employee_title": "Сотрудник", "items": []}


def human_total(rub: dict, scores: dict):
    got = tot = 0.0
    for it in rub["items"]:
        v = scores.get(it["code"])
        if v is None:
            continue
        got += it["weight"] * v / 2
        tot += it["weight"]
    return round(100 * got / tot) if tot else None


def final_total(r):
    """Балл с учётом правки человека, если она есть."""
    res = json.loads(r["result"]) if r["result"] else None
    if not res:
        return None
    rev = json.loads(r["review"]) if r["review"] else None
    return human_total(job_rubric(r), rev["scores"]) if rev else res.get("total")


def status_chip(r) -> str:
    if r["status"] != "done":
        label = STATUS[r["status"]]
        if r["status"] in ("asr", "scoring"):  # в ленте видно, на каком этапе и сколько прошло
            stage = dict(STAGES).get(r["stage"] or r["status"], "")
            label = f'{stage} · {r["progress"] or 0} %' if stage else label
        return f'<span class="chip {"r" if r["status"] == "error" else "a" if r["status"] == "stopped" else "n"}">{label}</span>'
    if r["review"]:
        return '<span class="chip g">Проверен</span>'
    res = json.loads(r["result"])
    if "critical_violation" in res["flags"] or any(i["confidence"] == "low" for i in res["items"]):
        return '<span class="chip a">Спорный пункт</span>'
    if (res.get("total") or 100) < 60:
        return '<span class="chip r">Низкий балл</span>'
    return '<span class="chip">Оценён моделью</span>'


def progress_html(r) -> str:
    """Этапы обработки звонка с шкалой: что сделано, что идёт сейчас, что впереди; для ожидающего — место в очереди."""
    order = [k for k, _ in STAGES]
    cur = "queue" if r["status"] == "queued" else (r["stage"] if r["stage"] in order else r["status"])
    if cur not in order:
        return ""
    steps = ""
    for k, name in STAGES:
        state = "done" if order.index(k) < order.index(cur) else "on" if k == cur else ""
        extra = f' · {r["progress"] or 0} %' if k == "asr" and state == "on" else ""
        steps += f'<li class="{state}">{name}{extra}</li>'
    note = ""
    if cur == "queue":
        ahead = jobs.position(r["id"])
        busy = q("SELECT count(*) c FROM jobs WHERE status IN ('asr','scoring')").fetchone()["c"]
        if ahead is not None:
            note = f'<p class="muted small">Перед этой записью в очереди: {ahead}' + (f', ещё одна обрабатывается сейчас' if busy else '') + '.</p>'
    elif cur == "scoring":
        note = '<p class="muted small">Модель оценивает разговор: точный процент неизвестен, обычно до минуты.</p>'
    pct = 0 if cur == "queue" else (r["progress"] or 0)
    return f'<div class="steps"><ul>{steps}</ul><div class="trk big"><i style="width:{pct}%"></i></div>{note}</div>'


def is_disputed(r) -> bool:
    return r["status"] == "done" and not r["review"] and "Спорный" in status_chip(r)


def scope(user, extra: str = ""):
    """Условие выборки звонков: клиент видит только свои, администратор — все."""
    if user["role"] == "admin":
        return (f"WHERE {extra}" if extra else ""), ()
    return f"WHERE owner=?{' AND ' + extra if extra else ''}", (user["id"],)


def jobs_of(user, extra: str = "", order: str = "created DESC"):
    where, args = scope(user, extra)
    return q(f"SELECT * FROM jobs {where} ORDER BY {order}", *args).fetchall()


def load(jid: str, user):
    r = q("SELECT * FROM jobs WHERE id=?", jid).fetchone()
    if not r or (user["role"] != "admin" and r["owner"] != user["id"]):
        raise HTTPException(404)
    return r


def filter_jobs(rows, emp: str = "", rubric: str = "", st: str = "", sc: str = "") -> list:
    """Фильтры ленты; те же условия применяются к выгрузке в Excel и PDF."""
    def keep(r):
        t = final_total(r)
        return ((not emp or (r["employee"] or "") == emp) and (not rubric or r["rubric"] == rubric) and
                (not st or (st == "disputed" and is_disputed(r)) or (st == "reviewed" and r["review"]) or (st == "pending" and r["status"] == "done" and not r["review"])
                 or (st == "error" and r["status"] == "error") or (st == "stopped" and r["status"] == "stopped")) and
                (not sc or (t is not None and ((sc == "lo" and t < 60) or (sc == "mid" and 60 <= t < 80) or (sc == "hi" and t >= 80)))))
    return [r for r in rows if keep(r)]


# ---------- лента ----------
@app.get("/", response_class=HTMLResponse)
def feed(user=Depends(current_user), emp: str = "", rubric: str = "", st: str = "", sc: str = "", msg: str = ""):
    rows = jobs_of(user)
    names = rubrics.all_names()
    admin_view = user["role"] == "admin"
    owners = {u["id"]: (u["org"] or u["username"]) for u in q("SELECT * FROM users").fetchall()} if admin_view else {}
    done = [r for r in rows if r["status"] == "done"]
    today = time.strftime("%Y-%m-%d")
    totals = [t for t in (final_total(r) for r in done) if t is not None]
    kpis = (f'<div class="card kpi"><small>Звонков всего</small><b>{len(rows)}</b><span>сегодня: {sum(1 for r in rows if time.strftime("%Y-%m-%d", time.localtime(r["created"])) == today)}</span></div>'
            f'<div class="card kpi"><small>Средний балл</small><b>{round(sum(totals) / len(totals), 1) if totals else "—"}</b><span>по {len(totals)} оценённым</span></div>'
            f'<div class="card kpi"><small>Ждут проверки</small><b>{sum(1 for r in done if not r["review"])}</b><span>из них спорных — {sum(1 for r in done if is_disputed(r))}</span></div>'
            f'<div class="card kpi"><small>Проверено людьми</small><b>{sum(1 for r in done if r["review"])}</b><span>{round(100 * sum(1 for r in done if r["review"]) / len(done)) if done else 0} % оценённых</span></div>')

    shown = filter_jobs(rows, emp, rubric, st, sc)
    qs = urlencode({k: v for k, v in (("emp", emp), ("rubric", rubric), ("st", st), ("sc", sc)) if v})  # те же фильтры уходят в экспорт
    emps = sorted({r["employee"] for r in rows if r["employee"]})
    dir_names = sorted({e["name"] for e in q("SELECT name FROM employees WHERE owner=?", user["id"]).fetchall()} | set(emps))  # подсказки при загрузке: справочник + прежние
    emp_opts = "".join('<option value="%s">' % esc(e) for e in dir_names)
    left, why = upload_allowance(user["id"])
    limit_note = f' · сейчас можно добавить: {left} ({why})' if left is not None else ""

    def sel(name, label, opts, cur):
        o = "".join(f'<option value="{esc(v)}" {"selected" if v == cur else ""}>{esc(t)}</option>' for v, t in opts)
        return f'<label class="sel"><small>{label}</small><select name="{name}" onchange="this.form.submit()">{o}</select></label>'
    filters = (f'<form class="filters" method="get">'
               + sel("emp", "Сотрудник", [("", "Все")] + [(e, e) for e in emps], emp)
               + sel("rubric", "Чек-лист", [("", "Все")] + (list(names.items()) if admin_view else rubrics.available(user, with_hidden=True)), rubric)
               + sel("sc", "Балл", [("", "любой"), ("hi", "80 и выше"), ("mid", "60–79"), ("lo", "ниже 60")], sc)
               + sel("st", "Статус", [("", "любой"), ("pending", "Ждут проверки"), ("disputed", "Спорные"), ("reviewed", "Проверены"), ("error", "Ошибка"), ("stopped", "Остановлены")], st)
               + '<a class="btn" href="/">Сбросить</a></form>')
    trs = ""
    for r in shown:
        t = final_total(r)
        res = json.loads(r["result"]) if r["result"] else None
        flag = '<td class="flag">⚑</td>' if res and ("critical_violation" in res["flags"] or "conflict" in res["flags"] or "complaint" in res["flags"]) else "<td></td>"
        own = f'<td>{esc(owners.get(r["owner"], "—"))}</td>' if admin_view else ""
        chk = f'<td onclick="event.stopPropagation()"><input type="checkbox" name="ids" value="{r["id"]}" onchange="selchg()"></td>'
        trs += (f'<tr onclick="location=\'/job/{r["id"]}\'">{chk}<td>{time.strftime("%d.%m %H:%M", time.localtime(r["created"]))}</td>{own}'
                f'<td>{esc(r["employee"] or "—")}</td><td>{esc(r["filename"])}</td><td><span class="chip n">{esc(names.get(r["rubric"], r["rubric"]))}</span></td>'
                f'<td>{mmss(r["duration"]) if r["duration"] else "—"}</td><td>{score_html(t)}</td><td>{status_chip(r)}</td>{flag}</tr>')
    cols = 10 if admin_view else 9
    if not trs:
        trs = f'<tr><td colspan="{cols}" class="muted" style="padding:28px;text-align:center">Нет звонков. Загрузите запись выше.</td></tr>'
    opts = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in rubrics.available(user)) or '<option value="">Нет чек-листов: создайте на странице «Чек-листы»</option>'
    upload = (f'<form class="card upload" id="upf" action="/upload" method="post" enctype="multipart/form-data"><b>Новые записи</b>'
              f'<label class="drop" id="drop">Перетащите файлы сюда или <u>выберите</u> (можно много)<span id="cnt" class="muted"></span>'
              f'<input type="file" name="audio" id="fl" multiple accept="audio/*,.wav,.mp3,.m4a,.ogg,.flac,.opus,.amr" hidden></label>'
              f'<label class="sel"><small>Чек-лист</small><select name="rubric">{opts}</select></label>'
              f'<label class="sel"><small>Язык записи</small><select name="lang"><option value="">по умолчанию ({esc(LANGS.get(setting("whisper_lang"), ""))})</option>'
              + "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in LANGS.items()) + '</select></label>'
              f'<input class="inp" name="employee" placeholder="Сотрудник (необязательно)" list="emps"><datalist id="emps">{emp_opts}</datalist>'
              f'<button class="btn p" id="go">Загрузить и оценить</button><span class="muted">до {MAX_MB} МБ каждая · ≈ 3–4 с распознавания на минуту записи, звонки идут по очереди{esc(limit_note)}</span>'
              f'<div class="prog" id="prog" hidden><i id="bar"></i></div></form><script>{UPLOAD_JS}</script>')
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    refresh = any(r["status"] in ("queued", "asr", "scoring") for r in rows)
    own_h = "<th>Клиент</th>" if admin_view else ""
    body = (f'{banner}{upload}<div class="kpis">{kpis}</div>{filters}<form method="post" action="/jobs/delete" id="delf" onsubmit="return confirm(\'Удалить выбранные звонки вместе с записями? Это нельзя отменить.\')">'
            f'<div class="card" style="overflow:auto"><table class="list"><tr><th><input type="checkbox" id="all" title="Выбрать все" onclick="selall(this)"></th><th>Время</th>{own_h}<th>Сотрудник</th><th>Файл</th><th>Чек-лист</th>'
            f'<th>Длит.</th><th>Балл</th><th>Статус</th><th></th></tr>{trs}</table><div class="pager"><span>Показано {len(shown)} из {len(rows)}</span> '
            f'<button class="btn dng" id="delb" hidden>Удалить выбранные (<span id="delc">0</span>)</button>'
            f'<button class="btn" id="stopb" hidden formaction="/jobs/stop" formnovalidate onclick="this.form.onsubmit=null">Остановить выбранные</button>'
            f'<span class="exp">Скачать список: <a class="btn sm" href="/export/calls.xlsx?{esc(qs)}">Excel</a> <a class="btn sm" href="/export/calls.pdf?{esc(qs)}">PDF</a></span></div></div></form><script>{SELECT_JS}</script>')
    return layout("feed", "Все звонки" if admin_view else "Лента звонков", body, user, '<span class="demo">пилот · тестовые данные</span>', refresh=refresh)


SELECT_JS = """
function boxes(){return document.querySelectorAll('input[name=ids]');}
function selchg(){var n=document.querySelectorAll('input[name=ids]:checked').length;document.getElementById('delb').hidden=!n;document.getElementById('stopb').hidden=!n;document.getElementById('delc').textContent=n;}
function selall(a){boxes().forEach(function(b){b.checked=a.checked});selchg();}
"""

UPLOAD_JS = """
var fl=document.getElementById('fl'),drop=document.getElementById('drop'),cnt=document.getElementById('cnt'),upf=document.getElementById('upf');
function names(){var n=fl.files.length;cnt.textContent=n?' — выбрано файлов: '+n:'';}
fl.addEventListener('change',names);
['dragover','dragenter'].forEach(function(e){drop.addEventListener(e,function(ev){ev.preventDefault();drop.classList.add('over')})});
['dragleave','drop'].forEach(function(e){drop.addEventListener(e,function(ev){ev.preventDefault();drop.classList.remove('over')})});
drop.addEventListener('drop',function(ev){fl.files=ev.dataTransfer.files;names();});
upf.addEventListener('submit',function(ev){ev.preventDefault();if(!fl.files.length){alert('Выберите файлы');return;}var x=new XMLHttpRequest(),pr=document.getElementById('prog'),bar=document.getElementById('bar');
pr.hidden=false;document.getElementById('go').disabled=true;
x.upload.onprogress=function(e){if(e.lengthComputable)bar.style.width=(100*e.loaded/e.total)+'%';};
x.onload=function(){location.href=x.responseURL||'/';};x.onerror=function(){alert('Загрузка не удалась');location.reload();};
x.open('POST','/upload');x.send(new FormData(upf));});
"""


@app.post("/upload")
def upload(audio: list[UploadFile] = File(...), rubric: str = Form(...), employee: str = Form(""), lang: str = Form(""), user=Depends(current_user)):
    """Одна или несколько записей за раз; все идут в общую очередь с одним чек-листом, сотрудником и языком."""
    if not rubrics.can_use(rubric, user):
        raise HTTPException(400, "неизвестный чек-лист")
    ids, skipped, over, why = save_uploads(audio, rubric, employee, user["id"], lang)
    if len(ids) == 1 and not skipped and not over:
        return RedirectResponse(f"/job/{ids[0]}", 303)
    msg = (f"Добавлено в очередь: {len(ids)}." + (f" Пропущены (пустые, больше {MAX_MB} МБ или не аудио): {', '.join(skipped)}." if skipped else "")
           + (f" Не приняты из-за лимита ({why}): {', '.join(over)}." if over else ""))
    return redirect("/", msg=msg)


# ---------- карточка звонка ----------
@app.get("/audio/{jid}")
def audio_file(jid: str, user=Depends(current_user)):
    load(jid, user)
    p = next((DATA / "audio").glob(f"{jid}.*"), None)
    if not p:
        raise HTTPException(404)
    # Тип задаём сами, а не по расширению файла; sandbox в CSP отключает скрипты, если файл открыть напрямую.
    return FileResponse(p, media_type=AUDIO_TYPES.get(p.suffix.lower(), "application/octet-stream"),
                        headers={"Content-Security-Policy": "sandbox", "Content-Disposition": "inline"})


@app.post("/job/{jid}/review")
async def review(jid: str, request: Request, user=Depends(current_user)):
    r = load(jid, user)
    if r["status"] != "done":
        raise HTTPException(409, "звонок ещё не оценён")
    body = await request.json()
    rub = job_rubric(r)
    codes = {i["code"] for i in rub["items"]}
    scores = {}
    for k, v in body.get("scores", {}).items():
        if k in codes and v in (0, 1, 2, None):
            scores[k] = v
    if set(scores) != codes:
        raise HTTPException(400, "нужны оценки по всем пунктам")
    q("UPDATE jobs SET review=? WHERE id=?", json.dumps({"scores": scores, "comment": str(body.get("comment", ""))[:2000], "by": user["username"], "at": time.time()}, ensure_ascii=False), jid)
    fire_webhook(jid, "call.reviewed")
    return {"ok": True, "total": human_total(rub, scores)}


@app.post("/job/{jid}/speaker")
async def set_speaker(jid: str, request: Request, user=Depends(current_user)):
    """Правка роли одной реплики: {"idx": номер, "spk": "employee"|"client"}."""
    r = load(jid, user)
    body = await request.json()
    segs = json.loads(r["transcript"] or "[]")
    idx, spk = body.get("idx"), body.get("spk")
    if not isinstance(idx, int) or not 0 <= idx < len(segs) or spk not in ("employee", "client"):
        raise HTTPException(400, "неверные параметры")
    changed = segs[idx].get("spk") != spk
    segs[idx]["spk"] = spk
    q("UPDATE jobs SET transcript=?, role_edits=coalesce(role_edits,0)+? WHERE id=?", json.dumps(segs, ensure_ascii=False), 1 if changed else 0, jid)  # число правок — для экрана «Качество модели»
    return {"ok": True}


@app.post("/job/{jid}/rescore")
def rescore(jid: str, user=Depends(current_user)):
    r = load(jid, user)
    if r["status"] in ("done", "error") and r["transcript"]:
        q("UPDATE jobs SET status='queued', stage='queue', progress=0 WHERE id=?", jid)
        jobs.put("R:" + jid)
    return RedirectResponse(f"/job/{jid}", 303)


def delete_jobs(ids, user) -> tuple:
    """Удаляет звонки и файлы записей. Идущие в работе (распознавание/оценка) не трогаем — сначала дождитесь или остановите."""
    done, busy = 0, 0
    for jid in ids:
        r = q("SELECT * FROM jobs WHERE id=?", jid).fetchone()
        if not r or (user["role"] != "admin" and r["owner"] != user["id"]):
            continue
        if r["status"] in ("asr", "scoring"):
            busy += 1
            continue
        q("DELETE FROM jobs WHERE id=?", jid)
        for p in (DATA / "audio").glob(f"{jid}.*"):
            p.unlink(missing_ok=True)
        if user["role"] == "admin" and r["owner"] != user["id"]:  # администратор удалил чужой звонок — в журнал
            audit(user, "удаление звонка клиента", jid, f'{r["filename"]} (клиент id {r["owner"]})')
        done += 1
    return done, busy


def _del_msg(done: int, busy: int) -> str:
    return f"Удалено звонков: {done}." + (f" Не удалено (идёт обработка — остановите её сначала): {busy}." if busy else "")


@app.post("/jobs/delete")
def delete_many(ids: list[str] = Form(default=[]), user=Depends(current_user)):
    done, busy = delete_jobs(ids, user)
    return redirect("/", msg=_del_msg(done, busy))


@app.post("/job/{jid}/delete")
def delete_one(jid: str, user=Depends(current_user)):
    load(jid, user)
    done, busy = delete_jobs([jid], user)
    return redirect("/", msg=_del_msg(done, busy)) if done else redirect(f"/job/{jid}", msg="Идёт обработка — сначала остановите её или дождитесь конца")


def stop_jobs(ids, user) -> tuple:
    """Остановка: ждущий в очереди снимается сразу; идущий останавливается на ближайшей границе (между репликами или после ответа модели)."""
    queued = running = 0
    for jid in ids:
        r = q("SELECT * FROM jobs WHERE id=?", jid).fetchone()
        if not r or (user["role"] != "admin" and r["owner"] != user["id"]) or r["status"] not in ("queued", "asr", "scoring"):
            continue
        STOP.add(jid)
        if r["status"] == "queued":
            # повторная оценка готового звонка возвращается в «оценён», остальные становятся «остановлен»
            q("UPDATE jobs SET status=? WHERE id=? AND status='queued'", "done" if r["result"] else "stopped", jid)
            queued += 1
        else:
            running += 1
    return queued, running


def _stop_msg(queued: int, running: int) -> str:
    if not queued and not running:
        return "Нечего останавливать: выбранные звонки не в обработке."
    return (f"Снято с очереди: {queued}. " if queued else "") + (f"Останавливается: {running} (прервётся в течение нескольких секунд, ответ модели дожидается). " if running else "")


@app.post("/jobs/stop")
def stop_many(ids: list[str] = Form(default=[]), user=Depends(current_user)):
    return redirect("/", msg=_stop_msg(*stop_jobs(ids, user)).strip())


@app.post("/job/{jid}/stop")
def stop_one(jid: str, user=Depends(current_user)):
    load(jid, user)
    return redirect(f"/job/{jid}", msg=_stop_msg(*stop_jobs([jid], user)).strip())


@app.post("/job/{jid}/retry")
def retry(jid: str, user=Depends(current_user)):
    r = load(jid, user)
    if r["status"] in ("error", "stopped"):
        STOP.discard(jid)
        q("UPDATE jobs SET status='queued', stage='queue', progress=0, error=NULL WHERE id=?", jid)
        jobs.put(jid)
    return RedirectResponse(f"/job/{jid}", 303)


@app.get("/job/{jid}", response_class=HTMLResponse)
def job(jid: str, msg: str = "", user=Depends(current_user)):
    r = load(jid, user)
    back = '<span class="back"><a href="/">‹ Лента</a> /</span>'
    delbtn = ("" if r["status"] in ("asr", "scoring") else
              f'<form method="post" action="/job/{jid}/delete" onsubmit="return confirm(\'Удалить звонок вместе с записью? Это нельзя отменить.\')"><button class="btn dng">Удалить звонок</button></form>')
    if r["status"] == "done":
        delbtn = (f'<a class="btn" href="/job/{jid}/report.pdf">PDF</a> <a class="btn" href="/job/{jid}/report.xlsx">Excel</a> ') + delbtn
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    meta = (f'<div class="card head"><div class="meta"><small>Дата и время</small><b>{time.strftime("%d.%m.%Y %H:%M", time.localtime(r["created"]))}</b></div>'
            f'<div class="meta"><small>Сотрудник</small><b>{esc(r["employee"] or "—")}</b></div>'
            f'<div class="meta"><small>Файл</small><b>{esc(r["filename"])}</b></div>'
            f'<div class="meta"><small>Длительность</small><b>{mmss(r["duration"]) if r["duration"] else "—"}</b></div>'
            f'<div class="meta"><small>Методика</small><b><span class="chip n">{esc(rubrics.name(r["rubric"]))}</span></b></div>')
    if r["status"] == "error":
        body = meta + f'</div><div class="card pad"><p class="bad"><b>Ошибка:</b> {esc(r["error"])}</p><form method="post" action="/job/{jid}/retry"><button class="btn p">Повторить</button></form></div>'
        return layout("feed", back + "Карточка звонка", banner + body, user, delbtn)
    if r["status"] == "stopped":
        body = (meta + f'</div><div class="card pad"><p><b>Обработка остановлена.</b></p><p class="muted">Звонок не оценён. Можно запустить заново или удалить.</p>'
                f'<form method="post" action="/job/{jid}/retry"><button class="btn p">Запустить заново</button></form></div>')
        return layout("feed", back + "Карточка звонка", banner + body, user, delbtn)
    if r["status"] != "done":
        stopped_now = jid in STOP
        stopbtn = ('<p class="muted">Останавливается…</p>' if stopped_now else
                   f'<form method="post" action="/job/{jid}/stop" onsubmit="return confirm(\'Остановить обработку этого звонка?\')"><button class="btn dng">Остановить обработку</button></form>')
        body = (meta + f'</div><div class="card pad"><p><b>{STATUS[r["status"]]}</b></p>{progress_html(r)}<p class="muted">Страница обновится сама. Распознавание ≈ 3–4 с на минуту записи, оценка — до минуты. '
                f'Остановка срабатывает за несколько секунд; если уже идёт ответ модели, дожидаемся его конца и отбрасываем.</p>{stopbtn}</div>')
        return layout("feed", back + "Карточка звонка", banner + body, user, delbtn, refresh=True)
    res = json.loads(r["result"])
    rev = json.loads(r["review"]) if r["review"] else None
    rub = job_rubric(r)
    model_total, shown_total = res.get("total"), final_total(r)
    meta += (f'<div class="tot"><b>{shown_total if shown_total is not None else "—"}</b>'
             f'<small>из 100 · {"с правкой человека" if rev else "оценка модели"}' + (f' (модель: {model_total})' if rev else "") + '</small></div></div>')
    peaks = json.loads(r["peaks"]) if r["peaks"] else None
    if peaks is None:
        try:
            peaks = asr.compute_peaks(next((DATA / "audio").glob(f"{jid}.*")))
            q("UPDATE jobs SET peaks=? WHERE id=?", json.dumps(peaks), jid)
        except Exception:
            peaks = [0.1] * 190
    if r["audio_deleted"]:
        player = '<div class="card pad muted small">Аудио удалено по сроку хранения; расшифровка и оценка сохранены.</div>'
    else:
        player = ('<div class="card player"><audio id="au" preload="metadata" src="/audio/' + jid + '"></audio>'
              '<div class="play" id="pl" onclick="tgl()"></div><div class="wave"><svg id="wf" viewBox="0 0 1000 54" preserveAspectRatio="none"></svg></div>'
              '<div class="time" id="tm">0:00 / ' + mmss(r["duration"] or 0) + '</div><span class="chip" id="spd" onclick="spd()">1×</span></div>')
    emp_name = rub.get("employee_title") or "Сотрудник"

    def msg(i, s):
        spk = s.get("spk")
        tag = (f'<span class="w {"e" if spk == "employee" else "c"}" title="Нажмите, чтобы сменить роль" onclick="flip(event,{i})">'
               f'{emp_name if spk == "employee" else "Клиент"}</span>') if spk else '<span class="w"></span>'
        return f'<div class="msg" data-i="{i}" data-t="{s["t"]}" onclick="seek({s["t"]})"><span class="t">{mmss(s["t"])}</span>{tag}<span>{esc(s["text"])}</span></div>'
    msgs = "".join(msg(i, s) for i, s in enumerate(json.loads(r["transcript"])))
    items = ""
    for it in res["items"]:
        meta_it = next((x for x in rub["items"] if x["code"] == it["code"]), {"title": it["code"], "weight": ""})
        sc = it["score"]
        ico = '<span class="muted">–</span>' if sc is None else '<span class="ok">✔</span>' if sc == 2 else '<span class="warn">⚠</span>' if sc == 1 else '<span class="bad">✖</span>'
        ev = "".join(f'<div class="q">«{esc(e["quote"])}» <a onclick="seek({to_sec(e["t"])})">▶ {esc(e["t"])}</a> <span class="muted">{"сотрудник" if e["speaker"] == "employee" else "клиент"}</span></div>' for e in it["evidence"])
        cur = rev["scores"].get(it["code"]) if rev else sc
        choose = "".join(f'<option value="{v}" {"selected" if cur == (None if v == "null" else int(v)) else ""}>{t}</option>' for v, t in (("2", "2"), ("1", "1"), ("0", "0"), ("null", "н/п")))
        changed = rev is not None and rev["scores"].get(it["code"]) != sc
        items += (f'<div class="item {"ch" if changed else ""}" data-code="{it["code"]}"><div><div class="n">{ico} {esc(meta_it["title"])} '
                  f'<span class="muted">вес {meta_it["weight"]}</span>{" <span class=chip>низкая уверенность</span>" if it["confidence"] == "low" else ""}</div>'
                  f'<div class="q">{esc(it["reason"])}</div>{ev}</div><div class="m">{"н/п" if sc is None else sc} / 2</div>'
                  f'<select class="h" onchange="mark(this)">{choose}</select></div>')
    flags = ", ".join(FLAGS.get(f, f) for f in res["flags"]) or "нет"
    summary = (f'<div class="sumline"><b>Кратко:</b> {esc(res["summary"])}<br><span class="muted">Тема: {esc(res["topic"])} · исход: {OUTCOME.get(res["outcome"], res["outcome"])} · флаги: {esc(flags)}</span></div>')
    footer = (f'<div class="card foot"><input class="cm" id="cm" placeholder="Комментарий руководителя…" value="{esc(rev["comment"]) if rev else ""}">'
              f'<button class="btn" onclick="save(true)">Согласен со всеми</button><button class="btn p" onclick="save(false)">Сохранить исправления</button>'
              f'<span id="saved" class="muted">{("Проверено: " + esc(rev["by"]) + ", " + time.strftime("%d.%m %H:%M", time.localtime(rev["at"]))) if rev else ""}</span></div>')
    body = (f'{meta}{player}<div class="cols"><div class="card tr"><h3>Расшифровка</h3>'
            f'<div class="rolebar muted small">Роли расставлены автоматически — нажмите на метку роли, чтобы исправить. '
            f'<form method="post" action="/job/{jid}/rescore" id="rsf" hidden style="display:inline"><button class="btn p">Переоценить с новыми ролями</button></form></div>{msgs}'
            f'<p class="muted small">Автоматическая расшифровка ({esc(r["asr_engine"] or "GigaAM")}) и разметка ролей — возможны ошибки. Распознавание {r["asr_sec"] or 0:.0f} с, оценка {r["llm_sec"] or 0:.0f} с.</p></div>'
            f'<div class="card sc"><h3>Оценка по пунктам — «{esc(rubrics.name(r["rubric"]))}» v{esc(rub["version"])}</h3>{summary}'
            f'<div class="colh"><span>Пункт</span><span>Модель</span><span>Человек</span></div>{items}</div></div>{footer}'
            f'<script>var JID="{jid}",EMPNAME={json.dumps(emp_name, ensure_ascii=False)},PEAKS={json.dumps(peaks)},MODEL={json.dumps({i["code"]: i["score"] for i in res["items"]})};{JOB_JS % (r["duration"] or 0)}</script>')
    return layout("feed", back + "Карточка звонка", banner + body, user, delbtn)


JOB_JS = """
var au=document.getElementById('au'),speeds=[1,1.25,1.5,2],si=0;
var wf=document.getElementById('wf'),pl=document.getElementById('pl'),tm=document.getElementById('tm'),total=%s;
function fmt(s){s=Math.floor(s);return Math.floor(s/60)+':'+('0'+s%%60).slice(-2);}
function draw(){var dur=au.duration||total||1,pos=(au.currentTime/dur)*PEAKS.length,s='';
PEAKS.forEach(function(v,i){var a=3+23*v,done=i<pos,c=i%%2?(done?'#E5007E':'#F5A3CE'):(done?'#2A2A2F':'#D6D6DB');
s+='<rect x="'+(i*1000/PEAKS.length)+'" y="'+(27-a)+'" width="3" height="'+(2*a)+'" rx="1.5" fill="'+c+'"/>';});
s+='<rect x="'+(pos*1000/PEAKS.length)+'" y="0" width="2" height="54" fill="#E9A23B"/>';wf.innerHTML=s;
tm.textContent=fmt(au.currentTime)+' / '+fmt(dur);pl.classList.toggle('pause',!au.paused);}
function tgl(){au.paused?au.play():au.pause();}
wf.addEventListener('click',function(e){var b=wf.getBoundingClientRect();au.currentTime=((e.clientX-b.left)/b.width)*(au.duration||total);draw();});
function seek(t){au.currentTime=t;au.play();}
function flip(ev,i){ev.stopPropagation();var w=ev.currentTarget,emp=w.classList.contains('e'),spk=emp?'client':'employee';
fetch('/job/'+JID+'/speaker',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idx:i,spk:spk})}).then(function(r){return r.json()}).then(function(d){
if(!d.ok)return;w.classList.toggle('e',!emp);w.classList.toggle('c',emp);w.textContent=emp?'Клиент':EMPNAME;document.getElementById('rsf').hidden=false;});}
function spd(){si=(si+1)%%speeds.length;au.playbackRate=speeds[si];document.getElementById('spd').textContent=speeds[si]+'×';}
['play','pause','seeked','loadedmetadata'].forEach(function(ev){au.addEventListener(ev,draw)});draw();
au.addEventListener('timeupdate',function(){draw();var c=null;document.querySelectorAll('.msg').forEach(function(m){m.classList.remove('hl');if(+m.dataset.t<=au.currentTime)c=m});if(c&&!au.paused)c.classList.add('hl');});
function mark(sel){var it=sel.closest('.item'),v=sel.value==='null'?null:+sel.value;it.classList.toggle('ch',v!==MODEL[it.dataset.code]);}
function save(agree){var s={};document.querySelectorAll('.item').forEach(function(it){var sel=it.querySelector('select');if(agree)sel.value=MODEL[it.dataset.code]===null?'null':String(MODEL[it.dataset.code]);s[it.dataset.code]=sel.value==='null'?null:+sel.value;});
fetch('/job/'+JID+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scores:s,comment:document.getElementById('cm').value})})
.then(function(r){return r.json()}).then(function(d){if(d.ok)location.reload();else alert('Не сохранилось');});}
(function(){var m=location.hash.match(/t=([0-9]+)/);if(!m)return;var t=+m[1];setTimeout(function(){var best=null;document.querySelectorAll('.msg').forEach(function(e){if(+e.dataset.t<=t)best=e;});
if(best){best.scrollIntoView({block:'center'});best.classList.add('hl');}},50);})();
"""


# ---------- сводка ----------
@app.get("/summary", response_class=HTMLResponse)
def summary(user=Depends(current_user)):
    done = jobs_of(user, "status='done'", "created")
    if not done:
        return layout("sum", "Сводка", '<div class="card pad muted">Пока нет оценённых звонков.</div>', user)
    tot = [(r, final_total(r)) for r in done]
    vals = [t for _, t in tot if t is not None]
    avg = round(sum(vals) / len(vals), 1) if vals else 0
    low = round(100 * sum(1 for v in vals if v < 60) / len(vals), 1) if vals else 0
    revd = sum(1 for r in done if r["review"])
    agree = n = 0
    for r in done:
        if r["review"]:
            res = json.loads(r["result"])
            for i in res["items"]:
                n += 1
                agree += i["score"] == json.loads(r["review"])["scores"].get(i["code"])
    kpis = (f'<div class="card kpi"><small>Оценено звонков</small><b>{len(done)}</b><span>всего загружено: {len(jobs_of(user))}</span></div>'
            f'<div class="card kpi"><small>Средний балл</small><b>{avg}</b><span>с учётом правок людей</span></div>'
            f'<div class="card kpi"><small>Низкий балл (&lt; 60)</small><b>{low} %</b><span>доля звонков</span></div>'
            f'<div class="card kpi"><small>Проверено руководителями</small><b>{round(100 * revd / len(done))} %</b><span>{revd} из {len(done)}</span></div>'
            f'<div class="card kpi"><small>Совпадение модели и людей</small><b>{(str(round(100 * agree / n)) + " %") if n else "—"}</b><span>{"по " + str(n) + " проверенным пунктам" if n else "нет проверенных"}</span></div>')
    days: dict = {}
    for r, t in tot:
        if t is not None:
            days.setdefault(time.strftime("%d.%m", time.localtime(r["created"])), []).append(t)
    pts = [(d, sum(v) / len(v)) for d, v in days.items()][-14:]
    chart_title = "Средний балл по дням"
    if len(pts) < 3:  # мало дней — показываем последние звонки по порядку
        pts = [(time.strftime("%H:%M", time.localtime(r["created"])), t) for r, t in tot if t is not None][-14:]
        chart_title = "Балл по последним звонкам"
    W, H, L, B, T = 700, 210, 34, 24, 10

    def X(i): return L + (i * (W - L - 10) / max(len(pts) - 1, 1) if len(pts) > 1 else (W - L) / 2)
    def Y(v): return T + (1 - v / 100) * (H - T - B)
    svg = "".join(f'<line x1="{L}" x2="{W - 10}" y1="{Y(g)}" y2="{Y(g)}" stroke="#EEF1F5"/><text x="{L - 6}" y="{Y(g) + 4}" font-size="11" fill="#5E5E66" text-anchor="end">{g}</text>' for g in (0, 25, 50, 75, 100))
    svg += "".join(f'<text x="{X(i)}" y="{H - 6}" font-size="11" fill="#5E5E66" text-anchor="middle">{d}</text>' for i, (d, _) in enumerate(pts))
    if len(pts) > 1:
        svg += '<path d="' + " ".join(f'{"ML"[i > 0]}{X(i)} {Y(v)}' for i, (_, v) in enumerate(pts)) + '" fill="none" stroke="#E5007E" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
    svg += "".join(f'<circle cx="{X(i)}" cy="{Y(v)}" r="3.5" fill="#fff" stroke="#E5007E" stroke-width="2"/><text x="{X(i)}" y="{Y(v) - 9}" font-size="11" font-weight="700" fill="#B5005F" text-anchor="middle">{round(v)}</text>' for i, (_, v) in enumerate(pts))
    agg: dict = {}
    for r in done:
        rev = json.loads(r["review"])["scores"] if r["review"] else None
        for i in json.loads(r["result"])["items"]:
            v = rev.get(i["code"]) if rev else i["score"]
            if v is not None:
                title = next((x["title"] for x in job_rubric(r)["items"] if x["code"] == i["code"]), i["code"])
                agg.setdefault((rubrics.name(r["rubric"]), title), []).append(v / 2 * 100)
    weak = sorted(((k, sum(v) / len(v)) for k, v in agg.items()), key=lambda x: x[1])[:7]
    bars = "".join(f'<div class="hb"><span>{esc(t)} <small class="muted">{esc(rb)}</small></span><div class="trk"><i style="width:{v:.0f}%;background:{"#D9534F" if v < 50 else "#E9A23B" if v < 75 else "#E5007E"}"></i></div><b>{v:.0f}</b></div>' for (rb, t), v in weak)
    by: dict = {}
    for r, t in tot:
        if t is not None:
            by.setdefault(r["employee"] or "— не указан —", []).append((r, t))
    erows = ""
    for e, lst in sorted(by.items(), key=lambda x: -sum(t for _, t in x[1]) / len(x[1])):
        a = sum(t for _, t in lst) / len(lst)
        w: dict = {}
        for r, _ in lst:
            for i in json.loads(r["result"])["items"]:
                if i["score"] is not None:
                    w.setdefault(i["code"], []).append(i["score"])
        worst = min(w.items(), key=lambda kv: sum(kv[1]) / len(kv[1])) if w else None
        wt = next((x["title"] for r, _ in lst[:1] for x in job_rubric(r)["items"] if worst and x["code"] == worst[0]), "—")
        erows += f'<tr><td>{esc(e)}</td><td>{len(lst)}</td><td><span class="chip {"g" if a >= 80 else "a" if a >= 60 else "r"}">{a:.0f}</span></td><td>{esc(wt)}</td><td>{sum(1 for r, _ in lst if is_disputed(r))}</td></tr>'
    body = (f'<div class="kpis">{kpis}</div><div class="row"><div class="card" style="flex:1.35"><h3>{chart_title}</h3><div class="chart"><svg viewBox="0 0 {W} {H}" width="100%" height="210">{svg}</svg></div></div>'
            f'<div class="card" style="flex:1"><h3>Слабые пункты чек-листа (средний % от максимума)</h3>{bars}</div></div>'
            f'<div class="card"><h3>Сотрудники</h3><table class="list plain"><tr><th>Сотрудник</th><th>Звонков</th><th>Средний балл</th><th>Слабый пункт</th><th>Спорных</th></tr>{erows}</table></div>')
    return layout("sum", "Сводка", body, user, '<span class="demo">пилот · тестовые данные</span>')


# ---------- справочник сотрудников ----------
def _emp(eid: int, user):
    e = q("SELECT * FROM employees WHERE id=?", eid).fetchone()
    if not e or (user["role"] != "admin" and e["owner"] != user["id"]):
        raise HTTPException(404)
    return e


def _emp_fields(name: str, job_title: str, phone: str):
    name, job_title, phone = " ".join(name.split())[:80], job_title.strip()[:80], phone.strip()[:40]
    return name, job_title, phone


@app.get("/employees", response_class=HTMLResponse)
def employees(msg: str = "", user=Depends(current_user)):
    admin_view = user["role"] == "admin"
    where, args = ("", ()) if admin_view else ("WHERE e.owner=?", (user["id"],))
    emps = q(f"SELECT e.*, u.org, u.username FROM employees e LEFT JOIN users u ON u.id=e.owner {where} ORDER BY lower(e.name)", *args).fetchall()
    stats, used = {}, {}
    for r in jobs_of(user):
        if not r["employee"]:
            continue
        used[(r["owner"], r["employee"])] = used.get((r["owner"], r["employee"]), 0) + 1
        t = final_total(r) if r["status"] == "done" else None
        if t is not None:
            stats.setdefault((r["owner"], r["employee"]), []).append(t)
    known = {(e["owner"], e["name"]) for e in emps}
    owners = _owners(user)
    rows = ""
    for e in emps:
        s = stats.get((e["owner"], e["name"]), [])
        f = f"f{e['id']}"
        own = f'<td>{esc(owners.get(e["owner"], "—"))}</td>' if admin_view else ""
        avg = f'<span class="chip {"g" if sum(s) / len(s) >= 80 else "a" if sum(s) / len(s) >= 60 else "r"}">{sum(s) / len(s):.0f}</span>' if s else "—"
        rows += (f'<tr><td><form id="{f}" method="post" action="/employees/{e["id"]}"></form><input class="inp" form="{f}" name="name" value="{esc(e["name"])}" required maxlength="80"></td>{own}'
                 f'<td><input class="inp" form="{f}" name="job_title" value="{esc(e["job_title"] or "")}" maxlength="80" placeholder="должность"></td>'
                 f'<td><input class="inp sm" form="{f}" name="phone" value="{esc(e["phone"] or "")}" maxlength="40" placeholder="внутр. номер"></td>'
                 f'<td>{used.get((e["owner"], e["name"]), 0)}</td><td>{avg}</td>'
                 f'<td style="white-space:nowrap"><button class="btn sm" form="{f}">Сохранить</button> '
                 f'<button class="btn sm dng" form="{f}" formaction="/employees/{e["id"]}/delete" formnovalidate onclick="return confirm(\'Убрать из справочника? Звонки останутся.\')">Убрать</button></td></tr>')
    extra = sorted(k for k in used if k not in known)
    extra_html = ""
    if extra:
        names = ", ".join(esc(n) for _, n in extra[:40]) + (" …" if len(extra) > 40 else "")
        extra_html = (f'<div class="card pad" style="margin-bottom:14px"><p><b>В звонках есть {len(extra)} имён, которых нет в справочнике:</b> {names}</p>'
                      f'<form method="post" action="/employees/import"><button class="btn">Добавить их в справочник</button></form></div>')
    own_h = "<th>Клиент</th>" if admin_view else ""
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    add = ('<form class="card upload" method="post" action="/employees"><b>Новый сотрудник</b><input class="inp" name="name" placeholder="Фамилия Имя" required maxlength="80">'
           '<input class="inp" name="job_title" placeholder="Должность (необязательно)" maxlength="80"><input class="inp" name="phone" placeholder="Внутренний номер (необязательно)" maxlength="40">'
           '<button class="btn p">Добавить</button></form>')
    table = (f'<div class="card" style="overflow:auto"><table class="list plain"><tr><th>Сотрудник</th>{own_h}<th>Должность</th><th>Номер</th><th>Звонков</th><th>Средний балл</th><th></th></tr>'
             + (rows or f'<tr><td colspan="{7 if admin_view else 6}" class="muted" style="padding:24px;text-align:center">Справочник пуст. Добавьте сотрудников, и они будут подсказываться при загрузке записей.</td></tr>')
             + '</table><div class="pager">При переименовании сотрудника новое имя проставляется и в его прошлых звонках. Если убрать сотрудника из справочника, звонки с его именем остаются.</div></div>')
    return layout("staff", "Сотрудники", f"{banner}{add}{extra_html}{table}", user)


@app.post("/employees")
def employee_add(name: str = Form(...), job_title: str = Form(""), phone: str = Form(""), user=Depends(current_user)):
    name, job_title, phone = _emp_fields(name, job_title, phone)
    if not name:
        return redirect("/employees", msg="Укажите имя сотрудника")
    if q("SELECT 1 FROM employees WHERE owner=? AND lower(name)=lower(?)", user["id"], name).fetchone():
        return redirect("/employees", msg=f"«{name}» уже есть в справочнике")
    q("INSERT INTO employees(owner,name,job_title,phone,created) VALUES(?,?,?,?,?)", user["id"], name, job_title, phone, time.time())
    return redirect("/employees", msg=f"Сотрудник «{name}» добавлен")


@app.post("/employees/import")
def employee_import(user=Depends(current_user)):
    n = 0
    for r in jobs_of(user, "employee IS NOT NULL AND employee<>''"):
        name = " ".join(r["employee"].split())[:80]
        if name and not q("SELECT 1 FROM employees WHERE owner=? AND lower(name)=lower(?)", r["owner"], name).fetchone():
            q("INSERT INTO employees(owner,name,created) VALUES(?,?,?)", r["owner"], name, time.time())
            n += 1
    return redirect("/employees", msg=f"Добавлено в справочник: {n}")


@app.post("/employees/{eid}")
def employee_save(eid: int, name: str = Form(...), job_title: str = Form(""), phone: str = Form(""), user=Depends(current_user)):
    e = _emp(eid, user)
    name, job_title, phone = _emp_fields(name, job_title, phone)
    if not name:
        return redirect("/employees", msg="Имя не может быть пустым")
    if name != e["name"] and q("SELECT 1 FROM employees WHERE owner=? AND lower(name)=lower(?) AND id<>?", e["owner"], name, eid).fetchone():
        return redirect("/employees", msg=f"«{name}» уже есть в справочнике")
    q("UPDATE employees SET name=?, job_title=?, phone=? WHERE id=?", name, job_title, phone, eid)
    if name != e["name"]:
        q("UPDATE jobs SET employee=? WHERE owner=? AND employee=?", name, e["owner"], e["name"])
        if user["role"] == "admin" and e["owner"] != user["id"]:
            audit(user, "переименование сотрудника клиента", f'{e["name"]} → {name}', f'клиент id {e["owner"]}')
    return redirect("/employees", msg="Сохранено")


@app.post("/employees/{eid}/delete")
def employee_delete(eid: int, user=Depends(current_user)):
    e = _emp(eid, user)
    q("DELETE FROM employees WHERE id=?", eid)
    return redirect("/employees", msg=f"«{e['name']}» убран из справочника")


# ---------- качество модели ----------
def _pct(v, nd=0) -> str:
    return "—" if v is None else f"{v:.{nd}f} %"


def _num(v, nd=2, sign=False) -> str:
    return "—" if v is None else (f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}")


@app.get("/quality", response_class=HTMLResponse)
def quality_page(user=Depends(current_user)):
    rows = jobs_of(user, "status='done'", "created")
    qs = insight.quality(rows, job_rubric, human_total, rubrics.name)
    it = qs["items"]
    if not qs["done"]:
        return layout("quality", "Качество модели", '<div class="card pad muted">Пока нет оценённых звонков.</div>', user)
    few = ('<div class="banner">Проверенных пунктов мало (меньше 30): цифры ниже ориентировочные, на них нельзя делать выводы о модели. '
           'Проверяйте звонки в карточке («Согласен со всеми» или правка оценок) — каждая проверка улучшает эту статистику.</div>') if it["n"] < 30 else ""
    bias_note = "модель строже человека" if (it["bias"] or 0) < -0.05 else "модель мягче человека" if (it["bias"] or 0) > 0.05 else "без заметного смещения"
    kpis = (f'<div class="card kpi"><small>Проверено людьми</small><b>{qs["reviewed"]} из {qs["done"]}</b><span>{it["n"]} пунктов</span></div>'
            f'<div class="card kpi"><small>Совпадение по пунктам</small><b>{_pct(it["agree"])}</b><span>модель = человек</span></div>'
            f'<div class="card kpi"><small>Средняя ошибка пункта</small><b>{_num(it["mae"])}</b><span>из 2 баллов</span></div>'
            f'<div class="card kpi"><small>Смещение</small><b>{_num(it["bias"], 2, True)}</b><span>{bias_note}</span></div>'
            f'<div class="card kpi"><small>Ошибка итогового балла</small><b>{_num(qs["total_mae"], 1)}</b><span>из 100 · корреляция {_num(qs["total_corr"])}' + (f' · звонков {qs["total_n"]}' if qs["total_n"] else '') + '</span></div>'
            f'<div class="card kpi"><small>Пункты с низкой уверенностью</small><b>{_pct(qs["low_conf_share"])}</b><span>модель сама сомневалась</span></div>'
            f'<div class="card kpi"><small>Правки ролей в расшифровке</small><b>{_pct(qs["roles_edit_share"], 1)}</b><span>{qs["roles_edits"]} правок на {qs["roles_segs"]} реплик</span></div>')
    lab = {0: "0", 1: "1", 2: "2", None: "н/п"}
    mx = qs["matrix"]
    mrows = ""
    for m in (2, 1, 0, None):
        cells = "".join(f'<td class="mx {"hit" if m == h else ""}">{mx[(m, h)] or "·"}</td>' for h in (2, 1, 0, None))
        mrows += f'<tr><th>модель: {lab[m]}</th>{cells}</tr>'
    matrix = (f'<div class="card" style="flex:1"><h3>Где модель и человек расходятся</h3><table class="plain mxt"><tr><th></th>'
              + "".join(f'<th>человек: {lab[h]}</th>' for h in (2, 1, 0, None)) + f'</tr>{mrows}</table>'
              f'<div class="pager">Строка — оценка модели, столбец — оценка человека. По диагонали — совпадения. н/п — пункт не применим.</div></div>')
    names = {"high": "высокая", "medium": "средняя", "low": "низкая"}
    crow = "".join(f'<tr><td>{names.get(k, k)}</td><td>{v["n"]}</td><td>{_pct(v["agree"])}</td><td>{_num(v["bias"], 2, True)}</td></tr>'
                   for k, v in sorted(qs["conf"].items(), key=lambda kv: ["high", "medium", "low"].index(kv[0]) if kv[0] in ("high", "medium", "low") else 9))
    conf = (f'<div class="card" style="flex:1"><h3>Уверенность модели и точность</h3><table class="list plain"><tr><th>Уверенность</th><th>Пунктов</th><th>Совпало</th><th>Смещение</th></tr>'
            f'{crow or "<tr><td colspan=4 class=muted>нет данных</td></tr>"}</table><div class="pager">Если при «высокой» уверенности точность не выше, чем при «низкой», на уверенность модели полагаться нельзя.</div></div>')
    crit = "".join(f'<div class="hb"><span>{esc(c["title"])} <small class="muted">{esc(c["rubric"])} · {c["n"]}</small></span>'
                   f'<div class="trk"><i style="width:{c["agree"]:.0f}%;background:{"#D9534F" if c["agree"] < 60 else "#E9A23B" if c["agree"] < 80 else "#E5007E"}"></i></div><b>{c["agree"]:.0f}</b></div>'
                   for c in qs["crit"][:12])
    crit_card = f'<div class="card" style="flex:1.3"><h3>Совпадение по пунктам чек-листа (худшие сверху, %)</h3>{crit or "<p class=muted style=padding:0_18px>нет данных</p>"}</div>'
    mrows2 = "".join(f'<tr><td>{esc(m["llm"])}</td><td>{esc(m["asr"])}</td><td>{m["calls"]}</td><td>{_pct(m["agree"])}</td><td>{_num(m["mae"])}</td>'
                     f'<td>{m["asr_sec"]:.0f} с / {m["llm_sec"]:.0f} с</td><td>{m["tok"]:.0f}</td></tr>' for m in qs["models"])
    models = (f'<div class="card" style="margin-top:14px;overflow:auto"><h3>По моделям (только проверенные звонки)</h3><table class="list plain"><tr><th>Модель оценки</th><th>Распознавание</th><th>Звонков</th>'
              f'<th>Совпало</th><th>Ошибка пункта</th><th>Время: распознавание / оценка</th><th>Токенов</th></tr>{mrows2 or "<tr><td colspan=7 class=muted>нет проверенных звонков</td></tr>"}</table>'
              f'<div class="pager">Так видно, стало ли лучше после смены модели: сравнивайте строки между собой. Голоса, помеченные «голоса», разделены по акустике.</div></div>')
    body = f'{few}<div class="kpis">{kpis}</div><div class="row">{matrix}{conf}</div><div class="row">{crit_card}</div>{models}'
    return layout("quality", "Качество модели", body, user)


# ---------- выгрузка в Excel и PDF ----------
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def call_row(r, owners: dict) -> dict:
    """Плоское описание звонка для выгрузки (без HTML)."""
    res = json.loads(r["result"]) if r["result"] else None
    rev = json.loads(r["review"]) if r["review"] else None
    rub = job_rubric(r)
    titles = {x["code"]: x for x in rub["items"]}
    items = []
    for it in (res["items"] if res else []):
        meta_it = titles.get(it["code"], {"title": it["code"], "weight": ""})
        items.append({"title": meta_it["title"], "weight": meta_it["weight"], "model": it["score"],
                      "human": rev["scores"].get(it["code"]) if rev else "", "reason": it["reason"],
                      "evidence": [f'{e["t"]} {"сотрудник" if e["speaker"] == "employee" else "клиент"}: «{e["quote"]}»' for e in it["evidence"]]})
    return {"id": r["id"], "date": time.strftime("%d.%m.%Y %H:%M", time.localtime(r["created"])), "org": owners.get(r["owner"], "—"),
            "employee": r["employee"] or "", "filename": r["filename"], "rubric": rubrics.name(r["rubric"]),
            "duration": mmss(r["duration"]) if r["duration"] else "", "total": final_total(r),
            "model_total": res.get("total") if res else None,
            "status": "Проверен" if rev else STATUS[r["status"]],
            "flags": ", ".join(FLAGS.get(f, f) for f in res["flags"]) if res else "", "topic": res["topic"] if res else "",
            "outcome": OUTCOME.get(res["outcome"], res["outcome"]) if res else "", "summary": res["summary"] if res else "",
            "comment": rev["comment"] if rev else "", "items": items}


def job_export(r, owners: dict) -> dict:
    c = call_row(r, owners)
    rub = job_rubric(r)
    emp = rub.get("employee_title") or "Сотрудник"
    head = [("Дата и время", c["date"]), ("Сотрудник", c["employee"] or "—"), ("Файл", c["filename"]), ("Длительность", c["duration"] or "—"),
            ("Чек-лист", f'{c["rubric"]} v{rub["version"]}'),
            ("Итоговый балл", f'{c["total"]} из 100' + (f' (оценка модели: {c["model_total"]})' if c["status"] == "Проверен" else " (оценка модели)")),
            ("Тема", c["topic"]), ("Исход", c["outcome"]), ("Флаги", c["flags"] or "нет"), ("Кратко", c["summary"])]
    if c["comment"]:
        head.append(("Комментарий проверяющего", c["comment"]))
    tr = [(mmss(s["t"]), emp if s.get("spk") == "employee" else "Клиент" if s.get("spk") == "client" else "", s["text"]) for s in json.loads(r["transcript"] or "[]")]
    return {"title": f'Звонок: {c["employee"] or c["filename"]} · {c["date"]}', "head": head, "items": c["items"], "transcript": tr}


def _owners(user) -> dict:
    return {u["id"]: (u["org"] or u["username"]) for u in q("SELECT * FROM users").fetchall()} if user["role"] == "admin" else {}





def _file(data: bytes, name: str, mime: str) -> Response:
    return Response(data, media_type=mime, headers={"Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"})


def _export_rows(user, emp, rubric, st, sc):
    rows = [r for r in filter_jobs(jobs_of(user), emp, rubric, st, sc)]
    owners = _owners(user)
    return [call_row(r, owners) for r in rows]


@app.get("/export/calls.xlsx")
def export_calls_xlsx(emp: str = "", rubric: str = "", st: str = "", sc: str = "", user=Depends(current_user)):
    return _file(exports.xlsx_calls(_export_rows(user, emp, rubric, st, sc), user["role"] == "admin"), f"calls-{time.strftime('%Y%m%d')}.xlsx", XLSX)


@app.get("/export/calls.pdf")
def export_calls_pdf(emp: str = "", rubric: str = "", st: str = "", sc: str = "", user=Depends(current_user)):
    return _file(exports.pdf_calls(_export_rows(user, emp, rubric, st, sc), user["role"] == "admin", "Список звонков · Call QA"),
                 f"calls-{time.strftime('%Y%m%d')}.pdf", "application/pdf")


@app.get("/job/{jid}/report.xlsx")
def report_xlsx(jid: str, user=Depends(current_user)):
    r = load(jid, user)
    if r["status"] != "done":
        raise HTTPException(409, "звонок ещё не оценён")
    return _file(exports.xlsx_job(job_export(r, _owners(user))), f"call-{jid}.xlsx", XLSX)


@app.get("/job/{jid}/report.pdf")
def report_pdf(jid: str, user=Depends(current_user)):
    r = load(jid, user)
    if r["status"] != "done":
        raise HTTPException(409, "звонок ещё не оценён")
    return _file(exports.pdf_job(job_export(r, _owners(user))), f"call-{jid}.pdf", "application/pdf")


# ---------- API для клиентов (включается администратором) ----------
def _api_page(user, request: Request, new_key: str = ""):
    base = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    u = q("SELECT * FROM users WHERE id=?", user["id"]).fetchone()
    key_box = (f'<div class="banner">Ваш новый ключ (показывается один раз, сохраните): <code class="key">{esc(new_key)}</code></div>' if new_key else
               (f'<p>Текущий ключ: <code>{esc(u["api_key_prefix"])}…</code> (полностью не хранится)</p>' if u["api_key_prefix"] else '<p class="muted">Ключ ещё не создан.</p>'))
    body = (f'<div class="card pad" style="max-width:860px">{key_box}<form method="post" action="/api-access/key"><button class="btn p">{"Выпустить новый ключ (старый перестанет работать)" if u["api_key_prefix"] else "Создать ключ"}</button></form>'
            f'<h3 style="margin-top:22px">Загрузка записи</h3><pre>curl -X POST {base}/api/v1/calls \\\n  -H "Authorization: Bearer ВАШ_КЛЮЧ" \\\n  -F audio=@call.mp3 -F rubric=sales -F employee="Иванов Алексей" -F lang=ru</pre>'
            f'<p class="muted small">Ответ: <code>{{"ids": ["…"], "skipped": [], "rejected_by_limit": []}}</code>. Можно передать несколько файлов (повторить <code>-F audio=@…</code>). '
            f'Чек-листы: ключи из <code>GET /api/v1/checklists</code> (шаблоны <code>sales</code>, <code>support</code> и ваши собственные <code>r…</code>). '
            f'Язык <code>lang</code> — <code>ru</code>, <code>kk</code> или <code>auto</code> (необязательно). Сотрудники из справочника — <code>GET /api/v1/employees</code>. '
            f'Если дневной лимит или лимит очереди исчерпан, ответ <code>429</code>.</p>'
            f'<h3 style="margin-top:18px">Результат</h3><pre>curl {base}/api/v1/calls/ID -H "Authorization: Bearer ВАШ_КЛЮЧ"</pre>'
            f'<p class="muted small">Статус <code>status</code>: queued, asr, scoring, done, error, stopped; <code>stage</code> и <code>progress</code> (0–100) показывают этап и ход обработки. При <code>done</code> в ответе расшифровка с ролями и оценка по пунктам (<code>result.total</code> — балл 0–100).</p>'
            f'<h3 style="margin-top:18px">Список звонков</h3><pre>curl "{base}/api/v1/calls?limit=50" -H "Authorization: Bearer ВАШ_КЛЮЧ"</pre></div>')
    return layout("api", "API", body, user)


@app.get("/api-access", response_class=HTMLResponse)
def api_access(request: Request, user=Depends(current_user)):
    if not user["api_enabled"] and user["role"] != "admin":
        raise HTTPException(403, "API для вашей учётной записи не включён")
    return _api_page(user, request)


@app.post("/api-access/key", response_class=HTMLResponse)
def api_key(request: Request, user=Depends(current_user)):
    if not user["api_enabled"] and user["role"] != "admin":
        raise HTTPException(403, "API для вашей учётной записи не включён")
    return _api_page(user, request, new_api_key(user["id"]))


def _job_json(r, full: bool = True) -> dict:
    d = {"id": r["id"], "status": r["status"], "stage": r["stage"], "progress": r["progress"], "filename": r["filename"], "employee": r["employee"],
         "rubric": r["rubric"], "lang": r["lang"], "created": r["created"], "duration": r["duration"], "error": r["error"], "total": final_total(r)}
    if full:
        d.update(transcript=json.loads(r["transcript"] or "[]"), result=json.loads(r["result"] or "null"), review=json.loads(r["review"] or "null"))
    return d


@app.post("/api/v1/calls")
def api_upload(audio: list[UploadFile] = File(...), rubric: str = Form(...), employee: str = Form(""), lang: str = Form(""), user=Depends(api_user)):
    if not rubrics.can_use(rubric, user):
        raise HTTPException(400, f"неизвестный чек-лист, доступны: {', '.join(k for k, _ in rubrics.available(user))}")
    if lang and lang not in LANGS:
        raise HTTPException(400, f"неизвестный язык, доступны: {', '.join(LANGS)}")
    ids, skipped, over, why = save_uploads(audio, rubric, employee, user["id"], lang)
    if over and not ids:
        raise HTTPException(429, f"лимит: {why}")  # ни одна запись не принята
    return {"ids": ids, "skipped": skipped, "rejected_by_limit": over}


@app.get("/api/v1/employees")
def api_employees(user=Depends(api_user)):
    return [{"name": e["name"], "job_title": e["job_title"], "phone": e["phone"]} for e in q("SELECT * FROM employees WHERE owner=? ORDER BY name", user["id"]).fetchall()]


@app.get("/api/v1/checklists")
def api_checklists(user=Depends(api_user)):
    return [{"key": k, "name": n} for k, n in rubrics.available(user)]


@app.get("/api/v1/calls/{jid}")
def api_get(jid: str, user=Depends(api_user)):
    r = q("SELECT * FROM jobs WHERE id=? AND owner=?", jid, user["id"]).fetchone()
    if not r:
        raise HTTPException(404)
    return _job_json(r)


@app.get("/api/v1/calls")
def api_list(limit: int = 50, user=Depends(api_user)):
    rows = q("SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT ?", user["id"], max(1, min(limit, 200))).fetchall()
    return [_job_json(r, False) for r in rows]
