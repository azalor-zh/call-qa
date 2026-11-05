"""Личный кабинет: свои чек-листы, смена пароля, уведомления в Telegram. Доступно и клиенту, и администратору."""
import json
import re
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

import hooks
import notify
import rubrics
from core import audit, check_pw, current_user, esc, hash_pw, layout, q, redirect, set_session_cookie

router = APIRouter()


# ---------- чек-листы ----------
def _item_row(it: dict) -> str:
    lv = it.get("levels", {})
    g = lambda k: esc(lv.get(k, lv.get(str(k), "")))  # noqa: E731 — в JSON ключи уровней становятся строками
    return (f'<div class="irow"><div class="irow-h"><input class="inp" name="title" placeholder="Название пункта" value="{esc(it.get("title", ""))}" required>'
            f'<label class="muted">Вес <input class="inp sm" name="weight" type="number" min="1" max="100" value="{esc(it.get("weight", 10))}" required style="width:70px"></label>'
            f'<label class="muted">Критичный <select class="inp sm" name="crit"><option value="0">нет</option><option value="1" {"selected" if it.get("critical") else ""}>да</option></select></label>'
            f'<button type="button" class="btn sm dng" onclick="this.closest(\'.irow\').remove()">Убрать</button></div>'
            f'<input class="inp" name="definition" placeholder="Что проверяем (определение пункта)" value="{esc(it.get("definition", ""))}">'
            f'<div class="lv"><input class="inp" name="l2" placeholder="2 — выполнено: что считать" value="{g(2)}">'
            f'<input class="inp" name="l1" placeholder="1 — частично" value="{g(1)}"><input class="inp" name="l0" placeholder="0 — не выполнено" value="{g(0)}"></div>'
            f'<input class="inp" name="when" placeholder="Применимо, если… (необязательно; иначе пункт ставится «н/п»)" value="{esc(it.get("applicable_when", ""))}"></div>')


EDITOR_JS = """
function addRow(){var t=document.getElementById('rowt').content.cloneNode(true);document.getElementById('rows').appendChild(t);}
"""


def _form(user, action: str, title: str, name_: str, emp: str, ctx: str, items: list, err: str = "", rid=None):
    rows = "".join(_item_row(i) for i in items)
    banner = f'<div class="banner">{esc(err)}</div>' if err else ""
    body = (f'{banner}<form method="post" action="{action}" class="card pad" style="max-width:980px">'
            f'<p><label class="muted">Название чек-листа<input class="inp" name="name" value="{esc(name_)}" maxlength="80" required style="width:100%"></label></p>'
            f'<p><label class="muted">Как называть сотрудника в расшифровке (например: Менеджер, Оператор, Техспециалист)'
            f'<input class="inp" name="employee_title" value="{esc(emp)}" maxlength="40" style="width:100%"></label></p>'
            f'<p><label class="muted">Контекст компании: что сотруднику можно и нельзя (скидки, сроки, регламент) — модель учитывает это при оценке'
            f'<textarea class="inp" name="context" rows="4" maxlength="2000" style="width:100%" placeholder="Например: скидка до 10 % без согласования, доставка по городу 1–2 дня…">{esc(ctx)}</textarea></label></p>'
            f'<h3>Пункты</h3><div id="rows">{rows}</div>'
            f'<template id="rowt">{_item_row({})}</template>'
            f'<p><button type="button" class="btn" onclick="addRow()">+ Добавить пункт</button></p>'
            f'<p class="muted small">Оценка по пункту: 2 — выполнено, 1 — частично, 0 — не выполнено; итог 0–100 считается по весам. Критичный пункт с оценкой 0 помечает звонок флагом.</p>'
            f'<p><button class="btn p">Сохранить чек-лист</button> <a class="btn" href="/rubrics">Отмена</a></p></form><script>{EDITOR_JS}</script>')
    return layout("rub", title, body, user)


@router.get("/rubrics")
def list_rubrics(msg: str = "", user=Depends(current_user)):
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    tpl = ""
    gone = rubrics.hidden(user)
    for key in ("sales", "support"):
        t = rubrics.template(key)
        if not t or key in gone:
            continue
        rows = "".join(f'<tr><td><b>{esc(i["title"])}</b>{" <span class=chip>критичный</span>" if i.get("critical") else ""}</td><td>{i["weight"]}</td><td>{esc(i["definition"])}</td></tr>' for i in t["items"])
        tpl += (f'<div class="card" style="margin-bottom:14px"><h3 style="padding:16px 18px 0">{esc(rubrics.TEMPLATE_NAMES[key])} · версия {esc(t["version"])} · черновик, не утверждён владельцем методики</h3>'
                f'<table class="list plain"><tr><th>Пункт</th><th>Вес</th><th>Определение</th></tr>{rows}</table>'
                f'<div class="pager"><a class="btn p" href="/rubrics/new?tpl={key}">Создать свой чек-лист на основе шаблона</a> '
                f'<form method="post" action="/rubrics/template/{key}/delete" style="display:inline" onsubmit="return confirm(\'Удалить шаблон у вас? У других клиентов он останется, прошлые звонки не изменятся. Вернуть можно в списке ниже.\')">'
                f'<button class="btn dng">Удалить шаблон</button></form></div></div>')
    if gone:
        back = "".join(f'<tr><td><b>{esc(rubrics.TEMPLATE_NAMES[k])}</b></td><td><form method="post" action="/rubrics/template/{k}/restore" style="display:inline">'
                       f'<button class="btn sm">Вернуть</button></form></td></tr>' for k in ("sales", "support") if k in gone)
        tpl += f'<div class="card" style="margin-bottom:14px"><h3 style="padding:16px 18px 0">Удалённые шаблоны</h3><table class="list plain"><tr><th>Шаблон</th><th></th></tr>{back}</table></div>'
    if not tpl:
        tpl = '<p class="muted">Шаблонов нет.</p>'
    where, args = ("", ()) if user["role"] == "admin" else ("WHERE r.owner=?", (user["id"],))
    own = q(f"SELECT r.*, u.org, u.username FROM rubrics r LEFT JOIN users u ON u.id=r.owner {where} ORDER BY r.updated DESC", *args).fetchall()
    rows = ""
    for r in own:
        items = json.loads(r["items"])
        owner_cell = f'<td>{esc(r["org"] or r["username"])}</td>' if user["role"] == "admin" else ""
        rows += (f'<tr><td><b>{esc(r["name"])}</b></td>{owner_cell}<td>{len(items)}</td><td>v{r["version"]}</td>'
                 f'<td><a class="btn sm" href="/rubrics/{r["id"]}">Изменить</a> '
                 f'<form method="post" action="/rubrics/{r["id"]}/delete" style="display:inline" onsubmit="return confirm(\'Удалить чек-лист? Уже оценённые звонки не изменятся.\')"><button class="btn sm dng">Удалить</button></form></td></tr>')
    own_h = "<th>Клиент</th>" if user["role"] == "admin" else ""
    mine = (f'<div class="card" style="margin-bottom:14px"><h3 style="padding:16px 18px 0">{"Чек-листы клиентов" if user["role"] == "admin" else "Мои чек-листы"}</h3>'
            + (f'<table class="list plain"><tr><th>Название</th>{own_h}<th>Пунктов</th><th>Версия</th><th></th></tr>{rows}</table>' if rows else
               '<p class="muted" style="padding:6px 18px 4px">Пока нет своих чек-листов. Создайте с нуля или на основе шаблона ниже.</p>')
            + '<div class="pager"><a class="btn p" href="/rubrics/new">Создать чек-лист</a></div></div>')
    return layout("rub", "Чек-листы", f"{banner}{mine}<h3 class=\"sect\">Шаблоны</h3>{tpl}", user)


@router.post("/rubrics/template/{key}/delete")
def delete_template(key: str, user=Depends(current_user)):
    busy = q("SELECT count(*) c FROM jobs WHERE rubric=? AND owner=? AND status IN ('queued','asr','scoring')", key, user["id"]).fetchone()["c"]
    if busy:
        return redirect("/rubrics", msg="Нельзя удалить: по этому шаблону сейчас идёт обработка ваших звонков")
    if not rubrics.hide(user, key):
        raise HTTPException(404)
    if user["role"] == "admin":
        audit(user, "удалён шаблон чек-листа", rubrics.TEMPLATE_NAMES[key])
    return redirect("/rubrics", msg=f"Шаблон «{rubrics.TEMPLATE_NAMES[key]}» удалён. Вернуть можно внизу страницы.")


@router.post("/rubrics/template/{key}/restore")
def restore_template(key: str, user=Depends(current_user)):
    if key not in rubrics.TEMPLATE_NAMES:
        raise HTTPException(404)
    rubrics.restore(user, key)
    return redirect("/rubrics", msg=f"Шаблон «{rubrics.TEMPLATE_NAMES[key]}» возвращён")


@router.get("/rubrics/new")
def new_rubric(tpl: str = "", user=Depends(current_user)):
    t = rubrics.template(tpl) if tpl else None
    items = t["items"] if t else [{"title": "", "weight": 20}]
    return _form(user, "/rubrics/new", "Новый чек-лист", f"{rubrics.TEMPLATE_NAMES[tpl].removeprefix('Шаблон: ')} (мой)" if t else "", rubrics.TEMPLATE_TITLES.get(tpl, "Сотрудник"), "", items)


def _read(form_name, emp, ctx, title, weight, definition, l2, l1, l0, crit, when):
    n = len(title)
    if not all(len(x) == n for x in (weight, definition, l2, l1, l0, crit, when)):
        return None, "Ошибка формы"
    return rubrics.parse_form(form_name, emp, ctx, title, weight, definition, l2, l1, l0, crit, when)


@router.post("/rubrics/new")
def create_rubric(name: str = Form(...), employee_title: str = Form("Сотрудник"), context: str = Form(""),
                  title: list[str] = Form(default=[]), weight: list[str] = Form(default=[]), definition: list[str] = Form(default=[]),
                  l2: list[str] = Form(default=[]), l1: list[str] = Form(default=[]), l0: list[str] = Form(default=[]),
                  crit: list[str] = Form(default=[]), when: list[str] = Form(default=[]), user=Depends(current_user)):
    items, err = _read(name, employee_title, context, title, weight, definition, l2, l1, l0, crit, when)
    if err:
        return _form(user, "/rubrics/new", "Новый чек-лист", name, employee_title, context, [{"title": t, "weight": w} for t, w in zip(title, weight)], err)
    rubrics.save(user, None, name, employee_title, context, items)
    return redirect("/rubrics", msg=f"Чек-лист «{name.strip()}» создан")


def _own(rid: int, user):
    r = q("SELECT * FROM rubrics WHERE id=?", rid).fetchone()
    if not rubrics.can_edit(r, user):
        raise HTTPException(404)
    return r


@router.get("/rubrics/{rid}")
def edit_rubric(rid: int, user=Depends(current_user)):
    r = _own(rid, user)
    return _form(user, f"/rubrics/{rid}", f"Чек-лист «{r['name']}»", r["name"], r["employee_title"], r["context"], json.loads(r["items"]), rid=rid)


@router.post("/rubrics/{rid}")
def save_rubric(rid: int, name: str = Form(...), employee_title: str = Form("Сотрудник"), context: str = Form(""),
                title: list[str] = Form(default=[]), weight: list[str] = Form(default=[]), definition: list[str] = Form(default=[]),
                l2: list[str] = Form(default=[]), l1: list[str] = Form(default=[]), l0: list[str] = Form(default=[]),
                crit: list[str] = Form(default=[]), when: list[str] = Form(default=[]), user=Depends(current_user)):
    r = _own(rid, user)
    items, err = _read(name, employee_title, context, title, weight, definition, l2, l1, l0, crit, when)
    if err:
        return _form(user, f"/rubrics/{rid}", "Чек-лист", name, employee_title, context, [{"title": t, "weight": w} for t, w in zip(title, weight)], err, rid)
    rubrics.save(user, rid, name, employee_title, context, items)
    if user["role"] == "admin" and r["owner"] != user["id"]:
        audit(user, "изменён чек-лист клиента", r["name"], f"клиент id {r['owner']}")
    return redirect("/rubrics", msg=f"Чек-лист «{name.strip()}» сохранён (новая версия). Оценённые звонки остались по прежней версии.")


@router.post("/rubrics/{rid}/delete")
def delete_rubric(rid: int, user=Depends(current_user)):
    r = _own(rid, user)
    busy = q("SELECT count(*) c FROM jobs WHERE rubric=? AND status IN ('queued','asr','scoring')", f"r{rid}").fetchone()["c"]
    if busy:
        return redirect("/rubrics", msg="Нельзя удалить: по этому чек-листу сейчас идёт обработка звонков")
    q("DELETE FROM rubrics WHERE id=?", rid)
    if user["role"] == "admin" and r["owner"] != user["id"]:
        audit(user, "удалён чек-лист клиента", r["name"], f"клиент id {r['owner']}")
    return redirect("/rubrics", msg=f"Чек-лист «{r['name']}» удалён")


# ---------- профиль ----------
@router.get("/profile")
def profile(msg: str = "", user=Depends(current_user)):
    banner = f'<div class="banner">{esc(msg)}</div>' if msg else ""
    token_note = ("" if notify.configured() else '<p class="bad small">Бот Telegram администратором ещё не подключён — уведомления не уйдут, пока не задан токен бота.</p>')
    pw = ('<form method="post" action="/profile/password" class="card pad" style="max-width:520px;margin-bottom:14px"><h3>Смена пароля</h3>'
          '<p><label class="muted">Текущий пароль<input class="inp" type="password" name="current" required autocomplete="current-password" style="width:100%"></label></p>'
          '<p><label class="muted">Новый пароль (от 8 символов)<input class="inp" type="password" name="new" minlength="8" required autocomplete="new-password" style="width:100%"></label></p>'
          '<p><label class="muted">Новый пароль ещё раз<input class="inp" type="password" name="new2" minlength="8" required autocomplete="new-password" style="width:100%"></label></p>'
          '<p><button class="btn p">Сменить пароль</button> <span class="muted small">После смены вы останетесь в системе, а остальные сеансы завершатся.</span></p></form>')
    tg = (f'<form method="post" action="/profile/telegram" class="card pad" style="max-width:520px"><h3>Уведомления в Telegram</h3>{token_note}'
          f'<p class="muted small">Сообщение придёт, когда итоговый балл звонка ниже {notify.LOW_SCORE}. В нём только балл, сотрудник, чек-лист и ссылка. '
          f'Откройте бота и нажмите «Старт», затем узнайте свой chat id (например, у бота @userinfobot).</p>'
          f'<p><label class="muted">Chat id<input class="inp" name="chat_id" value="{esc(user["tg_chat_id"] or "")}" placeholder="например 123456789" style="width:100%"></label></p>'
          f'<p><label class="chk"><input type="checkbox" name="on" value="1" {"checked" if user["tg_enabled"] else ""}> присылать уведомления о низком балле</label></p>'
          f'<p><button class="btn p">Сохранить</button> <button class="btn" formaction="/profile/telegram/test">Проверить отправку</button></p></form>')
    return layout("profile", "Профиль", f"{banner}<p class=\"muted\" style=\"margin-bottom:12px\">Вы вошли как <b>{esc(user['username'])}</b></p>{pw}{tg}{_webhook_card(user)}", user)


def _webhook_card(user) -> str:
    """Веб-хук виден клиенту только для чтения: адрес задаёт администратор (иначе сервер можно направить во внутреннюю сеть)."""
    u = q("SELECT * FROM users WHERE id=?", user["id"]).fetchone()
    if not u["webhook_url"]:
        return ('<div class="card pad" style="max-width:520px;margin-top:14px"><h3>Веб-хук</h3><p class="muted small">Сервер может сам сообщать вашей системе, что звонок оценён. '
                'Чтобы подключить, сообщите администратору адрес, на который слать уведомления.</p></div>')
    rows = "".join(f'<tr><td>{time.strftime("%d.%m %H:%M:%S", time.localtime(w["ts"]))}</td><td>{esc(w["event"])}</td>'
                   f'<td><span class="chip {"g" if w["ok"] else "r"}">{esc(w["status_code"] or "нет ответа")}</span></td></tr>' for w in hooks.recent(user["id"], 8))
    return (f'<div class="card pad" style="max-width:620px;margin-top:14px"><h3>Веб-хук</h3>'
            f'<p>Адрес: <code>{esc(u["webhook_url"])}</code> · {"<b>включён</b>" if u["webhook_enabled"] else "выключен"}</p>'
            f'<p class="muted small">Отправляем POST с JSON при событиях: <code>call.done</code> (оценён), <code>call.error</code> (ошибка), <code>call.reviewed</code> (проверен человеком). '
            f'В теле только итог (балл, сотрудник, флаги, ссылки); расшифровку и цитаты берите через API. Подпись: заголовок <code>X-CallQA-Signature: sha256=…</code> — '
            f'HMAC-SHA256 от строки «<code>X-CallQA-Timestamp</code> + точка + тело запроса» с вашим секретом. До 4 попыток (сразу, через 10 с, 1 и 5 минут); ответ 2xx считается успехом.</p>'
            f'<p class="muted small">Секрет: <code class="key">{esc(u["webhook_secret"] or "")}</code></p>'
            f'<form method="post" action="/profile/webhook/test"><button class="btn">Проверить отправку</button></form>'
            + (f'<table class="list plain" style="margin-top:10px"><tr><th>Когда</th><th>Событие</th><th>Ответ</th></tr>{rows}</table>' if rows else "") + '</div>')


@router.post("/profile/webhook/test")
def test_webhook(user=Depends(current_user)):
    if not user["webhook_url"]:
        return redirect("/profile", msg="Адрес веб-хука не задан администратором")
    ok, info = hooks.ping(user["id"])
    return redirect("/profile", msg=("Тестовый запрос доставлен" if ok else f"Не доставлено: {info}"))


@router.post("/profile/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), new2: str = Form(...), user=Depends(current_user)):
    if not check_pw(current, user["pw_hash"]):
        return redirect("/profile", msg="Текущий пароль указан неверно")
    if new != new2:
        return redirect("/profile", msg="Новые пароли не совпадают")
    if len(new) < 8 or new == current:
        return redirect("/profile", msg="Пароль должен быть от 8 символов и отличаться от текущего")
    q("UPDATE users SET pw_hash=? WHERE id=?", hash_pw(new), user["id"])
    if user["role"] == "admin":
        audit(user, "администратор сменил свой пароль")
    resp = RedirectResponse("/profile?msg=Пароль+изменён", 303)
    set_session_cookie(resp, request, q("SELECT * FROM users WHERE id=?", user["id"]).fetchone())
    return resp


@router.post("/profile/telegram")
def save_telegram(chat_id: str = Form(""), on: str = Form(""), user=Depends(current_user)):
    chat_id = chat_id.strip()
    if chat_id and not notify.CHAT_RE.match(chat_id):
        return redirect("/profile", msg="Chat id — это число (для групп начинается с минуса)")
    q("UPDATE users SET tg_chat_id=?, tg_enabled=? WHERE id=?", chat_id or None, 1 if (on and chat_id) else 0, user["id"])
    return redirect("/profile", msg="Настройки уведомлений сохранены")


@router.post("/profile/telegram/test")
def test_telegram(chat_id: str = Form(""), user=Depends(current_user)):
    chat_id = chat_id.strip() or (user["tg_chat_id"] or "")
    if not notify.CHAT_RE.match(chat_id):
        return redirect("/profile", msg="Укажите chat id (число) и нажмите «Проверить отправку»")
    ok, info = notify.send(chat_id, "Call QA: проверка уведомлений. Если вы видите это сообщение, всё настроено.")
    return redirect("/profile", msg=("Тестовое сообщение отправлено" if ok else f"Не отправлено: {info}"))
