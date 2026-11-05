"""Оценка звонка по чек-листу через LiteLLM (OpenAI-совместимый API). Итоговый балл считает код, не модель."""
import json
import os
import re
from pathlib import Path

import httpx
import jsonschema
import yaml

BASE = Path(__file__).parent
RUBRICS = {p.stem.removeprefix("rubric-"): yaml.safe_load(p.read_text(encoding="utf-8")) for p in (BASE / "rubrics").glob("rubric-*.yaml")}
SCHEMA = json.loads((BASE / "rubrics" / "score.schema.json").read_text(encoding="utf-8"))
LLM_URL = os.environ.get("CALLQA_LLM_URL", "http://127.0.0.1:4000/v1")  # локальный сервер моделей (встроенный Ollama, LiteLLM, vLLM...)
LLM_KEY = os.environ.get("CALLQA_LLM_KEY") or "none"  # Ollama ключ не проверяет
NO_THINK = os.environ.get("CALLQA_LLM_NO_THINK") == "1"  # модели Qwen3: отключить режим размышлений (точнее и быстрее для оценки)
CONFIG = {"llm_model": os.environ.get("CALLQA_LLM_MODEL") or "qwen3:8b", "url": LLM_URL, "key": LLM_KEY}  # задаётся из настроек администратора перед каждым звонком
USAGE = {"in": 0, "out": 0}  # токены текущего звонка (обработка идёт по одному звонку за раз)


def mmss(sec) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _chat(messages: list, max_tokens: int, temperature: float = 0.1) -> str:
    local = CONFIG["url"] == LLM_URL
    if NO_THINK and local and messages:  # мягкий переключатель Qwen3; другим моделям он безвреден
        messages = messages[:-1] + [{**messages[-1], "content": str(messages[-1]["content"]) + " /no_think"}]
    r = httpx.post(f"{CONFIG['url'].rstrip('/')}/chat/completions", timeout=600, headers={"Authorization": f"Bearer {CONFIG['key']}"},
                   json={"model": CONFIG["llm_model"], "messages": messages, "temperature": temperature, "max_tokens": max_tokens})
    r.raise_for_status()
    u = r.json().get("usage") or {}
    USAGE["in"] += u.get("prompt_tokens", 0)
    USAGE["out"] += u.get("completion_tokens", 0)
    text = r.json()["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip() if "<think>" in text else text


def _roles_by_meaning(segments: list, ctx: str):
    """Роль каждой реплики по смыслу (моно без разделения голосов). None — модель не справилась."""
    lines = "\n".join(f"{i + 1}. {s['text']}" for i, s in enumerate(segments))
    prompt = (f"{ctx}\nНиже реплики телефонного разговора по порядку (расшифровка без разделения голосов, реплика — фрагмент речи между паузами; "
              f"подряд идущие реплики могут принадлежать одному человеку). Для КАЖДОЙ реплики укажи, кто говорит.\n\n{lines}\n\n"
              f'Ответь ТОЛЬКО JSON: {{"roles":["employee"|"client", ...]}} — ровно {len(segments)} элементов по порядку.')
    for _ in range(2):
        roles = _extract_json(_chat([{"role": "user", "content": prompt}], 40 + 12 * len(segments), 0)).get("roles")
        if isinstance(roles, list) and len(roles) == len(segments) and set(roles) <= {"employee", "client"}:
            return roles
    return None


def label_speakers(segments: list, stereo: bool, rubric: dict, voices: bool = False) -> list:
    """Расставляет роли employee/client в каждой реплике (поле spk).
    Стерео и моно с разделением голосов (voices): в репликах есть метка голоса/канала ch, модель определяет, кто из двух сотрудник, по началу разговора.
    Обычное моно: роль каждой реплики по смыслу. Если голоса разделены, но модель не смогла выбрать сотрудника, роли ставятся по смыслу, как в обычном моно.
    При сбое роли остаются None — оценка пойдёт по тексту без ролей."""
    if not segments:
        return segments
    stereo = stereo or voices
    who = str(rubric.get("employee_title") or "сотрудник").lower()
    ctx = (f"Телефонный разговор сотрудника компании с клиентом. Сотрудник (employee) — {who}: здоровается и представляется от компании, задаёт уточняющие вопросы, "
           "называет цены, сроки, условия, оформляет заявку. Клиент (client) — звонящий: описывает проблему или потребность, спрашивает, возражает.")
    try:
        if stereo:
            by = {"A": [], "B": []}
            for s in segments:
                if len(by[s["ch"]]) < 12:
                    by[s["ch"]].append(f"[{mmss(s['t'])}] {s['text']}")
            what = "Два голоса в записи (канал A и канал B) — разные участники" if voices else "Запись стерео: канал A и канал B — разные участники"
            prompt = (f"{ctx}\n{what}. По репликам определи, кто из них сотрудник.\n"
                      f"Канал A:\n" + "\n".join(by["A"]) + "\n\nКанал B:\n" + "\n".join(by["B"]) +
                      '\n\nОтветь ТОЛЬКО JSON: {"A":"employee|client","B":"employee|client"} (разные значения).')
            try:
                m = _extract_json(_chat([{"role": "user", "content": prompt}], 200, 0))
                if {m.get("A"), m.get("B")} != {"employee", "client"}:
                    raise ValueError("bad mapping")
                for s in segments:
                    s["spk"] = m[s["ch"]]
            except Exception:
                if not voices:
                    raise
                for s in segments:  # голоса разделены неудачно — возвращаемся к ролям по смыслу
                    s.pop("ch", None)
                roles = _roles_by_meaning(segments, ctx)
                if roles is None:
                    raise ValueError("bad roles")
                for s, r in zip(segments, roles):
                    s["spk"] = r
        else:
            roles = _roles_by_meaning(segments, ctx)
            if roles is None:
                raise ValueError("bad roles")
            for s, r in zip(segments, roles):
                s["spk"] = r
    except Exception:
        for s in segments:
            s.setdefault("spk", None)
    return segments


def build_prompt(rubric: dict, segments: list[dict]) -> list[dict]:
    items = []
    for it in rubric["items"]:
        lv = "; ".join(f"{k} — {v}" for k, v in sorted(it["levels"].items(), reverse=True))
        extra = f" Применимо, если: {it['applicable_when']} (иначе score = null)." if it.get("applicable_when") else ""
        items.append(f"- {it['code']} «{it['title']}»: {it['definition']} Уровни: {lv}.{extra}")
    system = (
        "Ты контролёр качества телефонных звонков. Оцени разговор строго по чек-листу.\n"
        f"Контекст компании:\n{rubric.get('context', '').strip()}\n\nПункты чек-листа:\n" + "\n".join(items) + "\n\n"
        "Правила: транскрипт получен автоматически, возможны ошибки распознавания и разметки ролей. Перед репликой указана роль "
        "(Сотрудник — сотрудник компании, Клиент — звонящий); если роли нет или она явно ошибочна, определяй по смыслу. Не выдумывай: если данных нет, "
        "ставь confidence low. Доказательства — дословные цитаты из транскрипта с меткой времени mm:ss (до 3 на пункт). "
        "Все тексты (reason, summary, topic) пиши только по-русски, без иероглифов и иностранных слов. "
        "Ответь ТОЛЬКО JSON-объектом без пояснений и без markdown, по схеме:\n"
        f'{{"rubric":"{rubric["code"]}","rubric_version":"{rubric["version"]}","items":[{{"code":"...","score":0|1|2|null,'
        '"evidence":[{"t":"mm:ss","speaker":"employee|client","quote":"..."}],"reason":"...","confidence":"low|medium|high"}],'
        '"summary":"кратко о чём звонок и как прошёл","topic":"тема","outcome":"agreed|not_agreed|needs_followup|resolved|escalated|other",'
        '"flags":["conflict|complaint|critical_violation|poor_audio|not_a_client_call"]}\n'
        "Каждый пункт чек-листа должен присутствовать ровно один раз."
    )
    role = {"employee": "Сотрудник: ", "client": "Клиент: "}
    text = "\n".join(f"[{mmss(s['t'])}] {role.get(s.get('spk'), '')}{s['text']}" for s in segments) or "(речь не распознана)"
    return [{"role": "system", "content": system}, {"role": "user", "content": f"Транскрипт звонка:\n{text}"}]


def _extract_json(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])


def finalize(result: dict, rubric: dict) -> dict:
    """Итоговый балл 0–100 по весам; неприменимые пункты (null) исключаются; 0 по critical → флаг."""
    by_code = {i["code"]: i for i in result["items"]}
    got = total = 0.0
    for it in rubric["items"]:
        r = by_code.get(it["code"])
        if r is None:
            raise ValueError(f"нет пункта {it['code']}")
        if r["score"] is None:
            continue
        got += it["weight"] * r["score"] / 2
        total += it["weight"]
        if it.get("critical") and r["score"] == 0 and "critical_violation" not in result["flags"]:
            result["flags"].append("critical_violation")
    result["total"] = round(100 * got / total) if total else None
    return result


def score(rubric: dict, segments: list[dict]) -> dict:
    messages = build_prompt(rubric, segments)
    last_err = None
    for _ in range(2):
        try:
            result = _extract_json(_chat(messages, 3000))
            jsonschema.validate(result, SCHEMA)
            return finalize(result, rubric)
        except Exception as e:  # повторяем один раз, подсказав ошибку
            last_err = e
            messages = messages + [{"role": "user", "content": f"Ответ не прошёл проверку: {str(e)[:300]}. Верни исправленный JSON."}]
    raise RuntimeError(f"модель не вернула корректный JSON: {last_err}")
