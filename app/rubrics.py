"""Чек-листы: встроенные шаблоны (файлы rubrics/*.yaml) и собственные чек-листы клиентов (таблица rubrics).

Ключ чек-листа в звонке: «sales», «support» (шаблоны) или «r<id>» (свой). При оценке чек-лист сохраняется в звонок
снимком, поэтому правка или удаление чек-листа не меняет уже оценённые звонки.
"""
import copy
import json
import time

import scoring
from core import q

TEMPLATE_NAMES = {"sales": "Шаблон: Продажи", "support": "Шаблон: Поддержка"}
TEMPLATE_TITLES = {"sales": "Менеджер", "support": "Техспециалист"}
NEUTRAL_CONTEXT = "Контекст компании не задан: оценивай только по определениям пунктов."
LEVEL_KEYS = (2, 1, 0)


def is_custom(key: str) -> bool:
    return key.startswith("r") and key[1:].isdigit()


def _row(key: str):
    return q("SELECT * FROM rubrics WHERE id=?", int(key[1:])).fetchone() if is_custom(key) else None


def template(key: str) -> dict | None:
    t = scoring.RUBRICS.get(key)
    if not t:
        return None
    t = copy.deepcopy(t)
    t["code"] = key
    t["version"] = str(t["version"])
    t["employee_title"] = TEMPLATE_TITLES.get(key, "Сотрудник")
    return t


def custom(key: str) -> dict | None:
    r = _row(key)
    if not r:
        return None
    items = json.loads(r["items"])
    return {"code": key, "version": str(r["version"]), "name": r["name"], "employee_title": r["employee_title"] or "Сотрудник",
            "context": r["context"] or "", "items": items}


def effective(key: str, owner_is_admin: bool) -> dict | None:
    """Чек-лист для оценки. У шаблонов контекст «Компания» показываем только своим: клиентам он не годится."""
    if is_custom(key):
        return custom(key)
    t = template(key)
    if t and not owner_is_admin:
        t["context"] = NEUTRAL_CONTEXT
    return t


def name(key: str) -> str:
    if is_custom(key):
        r = _row(key)
        return r["name"] if r else "Удалённый чек-лист"
    return TEMPLATE_NAMES.get(key, key)


def all_names() -> dict:
    d = dict(TEMPLATE_NAMES)
    for r in q("SELECT id,name FROM rubrics").fetchall():
        d[f"r{r['id']}"] = r["name"]
    return d


def hidden(user) -> set:
    """Шаблоны, которые пользователь удалил у себя (сами файлы шаблонов общие, у других клиентов они остаются)."""
    return {r["key"] for r in q("SELECT key FROM hidden_templates WHERE owner=?", user["id"]).fetchall()}


def hide(user, key: str) -> bool:
    if key not in scoring.RUBRICS:
        return False
    q("INSERT INTO hidden_templates(owner,key) VALUES(?,?) ON CONFLICT DO NOTHING", user["id"], key)
    return True


def restore(user, key: str):
    q("DELETE FROM hidden_templates WHERE owner=? AND key=?", user["id"], key)


def available(user, with_hidden: bool = False) -> list:
    """Что можно выбрать при загрузке: шаблоны (кроме удалённых пользователем) и свои чек-листы.
    with_hidden=True — для фильтров ленты: по удалённому шаблону прошлые звонки остаются."""
    skip = set() if with_hidden else hidden(user)
    out = [(k, TEMPLATE_NAMES[k]) for k in ("sales", "support") if k in scoring.RUBRICS and k not in skip]
    out += [(f"r{r['id']}", r["name"]) for r in q("SELECT id,name FROM rubrics WHERE owner=? ORDER BY name", user["id"]).fetchall()]
    return out


def can_use(key: str, user) -> bool:
    if is_custom(key):
        r = _row(key)
        return bool(r and r["owner"] == user["id"])
    return key in scoring.RUBRICS and key not in hidden(user)


def can_edit(r, user) -> bool:
    return bool(r) and (user["role"] == "admin" or r["owner"] == user["id"])


def parse_form(name_: str, employee_title: str, context: str, titles: list, weights: list, defs: list, l2: list, l1: list, l0: list,
               crit: list, when: list):
    """Проверяет форму редактора; возвращает (items, ошибка)."""
    if not name_.strip() or len(name_) > 80:
        return None, "Название обязательно, до 80 символов"
    items = []
    for i, t in enumerate(titles):
        if not t.strip():
            continue
        try:
            w = int(weights[i])
        except (ValueError, IndexError):
            return None, f"Пункт «{t}»: вес должен быть числом"
        if not 1 <= w <= 100:
            return None, f"Пункт «{t}»: вес от 1 до 100"
        it = {"code": f"i{len(items) + 1}", "title": t.strip()[:120], "weight": w, "definition": (defs[i] or "").strip()[:500],
              "levels": {2: (l2[i] or "").strip()[:300] or "Выполнено полностью.", 1: (l1[i] or "").strip()[:300] or "Выполнено частично.",
                         0: (l0[i] or "").strip()[:300] or "Не выполнено."}}
        if crit[i] == "1":
            it["critical"] = True
        if (when[i] or "").strip():
            it["applicable_when"] = when[i].strip()[:300]
        items.append(it)
    if not items:
        return None, "Добавьте хотя бы один пункт"
    if len(items) > 20:
        return None, "Не больше 20 пунктов"
    return items, ""


def save(user, rid, name_: str, employee_title: str, context: str, items: list) -> str:
    now = time.time()
    if rid is None:
        cur = q("INSERT INTO rubrics(owner,name,employee_title,context,items,version,created,updated) VALUES(?,?,?,?,?,1,?,?) RETURNING id",
                user["id"], name_.strip(), employee_title.strip() or "Сотрудник", context.strip(), json.dumps(items, ensure_ascii=False), now, now)
        return f"r{cur.fetchone()['id']}"
    q("UPDATE rubrics SET name=?, employee_title=?, context=?, items=?, version=version+1, updated=? WHERE id=?",
      name_.strip(), employee_title.strip() or "Сотрудник", context.strip(), json.dumps(items, ensure_ascii=False), now, rid)
    return f"r{rid}"
