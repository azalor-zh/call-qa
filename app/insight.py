"""Аналитика поверх готовых звонков: качество модели относительно проверок людей и поиск по расшифровкам.
Чистые функции без обращения к базе: строки звонков и вспомогательные функции приходят из main.py."""
import html
import json
import re


# ---------- качество модели ----------
def _mean(v):
    return sum(v) / len(v) if v else None


def _corr(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    da, db = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db) ** 0.5 if da and db else None


def quality(rows, rubric_of, human_total, name_of) -> dict:
    """Сравнение оценок модели с проверкой человека по звонкам, где проверка есть.
    rubric_of(r) — чек-лист звонка, human_total(rub, scores) — итог по оценкам человека, name_of(ключ чек-листа) — его название."""
    done = [r for r in rows if r["status"] == "done" and r["result"]]
    rev_rows = [r for r in done if r["review"]]
    items, by_crit, by_conf, by_model = [], {}, {}, {}
    tm, th = [], []
    roles_edits = roles_segs = 0
    for r in done:
        segs = json.loads(r["transcript"] or "[]")
        roles_segs += len(segs)
        roles_edits += r["role_edits"] or 0
    for r in rev_rows:
        res, rev, rub = json.loads(r["result"]), json.loads(r["review"])["scores"], rubric_of(r)
        titles = {i["code"]: i["title"] for i in rub["items"]}
        for it in res["items"]:
            if it["code"] not in rev:
                continue
            m, h = it["score"], rev[it["code"]]
            row = (m, h)
            items.append(row)
            by_crit.setdefault((name_of(r["rubric"]), titles.get(it["code"], it["code"])), []).append(row)
            by_conf.setdefault(it.get("confidence") or "medium", []).append(row)
            key = (r["llm_model"] or "—", r["asr_engine"] or "—")
            by_model.setdefault(key, {"pairs": [], "calls": set(), "asr": [], "llm": [], "tok": []})
            g = by_model[key]
            g["pairs"].append(row)
            if r["id"] not in g["calls"]:
                g["calls"].add(r["id"])
                g["asr"].append(r["asr_sec"] or 0)
                g["llm"].append(r["llm_sec"] or 0)
                g["tok"].append((r["tok_in"] or 0) + (r["tok_out"] or 0))
        ht = human_total(rub, rev)
        if ht is not None and res.get("total") is not None:
            tm.append(res["total"])
            th.append(ht)

    def stat(pairs):
        both = [(m, h) for m, h in pairs if m is not None and h is not None]
        return {"n": len(pairs), "agree": 100 * sum(1 for m, h in pairs if m == h) / len(pairs) if pairs else None,
                "mae": _mean([abs(m - h) for m, h in both]), "bias": _mean([m - h for m, h in both]),
                "na_mismatch": sum(1 for m, h in pairs if (m is None) != (h is None))}

    conf = {k: stat(v) for k, v in by_conf.items()}
    crit = sorted(({"rubric": k[0], "title": k[1], **stat(v)} for k, v in by_crit.items()), key=lambda x: (x["agree"], -x["n"]))
    models = [{"llm": k[0], "asr": k[1], "calls": len(g["calls"]), **stat(g["pairs"]), "asr_sec": _mean(g["asr"]), "llm_sec": _mean(g["llm"]), "tok": _mean(g["tok"])}
              for k, g in by_model.items()]
    matrix = {(m, h): 0 for m in (0, 1, 2, None) for h in (0, 1, 2, None)}
    for m, h in items:
        matrix[(m, h)] += 1
    low_conf = sum(1 for r in done for it in json.loads(r["result"])["items"] if it.get("confidence") == "low")
    all_items = sum(len(json.loads(r["result"])["items"]) for r in done)
    return {"done": len(done), "reviewed": len(rev_rows), "items": stat(items), "crit": crit, "conf": conf, "models": models, "matrix": matrix,
            "total_mae": _mean([abs(a - b) for a, b in zip(tm, th)]), "total_bias": _mean([a - b for a, b in zip(tm, th)]), "total_corr": _corr(tm, th),
            "total_n": len(tm), "low_conf_share": 100 * low_conf / all_items if all_items else None,
            "roles_edit_share": 100 * roles_edits / roles_segs if roles_segs else None, "roles_edits": roles_edits, "roles_segs": roles_segs}


# ---------- поиск по расшифровкам ----------
def norm(s: str) -> str:
    return (s or "").casefold().replace("ё", "е")


def words_of(query: str) -> list:
    return [norm(w) for w in re.split(r"\s+", query.strip()) if w][:8]


def highlight(text: str, words: list) -> str:
    """Экранированный текст с подсветкой слов запроса (е и ё считаются одной буквой)."""
    if not words:
        return html.escape(text)
    pat = re.compile("|".join(re.escape(w).replace("е", "[её]") for w in sorted(set(words), key=len, reverse=True)), re.IGNORECASE)
    out, pos = [], 0
    for m in pat.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append("<mark>" + html.escape(m.group(0)) + "</mark>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def search(rows, query: str, per_call: int = 6, max_calls: int = 100) -> tuple:
    """Звонки, где все слова запроса встречаются в одной реплике (или в теме/кратком содержании оценки).
    Возвращает ([(звонок, [(номер реплики, реплика)...], совпало_в_кратком)], слова запроса). Свежие звонки первыми."""
    words = words_of(query)
    if not words:
        return [], words
    hits = []
    for r in rows:
        segs = json.loads(r["transcript"] or "[]")
        found = [(i, s) for i, s in enumerate(segs) if all(w in norm(s["text"]) for w in words)]
        meta = False
        if r["result"]:
            res = json.loads(r["result"])
            blob = norm(f'{res.get("topic", "")} {res.get("summary", "")}')
            meta = all(w in blob for w in words)
        if found or meta:
            hits.append((r, found[:per_call], meta))
            if len(hits) >= max_calls:
                break
    return hits, words
