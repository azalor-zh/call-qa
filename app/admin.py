"""Панель администратора: клиенты, модели, состояние сервера."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
import yaml
from fastapi import APIRouter, Depends, Form, HTTPException

import asr
import hooks
import scoring
from core import DATA, admin_user, audit, check_pw, day_start, esc, hash_pw, jobs, layout, mmss, new_api_key, q, redirect, set_setting, setting, upload_allowance

PRIORITY = {1: "высокий", 0: "обычный", -1: "низкий"}

router = APIRouter(prefix="/admin")


def num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_dt(ts) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"


# ---------- клиенты ----------
@router.get("/clients")
def clients(msg: str = "", user=Depends(admin_user)):
    stats = {r["owner"]: r for r in q("""SELECT owner, count(*) n, coalesce(sum(duration),0) dur, avg(duration) avg_dur, avg(asr_sec) asr, avg(llm_sec) llm,
        coalesce(sum(tok_in),0) ti, coalesce(sum(tok_out),0) tout, coalesce(sum(asr_sec),0) asr_sum, coalesce(sum(llm_sec),0) llm_sum, max(created) AS last
        FROM jobs GROUP BY owner""").fetchall()}
    rows = ""
    for u in q("SELECT * FROM users ORDER BY role, id").fetchall():
        s = stats.get(u["id"])
        n = s["n"] if s else 0
        if s:  # обработанные звонки с замерами; без них — «нет записей»
            tok = s["ti"] + s["tout"]
            cells = (f'<td>{n}</td><td>{mmss(s["dur"])} · ср. {mmss(s["avg_dur"] or 0)}</td>'
                     f'<td>{(s["asr"] or 0):.0f} с / {(s["llm"] or 0):.0f} с</td>'
                     f'<td>{num(tok // max(n, 1))} <span class="muted">(всего {num(tok)})</span></td>'
                     f'<td>{(s["asr_sum"] + s["llm_sum"]) / 60:.1f} мин</td><td>{fmt_dt(s["last"])}</td>')
        else:
            cells = '<td>0</td><td colspan="5" class="muted">нет записей</td>'
        pw_form = (f'<form method="post" action="/admin/clients/{u["id"]}/password" style="display:inline"><input class="inp sm" name="password" placeholder="новый пароль" minlength="8" required>'
                   f'<button class="btn sm">Сменить пароль</button></form>')
        if u["role"] == "admin":
            api = '<span class="muted">—</span>'
            if u["id"] == user["id"]:
                ctl = '<span class="muted">это вы (пароль — в «Профиле»)</span>'
            else:
                ctl = (f'<form method="post" action="/admin/clients/{u["id"]}/active" style="display:inline"><button class="btn sm">{"Отключить" if u["active"] else "Включить"}</button></form> '
                       f'{pw_form}')
        else:
            api = (f'<form method="post" action="/admin/clients/{u["id"]}/api"><label class="chk"><input type="checkbox" name="on" value="1" '
                   f'{"checked" if u["api_enabled"] else ""} onchange="this.form.submit()"> API</label></form>')
            ctl = (f'<form method="post" action="/admin/clients/{u["id"]}/active" style="display:inline"><button class="btn sm">{"Отключить" if u["active"] else "Включить"}</button></form> '
                   f'<form method="post" action="/admin/clients/{u["id"]}/password" style="display:inline"><input class="inp sm" name="password" placeholder="новый пароль" minlength="8" required>'
                   f'<button class="btn sm">Сменить пароль</button></form>')
        status = '<span class="chip g">активен</span>' if u["active"] else '<span class="chip r">отключён</span>'
        badge = ' <span class="chip a">админ</span>' if u["role"] == "admin" else ""
        marks = (f' <span class="chip n" title="Приоритет в очереди">приоритет: {PRIORITY.get(u["priority"] or 0, "обычный")}</span>' if u["priority"] else "") + \
                (' <span class="chip n" title="Веб-хук включён">веб-хук</span>' if u["webhook_enabled"] and u["webhook_url"] else "") + \
                (' <span class="chip n" title="Есть лимиты">лимиты</span>' if u["daily_limit"] or u["queue_limit"] else "")
        rows += (f'<tr><td><a href="/admin/clients/{u["id"]}"><b>{esc(u["org"] or "—")}</b></a>{badge}{marks}<div class="muted small" style="margin:0">{esc(u["username"])}</div></td><td>{status}</td><td>{api}</td>{cells}<td style="white-space:normal">{ctl}</td></tr>')
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    body = (f'{banner}<p class="muted" style="margin-bottom:12px">Нового клиента или администратора добавляют на вкладке <a href="/admin/new">«Новая учётка»</a>.</p><div class="card" style="overflow:auto"><table class="list plain"><tr><th>Клиент</th><th>Статус</th><th>API</th><th>Записей</th>'
            f'<th>Аудио (всего · средняя)</th><th>Среднее на звонок: распознавание / оценка</th><th>Токенов LLM на звонок</th><th>Время обработки всего</th><th>Последняя запись</th><th></th></tr>{rows}</table>'
            f'<div class="pager">Ресурсы — это секунды работы процессора (распознавание) и видеокарт (оценка моделью) на обработку записей клиента.</div></div>')
    return layout("clients", "Клиенты", body, user)


@router.get("/new")
def new_account(msg: str = "", user=Depends(admin_user)):
    """Отдельная вкладка: создание клиента и администратора."""
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    login = ('<input class="inp" name="username" placeholder="Логин" required pattern="[A-Za-z0-9_.-]{3,32}" title="3–32 символа: латиница, цифры, _ . -">'
             '<input class="inp" name="password" type="password" autocomplete="new-password" placeholder="Пароль (от 8 символов)" minlength="8" required>')
    body = (f'{banner}<div class="row">'
            f'<form class="card pad newacc" method="post" action="/admin/clients/create"><h3 style="padding:0">Новый клиент</h3>'
            f'<p class="muted small" style="margin:0 0 4px">Клиент видит только свои звонки и чек-листы. Список клиентов — на вкладке «Клиенты».</p>'
            f'<input class="inp" name="org" placeholder="Название компании" required>{login}'
            f'<label class="chk"><input type="checkbox" name="api" value="1"> включить API</label><button class="btn p">Создать клиента</button></form>'
            f'<form class="card pad newacc" method="post" action="/admin/admins/create"><h3 style="padding:0">Новый администратор</h3>'
            f'<p class="muted small" style="margin:0 0 4px">Администратор видит всех клиентов, модели и состояние сервера. Выдавайте осторожно.</p>'
            f'<input class="inp" name="org" placeholder="Имя (как показывать)" required>{login}'
            f'<button class="btn p">Создать администратора</button></form></div>')
    return layout("new", "Новая учётка", body, user)


@router.post("/clients/create")
def create_client(org: str = Form(...), username: str = Form(...), password: str = Form(...), api: str = Form(""), user=Depends(admin_user)):
    username = username.strip()
    if len(password) < 8 or not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username) or q("SELECT 1 FROM users WHERE username=?", username).fetchone():
        return redirect("/admin/new", msg="Не создано: логин занят или неверен (3–32 символа: латиница, цифры, _ . -), либо пароль короче 8 символов")
    q("INSERT INTO users(username,org,role,pw_hash,api_enabled,created) VALUES(?,?,?,?,?,?)", username, org.strip(), "client", hash_pw(password), 1 if api else 0, time.time())
    audit(user, "создан клиент", username, f"{org.strip()}, API {'включён' if api else 'выключен'}")
    return redirect("/admin/clients", msg=f"Клиент «{org.strip()}» создан, логин {username}")  # после создания показываем список


@router.post("/admins/create")
def create_admin(org: str = Form(...), username: str = Form(...), password: str = Form(...), user=Depends(admin_user)):
    username = username.strip()
    if len(password) < 8 or not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username) or q("SELECT 1 FROM users WHERE username=?", username).fetchone():
        return redirect("/admin/new", msg="Не создано: логин занят или неверен (3–32 символа: латиница, цифры, _ . -), либо пароль короче 8 символов")
    q("INSERT INTO users(username,org,role,pw_hash,created) VALUES(?,?,?,?,?)", username, org.strip(), "admin", hash_pw(password), time.time())
    audit(user, "создан администратор", username, org.strip())
    return redirect("/admin/clients", msg=f"Администратор «{org.strip()}» создан, логин {username}")


def _client(uid: int):
    """Учётка для управления: клиент или другой администратор (себя отключать нельзя)."""
    u = q("SELECT * FROM users WHERE id=?", uid).fetchone()
    if not u:
        raise HTTPException(404)
    return u


@router.post("/clients/{uid}/api")
def toggle_api(uid: int, on: str = Form(""), user=Depends(admin_user)):
    u = _client(uid)
    if u["role"] != "client":
        raise HTTPException(400)
    q("UPDATE users SET api_enabled=? WHERE id=?", 1 if on else 0, uid)
    audit(user, "API " + ("включён" if on else "выключен"), u["username"])
    return redirect("/admin/clients", msg=f"API для «{u['org']}» {'включён' if on else 'выключен'}")


@router.post("/clients/{uid}/active")
def toggle_active(uid: int, user=Depends(admin_user)):
    u = _client(uid)
    if u["id"] == user["id"]:
        return redirect("/admin/clients", msg="Себя отключить нельзя")
    q("UPDATE users SET active=? WHERE id=?", 0 if u["active"] else 1, uid)
    audit(user, "учётка отключена" if u["active"] else "учётка включена", u["username"], u["role"])
    return redirect("/admin/clients", msg=f"Клиент «{u['org']}» {'отключён' if u['active'] else 'включён'}")


@router.post("/clients/{uid}/password")
def reset_password(uid: int, password: str = Form(...), user=Depends(admin_user)):
    u = _client(uid)
    if len(password) < 8:
        return redirect("/admin/clients", msg="Пароль короче 8 символов")
    q("UPDATE users SET pw_hash=? WHERE id=?", hash_pw(password), uid)
    audit(user, "сброшен пароль", u["username"], u["role"])
    return redirect("/admin/clients", msg=f"Пароль клиента «{u['org']}» изменён")


# ---------- карточка клиента: приоритет, лимиты, веб-хук ----------
@router.get("/clients/{uid}")
def client_card(uid: int, msg: str = "", user=Depends(admin_user)):
    u = _client(uid)
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    left, why = upload_allowance(uid)
    used = q("SELECT count(*) c FROM jobs WHERE owner=? AND created>=?", uid, day_start()).fetchone()["c"]
    busy = q("SELECT count(*) c FROM jobs WHERE owner=? AND status IN ('queued','asr','scoring')", uid).fetchone()["c"]
    prio = "".join(f'<option value="{k}" {"selected" if (u["priority"] or 0) == k else ""}>{v}</option>' for k, v in PRIORITY.items())
    limits = (f'<form class="card pad newacc" method="post" action="/admin/clients/{uid}/limits" style="max-width:520px"><h3 style="padding:0">Очередь и лимиты</h3>'
              f'<label class="muted">Приоритет в очереди обработки<select class="inp" name="priority">{prio}</select></label>'
              f'<p class="muted small" style="margin:0">Записи клиента с высоким приоритетом обрабатываются раньше тех, что ждут с обычным; внутри одного приоритета — по порядку загрузки. '
              f'Уже стоящие в очереди записи не переставляются.</p>'
              f'<label class="muted">Записей в сутки (0 — без ограничения)<input class="inp" type="number" name="daily_limit" min="0" max="100000" value="{u["daily_limit"] or 0}"></label>'
              f'<label class="muted">Записей в очереди одновременно (0 — без ограничения)<input class="inp" type="number" name="queue_limit" min="0" max="100000" value="{u["queue_limit"] or 0}"></label>'
              f'<p class="muted small" style="margin:0">Сегодня загружено: {used}, сейчас в обработке и очереди: {busy}. Лишние записи при загрузке не принимаются, остальные принимаются.</p>'
              f'<button class="btn p">Сохранить</button></form>')
    log = "".join(f'<tr class="logrow"><td>{fmt_dt(w["ts"])}</td><td>{esc(w["event"])}</td><td>{w["attempt"]}</td>'
                  f'<td><span class="chip {"g" if w["ok"] else "r"}">{esc(w["status_code"] or "нет ответа")}</span> <span class="muted small">{esc(w["error"] or "")}</span></td></tr>'
                  for w in hooks.recent(uid, 8))
    hook = (f'<form class="card pad newacc" method="post" action="/admin/clients/{uid}/webhook" style="max-width:620px"><h3 style="padding:0">Веб-хук</h3>'
            f'<p class="muted small" style="margin:0">Когда звонок оценён (или упал с ошибкой), сервер отправит POST с JSON на этот адрес. Адрес задаёт только администратор: сервер может обращаться и во внутреннюю сеть.</p>'
            f'<input class="inp" name="url" value="{esc(u["webhook_url"] or "")}" placeholder="https://crm.example.kz/hooks/callqa" maxlength="500">'
            f'<label class="chk"><input type="checkbox" name="on" value="1" {"checked" if u["webhook_enabled"] else ""}> отправлять уведомления</label>'
            f'<p class="muted small" style="margin:0">Секрет подписи: {("<code>" + esc(u["webhook_secret"][:8]) + "…</code> (клиент видит его в «Профиле»)") if u["webhook_secret"] else "будет создан при сохранении адреса"}.</p>'
            f'<div><button class="btn p">Сохранить</button> <button class="btn" formaction="/admin/clients/{uid}/webhook/test" formnovalidate>Проверить отправку</button> '
            f'<button class="btn" formaction="/admin/clients/{uid}/webhook/secret" formnovalidate onclick="return confirm(\'Выпустить новый секрет? Клиент должен будет обновить его у себя.\')">Новый секрет</button></div></form>')
    deliveries = (f'<div class="card" style="max-width:620px;margin-top:14px;overflow:auto"><h3>Последние доставки</h3><table class="list plain"><tr><th>Когда</th><th>Событие</th><th>Попытка</th><th>Результат</th></tr>{log}</table></div>' if log else "")
    title = f'<span class="back"><a href="/admin/clients">‹ Клиенты</a> /</span>{esc(u["org"] or u["username"])}'
    body = f'{banner}<div class="row">{limits}<div style="flex:1;min-width:320px">{hook}{deliveries}</div></div>'
    return layout("clients", title, body, user)


@router.post("/clients/{uid}/limits")
def save_limits(uid: int, priority: int = Form(0), daily_limit: str = Form("0"), queue_limit: str = Form("0"), user=Depends(admin_user)):
    u = _client(uid)
    if priority not in PRIORITY or not daily_limit.strip().isdigit() or not queue_limit.strip().isdigit() or int(daily_limit) > 100000 or int(queue_limit) > 100000:
        return redirect(f"/admin/clients/{uid}", msg="Не сохранено: приоритет и лимиты — целые числа от 0 до 100000")
    q("UPDATE users SET priority=?, daily_limit=?, queue_limit=? WHERE id=?", priority, int(daily_limit), int(queue_limit), uid)
    audit(user, "изменены очередь и лимиты", u["username"], f"приоритет {PRIORITY[priority]}, в сутки {int(daily_limit)}, в очереди {int(queue_limit)}")
    return redirect(f"/admin/clients/{uid}", msg="Сохранено")


@router.post("/clients/{uid}/webhook")
def save_webhook(uid: int, url: str = Form(""), on: str = Form(""), user=Depends(admin_user)):
    u = _client(uid)
    url = url.strip()
    if url and not hooks.valid_url(url):
        return redirect(f"/admin/clients/{uid}", msg="Не сохранено: адрес должен начинаться с http:// или https://")
    if on and not url:
        return redirect(f"/admin/clients/{uid}", msg="Не сохранено: чтобы включить веб-хук, укажите адрес")
    secret = u["webhook_secret"] or (hooks.new_secret() if url else None)
    q("UPDATE users SET webhook_url=?, webhook_enabled=?, webhook_secret=? WHERE id=?", url or None, 1 if (on and url) else 0, secret, uid)
    audit(user, "изменён веб-хук", u["username"], f"{url or 'адрес убран'}, {'включён' if on and url else 'выключен'}")
    return redirect(f"/admin/clients/{uid}", msg="Сохранено")


@router.post("/clients/{uid}/webhook/secret")
def new_webhook_secret(uid: int, user=Depends(admin_user)):
    u = _client(uid)
    q("UPDATE users SET webhook_secret=? WHERE id=?", hooks.new_secret(), uid)
    audit(user, "выпущен новый секрет веб-хука", u["username"])
    return redirect(f"/admin/clients/{uid}", msg="Новый секрет выпущен: клиент видит его в «Профиле»")


@router.post("/clients/{uid}/webhook/test")
def test_webhook(uid: int, user=Depends(admin_user)):
    _client(uid)
    ok, info = hooks.ping(uid)
    return redirect(f"/admin/clients/{uid}", msg=("Тестовый запрос доставлен" if ok else f"Не доставлено: {info}"))


# ---------- модели ----------
CATALOG = yaml.safe_load((Path(__file__).parent / "llm_catalog.yaml").read_text(encoding="utf-8"))["models"]
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}$")
PULL = {"tag": "", "state": "idle", "pct": 0, "text": "", "error": ""}  # одна загрузка модели за раз
_pull_lock = threading.Lock()


def _llm_base() -> str:
    return scoring.LLM_URL.rstrip("/").removesuffix("/v1")


def ollama_models():
    """Имена моделей встроенного Ollama; None — Ollama по адресу локального сервера не отвечает."""
    try:
        r = httpx.get(f"{_llm_base()}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return None


def llm_options() -> list:
    """Модели локального сервера: Ollama, LiteLLM (без облачных «collector»/«embed») или любой OpenAI-совместимый /v1/models."""
    names = ollama_models()
    if names:
        return names
    try:  # LiteLLM: /model/info — только реально настроенные модели; /models отдаёт и старые имена из прав ключа
        r = httpx.get(f"{_llm_base()}/model/info", headers={"Authorization": f"Bearer {scoring.LLM_KEY}"}, timeout=10)
        ids = [m["model_name"] for m in r.json()["data"] if m["model_name"] not in ("embed", "collector")]
        if ids:
            return ids
    except Exception:
        pass
    try:  # стандартный список моделей OpenAI-совместимого сервера (vLLM, llama.cpp, LM Studio)
        r = httpx.get(f"{scoring.LLM_URL.rstrip('/')}/models", headers={"Authorization": f"Bearer {scoring.LLM_KEY}"}, timeout=10)
        ids = [m["id"] for m in r.json()["data"]]
        if ids:
            return ids
    except Exception:
        pass
    return [setting("llm_model")]


def _pull_worker(tag: str):
    try:
        with httpx.stream("POST", f"{_llm_base()}/api/pull", json={"model": tag, "stream": True}, timeout=None) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise RuntimeError(d["error"])
                PULL["text"] = d.get("status", "")
                if d.get("total"):
                    PULL["pct"] = int(100 * d.get("completed", 0) / d["total"])
        PULL.update(state="done", pct=100, text="готово")
    except Exception as e:
        PULL.update(state="error", error=str(e)[:200] or "ошибка загрузки")


def _local_models_block(cur_llm: str) -> str:
    """Блок выбора локальной модели: установленные, каталог для скачивания, инструкция для ручной загрузки."""
    installed = ollama_models()
    manual = ('<details><summary>Как загрузить другую модель самостоятельно</summary>'
              '<p class="small">На сервере в папке установки: <code>docker compose exec ollama ollama pull имя:тег</code> '
              '(список — <a href="https://ollama.com/library" target="_blank" rel="noopener">ollama.com/library</a>), затем обновите эту страницу. '
              'Свою модель в формате GGUF: положите файл в <code>volumes/models/ollama</code>, создайте <code>Modelfile</code> со строкой <code>FROM /root/.ollama/файл.gguf</code> '
              'и выполните <code>docker compose exec ollama ollama create имя -f /root/.ollama/Modelfile</code>.</p></details>')
    if installed is None:
        return ('<p class="muted small">Встроенный сервер локальных моделей (Ollama) не запущен или не найден по адресу '
                f'<code>{esc(_llm_base())}</code>. Запустите установку с ключом <code>--with-llm</code> (и <code>--gpu</code>, если есть видеокарта NVIDIA) '
                'либо переключитесь на «Внешний API».</p>' + manual)
    opts = "".join(f'<option value="{esc(m)}" {"selected" if m == cur_llm else ""}>{esc(m)}</option>' for m in installed) or '<option value="">— сначала скачайте модель из списка ниже —</option>'
    rows = ""
    for m in CATALOG:
        have = m["tag"] in installed or m["tag"] + ":latest" in installed
        act = ('<span class="chip g">установлена</span>' if have else
               f'<button class="btn sm" formaction="/admin/models/pull" name="tag" value="{esc(m["tag"])}" formnovalidate>Скачать</button>')
        rows += (f'<tr><td><b>{esc(m["title"])}</b><br><span class="muted small">{esc(m["tag"])}</span></td><td>{m["size_gb"]} ГБ<br>'
                 f'<span class="muted small">видеопамять ≈ {m["vram_gb"]} ГБ</span></td><td class="small">{esc(m["note"])}</td><td>{act}</td></tr>')
    run = PULL["state"] == "running"
    state = {"running": f"Скачивается {esc(PULL['tag'])}: {PULL['pct']} % ({esc(PULL['text'])})", "error": f"Ошибка загрузки {esc(PULL['tag'])}: {esc(PULL['error'])}",
             "done": f"Модель {esc(PULL['tag'])} скачана"}.get(PULL["state"], "")
    return (f'<p><select class="inp" name="llm_model" style="width:100%">{opts}</select></p>'
            f'<div id="pull" data-run="{int(run)}" class="small" style="margin:6px 0">{state}</div>'
            '<p class="muted small">Записи остаются на сервере. Модель из списка скачивается один раз (десятки ГБ), потом выберите её выше и нажмите «Сохранить».</p>'
            f'<table class="list plain">{rows}</table>'
            '<p><input class="inp" name="custom" placeholder="Другая модель из каталога Ollama, например mistral-small3.2:24b" style="width:70%"> '
            '<button class="btn sm" formaction="/admin/models/pull" formnovalidate>Скачать по имени</button></p>' + manual)


PULL_JS = ("<script>(function(){var b=document.getElementById('pull');if(!b||b.dataset.run!=='1')return;"
           "function t(){fetch('/admin/models/pull-status').then(function(r){return r.json()}).then(function(d){"
           "if(d.state==='running'){b.textContent='Скачивается '+d.tag+': '+d.pct+' % ('+d.text+')';setTimeout(t,2000)}else{location.reload()}})}t()})();</script>")


@router.post("/models/pull")
def pull_model(tag: str = Form(""), custom: str = Form(""), user=Depends(admin_user)):
    tag = (tag or custom).strip()
    if not TAG_RE.match(tag):
        return redirect("/admin/models", msg="Укажите имя модели, например qwen3:14b")
    if ollama_models() is None:
        return redirect("/admin/models", msg="Сервер локальных моделей (Ollama) не запущен: загрузка невозможна")
    with _pull_lock:
        if PULL["state"] == "running":
            return redirect("/admin/models", msg=f"Уже скачивается {PULL['tag']}, дождитесь окончания")
        PULL.update(tag=tag, state="running", pct=0, text="старт", error="")
    audit(user, "загрузка локальной модели", details=tag)
    threading.Thread(target=_pull_worker, args=(tag,), daemon=True).start()
    return redirect("/admin/models", msg=f"Загрузка {tag} началась. Страница обновится сама, когда она закончится.")


@router.get("/models/pull-status")
def pull_status(user=Depends(admin_user)):
    return PULL


def _radios(name: str, cur: str, opts: dict) -> str:
    return "".join(f'<label class="chk"><input type="radio" name="{name}" value="{k}" {"checked" if k == cur else ""} onchange="sw(\'{name}\')"> {esc(v)}</label> ' for k, v in opts.items())


def _secret_hint(key: str) -> str:
    return "ключ сохранён, оставьте пустым, чтобы не менять" if setting(key) else "ключ API"


MODELS_JS = ("<script>function sw(n){var v=document.querySelector('input[name='+n+']:checked').value;"
             "document.querySelectorAll('[data-'+n+']').forEach(function(e){e.style.display=e.dataset[n]===v?'':'none'})}"
             "sw('llm_mode');sw('asr_device');</script>")


@router.get("/models")
def models(msg: str = "", user=Depends(admin_user)):
    cur_llm, cur_asr, cur_lang, cur_dev, cur_mode = setting("llm_model"), setting("asr_engine"), setting("whisper_lang"), setting("asr_device"), setting("llm_mode")
    engs = ""
    for dev, items in asr.ENGINES.items():
        opts = "".join(f'<option value="{k}" {"selected" if k == cur_asr else ""}>{esc(v)}</option>' for k, v in items.items())
        engs += f'<p data-asr_device="{dev}"><select class="inp" name="asr_engine_{dev}" style="width:100%">{opts}</select></p>'
    lang = "".join(f'<option value="{k}" {"selected" if k == cur_lang else ""}>{v}</option>' for k, v in (("ru", "русский"), ("kk", "казахский"), ("auto", "определять автоматически")))
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    body = (f'{banner}<form class="card pad" method="post" action="/admin/models" style="max-width:760px">'
            f'<p><b>Модель для аналитики (оценка звонка и разметка ролей)</b></p><p>{_radios("llm_mode", cur_mode, {"local": "Модель на нашем сервере", "api": "Внешний API"})}</p>'
            f'<div data-llm_mode="local">{_local_models_block(cur_llm)}</div>'
            f'<div data-llm_mode="api"><p><input class="inp" name="llm_api_url" value="{esc(setting("llm_api_url"))}" placeholder="Адрес API, например https://api.openai.com/v1" style="width:100%"></p>'
            f'<p><input class="inp" name="llm_api_key" type="password" autocomplete="off" placeholder="{_secret_hint("llm_api_key")}" style="width:100%"></p>'
            f'<p><input class="inp" name="llm_api_model" value="{esc(setting("llm_api_model"))}" placeholder="Название модели" style="width:100%"></p>'
            f'<p><button class="btn sm" formaction="/admin/models/test-llm" formnovalidate>Проверить подключение</button></p>'
            f'<p class="muted small">Подойдёт любой OpenAI-совместимый API: облачный или локальный (vLLM, Ollama, LM Studio, свой LiteLLM). '
            f'<b>При внешнем облачном API тексты расшифровок уходят за пределы сервера</b>: убедитесь, что договор с клиентами это допускает.</p></div>'
            f'<p style="margin-top:18px"><b>Модель для расшифровки аудио</b></p><p>{_radios("asr_device", cur_dev, asr.DEVICES)}</p>{engs}'
            f'<div data-asr_device="api"><p><input class="inp" name="asr_api_url" value="{esc(setting("asr_api_url"))}" placeholder="Адрес API, например https://api.openai.com/v1" style="width:100%"></p>'
            f'<p><input class="inp" name="asr_api_key" type="password" autocomplete="off" placeholder="{_secret_hint("asr_api_key")}" style="width:100%"></p>'
            f'<p><input class="inp" name="asr_api_model" value="{esc(setting("asr_api_model"))}" placeholder="Название модели, например whisper-1" style="width:100%"></p>'
            f'<p class="muted small">Нужен API, совместимый с OpenAI <code>/audio/transcriptions</code>. Аудио реплик уходит на этот адрес.</p></div>'
            f'<p><label>Язык распознавания по умолчанию <select class="inp" name="whisper_lang">{lang}</select></label></p>'
            f'<p class="muted small">Язык можно выбрать и при загрузке каждой записи. Для казахского и смеси языков на процессоре автоматически включается Whisper large-v3-turbo '
            f'(GigaAM понимает только русский). Качество на казахских записях пилота ещё не измерялось.</p>'
            f'<p style="margin-top:18px"><b>Роли в записи одним каналом (моно)</b></p>'
            f'<p><select class="inp" name="mono_diarize" style="width:100%"><option value="off" {"selected" if setting("mono_diarize") != "on" else ""}>Только по смыслу разговора (модель определяет роль каждой реплики)</option>'
            f'<option value="on" {"selected" if setting("mono_diarize") == "on" else ""}>Сначала разделить голоса по звуку, затем определить, кто сотрудник (экспериментально)</option></select></p>'
            f'<p class="muted small">Разделение по звуку — простой метод без нейросети; на 8 стерео-записях пилота, сведённых в моно, угадал голос в 91 % реплик (на одной записи из восьми — только в 53 %). '
            f'Работает на записях от 30 реплик, на коротких остаётся разметка по смыслу. Роль в любой реплике можно исправить в карточке звонка. '
            f'По умолчанию выключено: включайте после пробы на живых звонках. Стерео-записи разделяются по каналам в любом режиме.</p>'
            f'<p style="margin-top:18px"><b>Лимит длины записи</b></p>'
            f'<p><label><input class="inp" type="number" name="max_minutes" min="0" max="600" value="{esc(setting("max_minutes"))}" style="width:90px"> минут</label></p>'
            f'<p class="muted small">Запись длиннее лимита отклоняется до распознавания (ошибка в карточке звонка), очередь не занимается. 0 — без ограничения. '
            f'Для справки: на процессоре GigaAM считает ≈ 3 с на минуту записи, Whisper — до 2 мин на минуту.</p>'
            f'<p style="margin-top:18px"><b>Срок хранения аудио</b></p>'
            f'<p><label><input class="inp" type="number" name="retention_days" min="0" max="3650" value="{esc(setting("retention_days"))}" style="width:90px"> дней</label></p>'
            f'<p class="muted small">По умолчанию 90 дней (3 месяца). Аудио законченных звонков старше срока удаляется раз в сутки; расшифровка и оценка остаются. '
            f'Каждое удаление записывается в журнал. 0 — хранить бессрочно.</p>'
            f'<div data-asr_device="gpu"><p class="muted small">Видеокарты сейчас заняты основной моделью. Whisper на видеокарте помещается только при уменьшенном резерве llama-swap '
            f'(решение владельца сервера, см. docs/13); иначе обработка завершится ошибкой нехватки памяти.</p></div>'
            f'<p class="muted small">Выбор действует на новые записи; звонки в очереди обработаются с настройками на момент старта. '
            f'Загруженный в память движок меняется при первой записи после переключения (несколько секунд).</p>'
            f'<p><button class="btn p">Сохранить</button></p></form>{MODELS_JS}{PULL_JS}')
    return layout("models", "Модели", body, user)


@router.post("/models")
def save_models(llm_mode: str = Form("local"), llm_model: str = Form(""), llm_api_url: str = Form(""), llm_api_key: str = Form(""), llm_api_model: str = Form(""),
                asr_device: str = Form("cpu"), asr_engine_cpu: str = Form(""), asr_engine_gpu: str = Form(""), whisper_lang: str = Form("ru"),
                asr_api_url: str = Form(""), asr_api_key: str = Form(""), asr_api_model: str = Form(""), max_minutes: str = Form("90"), retention_days: str = Form("90"),
                mono_diarize: str = Form("off"), user=Depends(admin_user)):
    if llm_mode not in ("local", "api") or asr_device not in asr.DEVICES or whisper_lang not in ("ru", "kk", "auto") or mono_diarize not in ("off", "on"):
        return redirect("/admin/models", msg="Не сохранено: неизвестное значение")
    if not max_minutes.strip().isdigit() or int(max_minutes) > 600:
        return redirect("/admin/models", msg="Не сохранено: лимит длины записи — целое число минут от 0 до 600")
    if not retention_days.strip().isdigit() or int(retention_days) > 3650:
        return redirect("/admin/models", msg="Не сохранено: срок хранения — целое число дней от 0 до 3650")
    if llm_mode == "local" and llm_model not in llm_options():
        return redirect("/admin/models", msg="Не сохранено: неизвестная локальная модель")
    if llm_mode == "api" and not (llm_api_url.strip().startswith(("http://", "https://")) and llm_api_model.strip()):
        return redirect("/admin/models", msg="Не сохранено: для внешнего API нужны адрес (http/https) и название модели")
    engine = {"cpu": asr_engine_cpu, "gpu": asr_engine_gpu}.get(asr_device) or setting("asr_engine")
    if asr_device != "api" and engine not in asr.ENGINES[asr_device]:
        return redirect("/admin/models", msg="Не сохранено: этот движок недоступен на выбранном устройстве")
    if asr_device == "api" and not (asr_api_url.strip().startswith(("http://", "https://")) and asr_api_model.strip()):
        return redirect("/admin/models", msg="Не сохранено: для внешнего API распознавания нужны адрес (http/https) и название модели")
    before = {k: setting(k) for k in ("llm_mode", "llm_model", "llm_api_url", "llm_api_model", "asr_device", "asr_engine", "asr_api_url", "asr_api_model",
                                      "whisper_lang", "max_minutes", "retention_days", "mono_diarize")}
    set_setting("llm_mode", llm_mode)
    if llm_mode == "local":
        set_setting("llm_model", llm_model)
    else:
        set_setting("llm_api_url", llm_api_url.strip())
        set_setting("llm_api_model", llm_api_model.strip())
        if llm_api_key.strip():  # пустое поле — оставить прежний ключ
            set_setting("llm_api_key", llm_api_key.strip())
    set_setting("asr_device", asr_device)
    if asr_device != "api":
        set_setting("asr_engine", engine)
    else:
        set_setting("asr_api_url", asr_api_url.strip())
        set_setting("asr_api_model", asr_api_model.strip())
        if asr_api_key.strip():
            set_setting("asr_api_key", asr_api_key.strip())
    set_setting("whisper_lang", whisper_lang)
    set_setting("max_minutes", str(int(max_minutes)))
    set_setting("retention_days", str(int(retention_days)))
    set_setting("mono_diarize", mono_diarize)
    changes = [f"{k}: {v or '—'} → {setting(k) or '—'}" for k, v in before.items() if setting(k) != v]
    if llm_api_key.strip():
        changes.append("ключ LLM API изменён")
    if asr_api_key.strip():
        changes.append("ключ ASR API изменён")  # значения ключей в журнал не пишем
    audit(user, "изменены настройки моделей", details="; ".join(changes) or "без изменений")
    return redirect("/admin/models", msg="Сохранено. Новые записи пойдут с выбранными моделями.")


@router.post("/models/test-llm")
def test_llm(llm_api_url: str = Form(""), llm_api_key: str = Form(""), user=Depends(admin_user)):
    key = llm_api_key.strip() or setting("llm_api_key")
    try:
        r = httpx.get(f"{llm_api_url.strip().rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
        ok = r.status_code == 200
        msg = "Подключение работает (настройки не сохранены, нажмите «Сохранить»)" if ok else f"API ответил кодом {r.status_code}"
    except Exception as e:
        msg = f"Не удалось подключиться: {str(e)[:120]}"
    return redirect("/admin/models", msg=msg)


# ---------- сервер ----------
def sh(cmd: list, timeout: int = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def cpu_percent(interval: float = 0.4) -> float:
    """Загрузка всех ядер в процентах: две выборки /proc/stat с паузой."""
    def snap():
        v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        return v[3] + v[4], sum(v)  # idle + iowait, всего
    i1, t1 = snap()
    time.sleep(interval)
    i2, t2 = snap()
    return 100 * (1 - (i2 - i1) / max(t2 - t1, 1))


def bar(pct: float) -> str:
    c = "#D9534F" if pct >= 90 else "#E9A23B" if pct >= 75 else "#E5007E"
    return f'<div class="trk" style="width:140px;display:inline-block;vertical-align:middle"><i style="width:{min(pct, 100):.0f}%;background:{c}"></i></div> {pct:.0f} %'


@router.get("/server")
def server(user=Depends(admin_user)):
    load = os.getloadavg()
    cpus = os.cpu_count() or 1
    la = [min(v / cpus * 100, 100) for v in load]  # средняя очередь на ядро в процентах от числа ядер
    cpu_now = cpu_percent()
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if ":" in l}
    mem_used = (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"] * 100
    up = float(open("/proc/uptime").read().split()[0])
    disks = ""
    for path in ("/", str(DATA)):
        try:
            d = shutil.disk_usage(path)
            disks += f'<tr><td>{path}</td><td>{bar(d.used / d.total * 100)}</td><td>{d.used / 2**30:.0f} из {d.total / 2**30:.0f} ГБ</td></tr>'
        except OSError:
            pass
    gpus = ""
    for line in sh(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"]).splitlines():
        n, mu, mt, ut, tp = [x.strip() for x in line.split(",")]
        gpus += f'<tr><td>{esc(n)}</td><td>{bar(int(mu) / int(mt) * 100)}</td><td>{int(mu) / 1024:.1f} из {int(mt) / 1024:.0f} ГБ</td><td>загрузка {ut} %</td><td>{tp} °C</td></tr>'
    try:
        if not os.environ.get("CALLQA_LLAMA_SWAP_URL"):
            raise RuntimeError("не настроено")
        running = httpx.get(os.environ["CALLQA_LLAMA_SWAP_URL"].rstrip("/") + "/running", timeout=3).json().get("running", [])
        models = ", ".join(f'{esc(m["model"])} <span class="chip {"g" if m["state"] == "ready" else "a"}">{esc(m["state"])}</span>' for m in running) or "—"
    except Exception:
        models = '<span class="muted">нет данных</span>'
    units = [u for u in os.environ.get("CALLQA_SYSTEMD_UNITS", "").split(",") if u.strip()]  # в контейнере пусто
    svc = ""
    for name in units:
        st = sh(["systemctl", "is-active", name.strip()]) or "неизвестно"
        svc += f'<tr><td>{esc(name.strip())}</td><td><span class="chip {"g" if st == "active" else "r"}">{esc(st)}</span></td></tr>'
    if not units:
        svc = '<tr><td class="muted">контейнерный запуск: состояние служб смотрите через docker compose ps</td></tr>'
    jobs_stat = {r["status"]: r["c"] for r in q("SELECT status, count(*) c FROM jobs GROUP BY status").fetchall()}
    today = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    day = q("SELECT count(*) c, coalesce(sum(duration),0) d, coalesce(sum(asr_sec),0) a, coalesce(sum(llm_sec),0) l FROM jobs WHERE created>=?", today).fetchone()
    body = (f'<div class="kpis"><div class="card kpi"><small>Нагрузка процессора сейчас</small><b>{cpu_now:.0f} %</b><span>за 1 / 5 / 15 мин: {la[0]:.0f} % · {la[1]:.0f} % · {la[2]:.0f} % ({cpus} ядер)</span></div>'
            f'<div class="card kpi"><small>Память</small><b>{mem_used:.0f} %</b><span>свободно {mem["MemAvailable"] / 2**20:.0f} из {mem["MemTotal"] / 2**20:.0f} ГБ</span></div>'
            f'<div class="card kpi"><small>Очередь обработки</small><b>{jobs.qsize()}</b><span>в работе: {jobs_stat.get("asr", 0) + jobs_stat.get("scoring", 0)} · ошибок: {jobs_stat.get("error", 0)}</span></div>'
            f'<div class="card kpi"><small>Записей сегодня</small><b>{day["c"]}</b><span>аудио {mmss(day["d"])}, обработка {(day["a"] + day["l"]) / 60:.1f} мин</span></div>'
            f'<div class="card kpi"><small>Работает</small><b>{up / 86400:.1f} дн</b><span>с последней перезагрузки сервера</span></div></div>'
            f'<div class="card pad" style="margin-bottom:14px"><h3>Видеокарты</h3><table class="list plain">{gpus or "<tr><td class=muted>нет данных</td></tr>"}</table>'
            f'<p class="muted small">Загружено в llama-swap: {models}</p></div>'
            f'<div class="row"><div class="card pad" style="flex:1"><h3>Диски</h3><table class="list plain">{disks}</table></div>'
            f'<div class="card pad" style="flex:1"><h3>Службы</h3><table class="list plain">{svc}</table></div></div>')
    return layout("server", "Сервер", body, user, '<span class="demo">обновляется каждые 6 с</span>', refresh=True)
