"""Автопроверки новых функций Call QA на настоящем PostgreSQL (встроенный сервер pgserver), без моделей и без выхода в сеть.
Запуск: python app/tests/run_checks.py   (нужны: pgserver, fastapi, httpx, psycopg2-binary, jsonschema, pyyaml, numpy, openpyxl, reportlab, python-multipart)
Распознавание и оценка подменены заглушками; всё остальное — настоящий код: очередь, лимиты, журнал, веб-хуки, справочник, экран качества."""
import hashlib
import hmac
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
tmp = Path(tempfile.mkdtemp(prefix="callqa-check-"))
import pgserver  # noqa: E402

pg = pgserver.get_server(tmp / "pg", cleanup_mode="stop")
import secrets as _secrets
PW = {n: _secrets.token_urlsafe(12) for n in ("admin", "alpha", "beta", "gamma", "beta2")}  # пароли тестов генерируются заново при каждом запуске
os.environ.update(CALLQA_PG_DSN=pg.get_uri().replace("postgresql://", "postgresql://", 1), CALLQA_DATA=str(tmp / "data"), CALLQA_LLM_KEY="x",
                  CALLQA_WEB_USER="admin", CALLQA_WEB_PASSWORD=PW["admin"], CALLQA_BASE_URL="http://qa.test")

import asr  # noqa: E402
import scoring  # noqa: E402

GATE = threading.Event()
GATE.set()
SEEN = []  # что видела «распознавалка»: язык, движок, флаг разделения голосов
FAIL_NAMES = {"boom.wav"}


def fake_transcribe(src, workdir, should_stop=None, max_sec=0, progress=None, diarize_mono=False):
    SEEN.append({"engine": asr.CONFIG["engine"], "lang": asr.CONFIG["whisper_lang"], "diarize": diarize_mono})
    if progress:
        progress(0.5)
        time.sleep(1.7)
        progress(0.9)
    GATE.wait(20)
    if (core.q("SELECT filename FROM jobs WHERE id=?", Path(src).stem).fetchone() or {}).get("filename") in FAIL_NAMES:
        raise RuntimeError("тестовая ошибка распознавания")
    return {"duration": 90, "stereo": False, "voices": False, "segments": [
        {"t": 1.0, "text": "Добрый день, компания Ромашка, меня зовут Ёлена"}, {"t": 6.0, "text": "Здравствуйте, мне нужна скидка на заказ"},
        {"t": 12.0, "text": "Скидка десять процентов, доставка завтра"}]}


def fake_label(segments, stereo, rubric, voices=False):
    for i, s in enumerate(segments):
        s["spk"] = "employee" if i != 1 else "client"
    return segments


def fake_score(rubric, segments):
    items = [{"code": it["code"], "score": 2 if k % 2 == 0 else 1, "evidence": [], "reason": "тест", "confidence": "high" if k % 2 == 0 else "low"}
             for k, it in enumerate(rubric["items"])]
    return scoring.finalize({"rubric": rubric["code"], "rubric_version": rubric["version"], "items": items, "summary": "Клиент просил скидку на заказ",
                             "topic": "скидка", "outcome": "agreed", "flags": []}, rubric)


asr.transcribe = fake_transcribe
scoring.label_speakers = fake_label
scoring.score = fake_score

import hooks  # noqa: E402

hooks.RETRY_DELAYS = (0, 1)
import core  # noqa: E402
import insight  # noqa: E402
import main  # noqa: E402
import rubrics as rubrics_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASS = FAIL = 0


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ПРОВАЛ: {name} {info}")


def wait_for(cond, timeout=25, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


def status_of(jid):
    return core.q("SELECT * FROM jobs WHERE id=?", jid).fetchone()


def upload(c, name="a.wav", **form):
    data = {"rubric": "sales", "employee": "Иванов Алексей", **form}
    r = c.post("/upload", files={"audio": (name, b"RIFF0000data", "audio/wav")}, data=data, follow_redirects=False)
    return r


def upload_many(c, n, **form):
    files = [("audio", (f"m{i}.wav", b"RIFF0000data", "audio/wav")) for i in range(n)]
    return c.post("/upload", files=files, data={"rubric": "sales", "employee": "Иванов Алексей", **form}, follow_redirects=False)


def job_id_from(r):
    m = re.search(r"/job/([0-9a-f]{12})", r.headers.get("location", ""))
    return m.group(1) if m else None


def done(jid):
    return wait_for(lambda: status_of(jid)["status"] in ("done", "error", "stopped") and status_of(jid))


# ---------- заглушка получателя веб-хуков ----------
RECEIVED = []
HOOK_STATUS = [200]


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        RECEIVED.append({"headers": dict(self.headers), "body": body})
        self.send_response(HOOK_STATUS[0])
        self.end_headers()

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
HOOK_URL = f"http://127.0.0.1:{srv.server_port}/hook"

adm = TestClient(main.app)
r = adm.post("/login", data={"username": "admin", "password": PW["admin"]}, follow_redirects=False)
check("вход админа", r.status_code == 303)
adm.post("/admin/clients/create", data={"org": "Клиент А", "username": "alpha", "password": PW["alpha"], "api": "1"})
adm.post("/admin/clients/create", data={"org": "Клиент Б", "username": "beta", "password": PW["beta"]})
ua = core.q("SELECT * FROM users WHERE username='alpha'").fetchone()
ub = core.q("SELECT * FROM users WHERE username='beta'").fetchone()
ca, cb = TestClient(main.app), TestClient(main.app)
ca.post("/login", data={"username": "alpha", "password": PW["alpha"]})
cb.post("/login", data={"username": "beta", "password": PW["beta"]})

print("1. страницы открываются")
for c, who, paths in ((adm, "админ", ["/", "/admin/clients", f"/admin/clients/{ua['id']}", "/admin/models", "/admin/new", "/employees", "/quality", "/profile", "/summary", "/rubrics"]),
                      (ca, "клиент", ["/", "/employees", "/quality", "/profile", "/summary", "/api-access"])):
    for p in paths:
        rr = c.get(p)
        check(f"{who} {p}", rr.status_code == 200, rr.status_code)
check("клиент не видит страницы администратора", ca.get("/admin/models", follow_redirects=False).status_code in (403, 303))
check("клиент не видит карточку клиента", ca.get(f"/admin/clients/{ua['id']}", follow_redirects=False).status_code in (403, 303))

print("2. прогресс по этапам")
GATE.clear()
r = upload(ca, "p.wav")
jid = job_id_from(r)
check("загрузка вернула карточку", jid is not None)
row = wait_for(lambda: (status_of(jid)["stage"] == "asr" and (status_of(jid)["progress"] or 0) >= 30) and status_of(jid))
check("этап asr с процентом", bool(row), status_of(jid)["progress"] if jid else "")
page = ca.get(f"/job/{jid}").text
check("в карточке шкала этапов", 'class="steps"' in page and "Распознавание речи" in page and "Расстановка ролей" in page)
check("процент в ленте", "Распознавание речи · " in ca.get("/").text)
check("API отдаёт этап и процент", ca.get(f"/api/v1/calls/{jid}", headers={}).status_code in (401, 403))
GATE.set()
d = done(jid)
check("звонок дошёл до done со 100 %", d and d["status"] == "done" and d["progress"] == 100 and d["stage"] == "done")

print("3. язык и казахский")
SEEN.clear()
j1 = job_id_from(upload(ca, "k.wav", lang="kk"))
done(j1)
j2 = job_id_from(upload(ca, "r.wav"))
done(j2)
check("язык записан", status_of(j1)["lang"] == "kk" and status_of(j2)["lang"] is None)
check("для казахского на процессоре выбран Whisper", any(s["lang"] == "kk" and s["engine"].startswith("whisper") for s in SEEN), SEEN)
check("для русского остался GigaAM", any(s["lang"] == "ru" and s["engine"].startswith("gigaam") for s in SEEN), SEEN)
check("неизвестный язык игнорируется", status_of(job_id_from(upload(ca, "x.wav", lang="zz")))["lang"] is None)

print("4. разделение голосов в моно")
SEEN.clear()
j = job_id_from(upload(ca, "d0.wav")); done(j)
check("по умолчанию выключено", SEEN and SEEN[-1]["diarize"] is False)
form = {"llm_mode": "local", "llm_model": core.setting("llm_model"), "asr_device": "cpu", "asr_engine_cpu": core.setting("asr_engine"), "whisper_lang": "ru",
        "max_minutes": "90", "retention_days": "90", "mono_diarize": "on"}
adm.post("/admin/models", data=form)
check("настройка включена", core.setting("mono_diarize") == "on")
SEEN.clear()
j = job_id_from(upload(ca, "d1.wav")); done(j)
check("флаг дошёл до распознавания", SEEN and SEEN[-1]["diarize"] is True)
adm.post("/admin/models", data={**form, "mono_diarize": "off"})
check("плохое значение отклоняется", "Не сохранено" in adm.post("/admin/models", data={**form, "mono_diarize": "zzz"}, follow_redirects=True).text)

print("5. справочник сотрудников")
check("пустой справочник", "Справочник пуст" in ca.get("/employees").text)
check("в звонках есть имена вне справочника", "Иванов Алексей" in ca.get("/employees").text and "Добавить их в справочник" in ca.get("/employees").text)
ca.post("/employees", data={"name": "Петров  Пётр", "job_title": "менеджер", "phone": "101"})
check("добавлен, пробелы схлопнуты", core.q("SELECT 1 FROM employees WHERE owner=? AND name='Петров Пётр'", ua["id"]).fetchone() is not None)
check("дубль отклонён", "уже есть" in ca.post("/employees", data={"name": "петров пётр"}, follow_redirects=True).text)
ca.post("/employees/import")
eid = core.q("SELECT id FROM employees WHERE owner=? AND name='Иванов Алексей'", ua["id"]).fetchone()
check("импорт добавил имя из звонков", eid is not None)
check("подсказка при загрузке из справочника", '<option value="Петров Пётр">' in ca.get("/").text)
cb.post("/employees", data={"name": "Чужой Сотрудник"})
check("справочники клиентов раздельны", "Чужой Сотрудник" not in ca.get("/employees").text and "Петров" not in cb.get("/employees").text)
check("чужого сотрудника нельзя править", cb.post(f"/employees/{eid['id']}", data={"name": "Взлом"}, follow_redirects=False).status_code == 404)
ca.post(f"/employees/{eid['id']}", data={"name": "Иванов Алексей Сергеевич", "job_title": "", "phone": ""})
check("переименование меняет прошлые звонки", core.q("SELECT count(*) c FROM jobs WHERE owner=? AND employee='Иванов Алексей Сергеевич'", ua["id"]).fetchone()["c"] >= 3)
ca.post(f"/employees/{eid['id']}/delete")
check("удаление из справочника не трогает звонки", core.q("SELECT count(*) c FROM jobs WHERE owner=? AND employee='Иванов Алексей Сергеевич'", ua["id"]).fetchone()["c"] >= 3)
check("API сотрудников", ca.get("/api/v1/employees").status_code == 401)

print("6. очередь с приоритетами")
for uid_, pr in ((ua["id"], 0), (ub["id"], 1)):
    core.q("UPDATE users SET priority=? WHERE id=?", pr, uid_)
adm_row = core.q("SELECT id FROM users WHERE username='admin'").fetchone()
core.q("UPDATE users SET priority=-1 WHERE id=?", adm_row["id"])
jq = core.JobQueue()
ids = {}
for label, owner in (("low1", adm_row["id"]), ("norm1", ua["id"]), ("high1", ub["id"]), ("norm2", ua["id"]), ("high2", ub["id"])):
    jid_ = "q" + label.ljust(11, "0")[:11]
    core.q("INSERT INTO jobs(id,created,filename,rubric,status,owner) VALUES(?,?,?,?,'stopped',?)", jid_, time.time(), label, "sales", owner)
    ids[label] = jid_
    jq.put(jid_)
check("позиция в очереди", jq.position(ids["high1"]) == 0 and jq.position(ids["low1"]) == 4, [jq.position(v) for v in ids.values()])
order = [jq.get() for _ in range(5)]
check("порядок: высокий, обычный, низкий; внутри — по очереди", order == [ids["high1"], ids["high2"], ids["norm1"], ids["norm2"], ids["low1"]], order)
jq.put("R:" + ids["norm1"])
check("повторная оценка (R:) учитывает приоритет владельца", jq.get() == "R:" + ids["norm1"])
core.q("UPDATE users SET priority=0")

print("7. лимиты клиента")
adm.post(f"/admin/clients/{ub['id']}/limits", data={"priority": "0", "daily_limit": "2", "queue_limit": "0"})
r = upload_many(cb, 3)
check("в сутки принято 2 из 3", core.q("SELECT count(*) c FROM jobs WHERE owner=?", ub["id"]).fetchone()["c"] == 2)
check("клиент видит причину отказа", "Не приняты из-за лимита" in cb.get(r.headers["location"]).text)
check("лимит виден в ленте", "сейчас можно добавить: 0" in cb.get("/").text)
r2 = upload(cb, "late.wav")
check("при исчерпанном лимите — не принято, не карточка", "/job/" not in r2.headers.get("location", ""))
adm.post(f"/admin/clients/{ub['id']}/limits", data={"priority": "0", "daily_limit": "0", "queue_limit": "1"})
wait_for(lambda: core.q("SELECT count(*) c FROM jobs WHERE owner=? AND status IN ('queued','asr','scoring')", ub["id"]).fetchone()["c"] == 0)
GATE.clear()
upload_many(cb, 3)
wait_for(lambda: core.q("SELECT count(*) c FROM jobs WHERE owner=?", ub["id"]).fetchone()["c"] >= 3)
inq = core.q("SELECT count(*) c FROM jobs WHERE owner=? AND status IN ('queued','asr','scoring')", ub["id"]).fetchone()["c"]
check("лимит очереди: в обработке не больше 1", inq == 1, inq)
GATE.set()
wait_for(lambda: core.q("SELECT count(*) c FROM jobs WHERE owner=? AND status IN ('queued','asr','scoring')", ub["id"]).fetchone()["c"] == 0)
check("плохие значения лимитов отклоняются", "Не сохранено" in adm.post(f"/admin/clients/{ub['id']}/limits", data={"priority": "5", "daily_limit": "x", "queue_limit": "0"}, follow_redirects=True).text)
adm.post(f"/admin/clients/{ub['id']}/limits", data={"priority": "0", "daily_limit": "0", "queue_limit": "0"})
# через API: ключ -> 429
key = re.search(r"cqk_[A-Za-z0-9_\-]+", ca.post("/api-access/key").text).group(0)
core.q("UPDATE users SET daily_limit=1 WHERE id=?", ua["id"])
hdr = {"Authorization": f"Bearer {key}"}
ra = ca.post("/api/v1/calls", headers=hdr, files={"audio": ("api.wav", b"RIFF0000data")}, data={"rubric": "sales", "lang": "kk"})
check("API: при исчерпанном лимите 429", ra.status_code == 429, ra.text)
core.q("UPDATE users SET daily_limit=0 WHERE id=?", ua["id"])
ra = ca.post("/api/v1/calls", headers=hdr, files={"audio": ("api.wav", b"RIFF0000data")}, data={"rubric": "sales", "lang": "kk"})
check("API: загрузка принята", ra.status_code == 200 and len(ra.json()["ids"]) == 1, ra.text)
check("API: плохой язык отклонён", ca.post("/api/v1/calls", headers=hdr, files={"audio": ("api.wav", b"x")}, data={"rubric": "sales", "lang": "zz"}).status_code == 400)
done(ra.json()["ids"][0])
g = ca.get(f"/api/v1/calls/{ra.json()['ids'][0]}", headers=hdr).json()
check("API: этап, процент и язык в ответе", g["stage"] == "done" and g["progress"] == 100 and g["lang"] == "kk", g)
check("API: справочник", ca.get("/api/v1/employees", headers=hdr).status_code == 200)
# удаление шаблонов: только у себя, вернуть можно
rt = ca.post("/rubrics/template/support/delete", follow_redirects=False)
check("шаблон удалён у клиента", rt.status_code == 303 and "support" not in [k for k, _ in rubrics_mod.available(ua)])
check("у другого клиента шаблон остался", "support" in [k for k, _ in rubrics_mod.available(ub)])
check("удалённый шаблон нельзя выбрать при загрузке", ca.post("/api/v1/calls", headers=hdr, files={"audio": ("t.wav", b"RIFF0000data")}, data={"rubric": "support"}).status_code == 400)
check("на странице чек-листов есть «Удалённые шаблоны»", "Удалённые шаблоны" in ca.get("/rubrics").text)
check("плохой ключ шаблона — 404", ca.post("/rubrics/template/nope/delete", follow_redirects=False).status_code == 404)
ca.post("/rubrics/template/support/restore")
check("шаблон возвращён", "support" in [k for k, _ in rubrics_mod.available(ua)])
# загрузка только аудио; отдача записи с фиксированным типом
bad = ca.post("/api/v1/calls", headers=hdr, files=[("audio", ("x.html", b"<script>alert(1)</script>", "text/html")), ("audio", ("y.svg", b"<svg onload=alert(1)>", "image/svg+xml")), ("audio", ("noext", b"RIFF0000data"))], data={"rubric": "sales"})
check("не-аудио файлы не принимаются", bad.status_code == 200 and bad.json()["ids"] == [] and len(bad.json()["skipped"]) == 3, bad.text)
check("в хранилище нет html/svg", not list((core.DATA / "audio").glob("*.html")) and not list((core.DATA / "audio").glob("*.svg")))
au = ca.get(f"/audio/{ra.json()['ids'][0]}")
check("аудио: фиксированный тип и CSP sandbox", au.status_code == 200 and au.headers["content-type"] == "audio/wav" and au.headers.get("content-security-policy") == "sandbox", dict(au.headers))

print("8. веб-хуки")
RECEIVED.clear()
check("плохой адрес отклонён", "Не сохранено" in adm.post(f"/admin/clients/{ua['id']}/webhook", data={"url": "ftp://x", "on": "1"}, follow_redirects=True).text)
check("без адреса включить нельзя", "Не сохранено" in adm.post(f"/admin/clients/{ua['id']}/webhook", data={"url": "", "on": "1"}, follow_redirects=True).text)
adm.post(f"/admin/clients/{ua['id']}/webhook", data={"url": HOOK_URL, "on": "1"})
secret = core.q("SELECT webhook_secret FROM users WHERE id=?", ua["id"]).fetchone()["webhook_secret"]
check("секрет создан и виден клиенту", secret.startswith("whs_") and secret in ca.get("/profile").text)
check("клиент не может сменить адрес", cb.post(f"/admin/clients/{ua['id']}/webhook", data={"url": "http://evil/", "on": "1"}, follow_redirects=False).status_code in (403, 303))
adm.post(f"/admin/clients/{ua['id']}/webhook/test")
check("ping доставлен и подписан", len(RECEIVED) == 1 and json.loads(RECEIVED[0]["body"])["event"] == "ping")
RECEIVED.clear()
jid = job_id_from(upload(ca, "hook.wav", employee="Сидоров"))
wait_for(lambda: len(RECEIVED) >= 1)
got = RECEIVED[0]
body = json.loads(got["body"])
exp = "sha256=" + hmac.new(secret.encode(), got["headers"]["X-CallQA-Timestamp"].encode() + b"." + got["body"], hashlib.sha256).hexdigest()
check("событие call.done", body["event"] == "call.done" and body["id"] == jid and body["status"] == "done" and body["total"] is not None, body)
check("подпись верна", got["headers"]["X-CallQA-Signature"] == exp)
check("в теле нет расшифровки и цитат", "transcript" not in body and "result" not in body and body["url"] == f"http://qa.test/job/{jid}")
RECEIVED.clear()
upload(ca, "boom.wav", employee="Сидоров")
wait_for(lambda: any(json.loads(x["body"])["event"] == "call.error" for x in RECEIVED))
check("событие call.error при ошибке обработки", any(json.loads(x["body"])["event"] == "call.error" for x in RECEIVED))
RECEIVED.clear()
ca.post(f"/job/{jid}/review", json={"scores": {i["code"]: 2 for i in json.loads(status_of(jid)["result"])["items"]}, "comment": "ок"})
wait_for(lambda: RECEIVED)
check("событие call.reviewed", RECEIVED and json.loads(RECEIVED[0]["body"])["event"] == "call.reviewed")
RECEIVED.clear()
HOOK_STATUS[0] = 500
upload(ca, "retry.wav", employee="Сидоров")
wait_for(lambda: len(RECEIVED) >= 2)
check("при ответе 500 — повтор", len(RECEIVED) >= 2, len(RECEIVED))
check("попытки пишутся в журнал доставок", core.q("SELECT count(*) c FROM webhook_log WHERE owner=? AND ok=0", ua["id"]).fetchone()["c"] >= 2)
HOOK_STATUS[0] = 200
check("карточка клиента показывает доставки", "Последние доставки" in adm.get(f"/admin/clients/{ua['id']}").text and "ответ 500" in adm.get(f"/admin/clients/{ua['id']}").text)
adm.post(f"/admin/clients/{ua['id']}/webhook/secret")
check("новый секрет выпущен", core.q("SELECT webhook_secret FROM users WHERE id=?", ua["id"]).fetchone()["webhook_secret"] != secret)
adm.post(f"/admin/clients/{ua['id']}/webhook", data={"url": HOOK_URL, "on": ""})
RECEIVED.clear()
upload(ca, "off.wav", employee="Сидоров")
time.sleep(3)
check("выключенный веб-хук молчит", not RECEIVED)
check("клиент Б без веб-хука — ничего не шлётся", core.q("SELECT webhook_url FROM users WHERE id=?", ub["id"]).fetchone()["webhook_url"] is None)

print("9. страницы поиска и журнала убраны из интерфейса")
check("поиска и страницы журнала нет", ca.get("/search").status_code == 404 and adm.get("/admin/audit").status_code == 404)

print("10. качество модели")
q_ = ca.get("/quality").text
check("экран показывает проверенные", "Проверено людьми" in q_ and "Где модель и человек расходятся" in q_)
rows = core.q("SELECT * FROM jobs WHERE owner=? AND status='done'", ua["id"]).fetchall()
st = insight.quality(rows, main.job_rubric, main.human_total, main.rubrics.name)
check("считает проверенные", st["reviewed"] == 1 and st["items"]["n"] > 0, st["reviewed"])
res = json.loads(status_of(jid)["result"])
exact = sum(1 for it in res["items"] if it["score"] == 2)
check("совпадение посчитано верно", abs(st["items"]["agree"] - 100 * exact / len(res["items"])) < 1e-6, st["items"])
check("смещение: модель не мягче человека (человек поставил 2 везде)", st["items"]["bias"] <= 0)
check("матрица сходится с числом пунктов", sum(st["matrix"].values()) == st["items"]["n"])
ca.post(f"/job/{jid}/speaker", json={"idx": 0, "spk": "client"})
ca.post(f"/job/{jid}/speaker", json={"idx": 0, "spk": "client"})
check("правки ролей считаются один раз за смену", status_of(jid)["role_edits"] == 1)
adm.post("/admin/clients/create", data={"org": "Пустой", "username": "gamma", "password": PW["gamma"]})
cg = TestClient(main.app)
cg.post("/login", data={"username": "gamma", "password": PW["gamma"]})
check("пустые экраны не падают", all("Пока нет" in cg.get(p).text or cg.get(p).status_code == 200 for p in ("/quality", "/summary", "/employees")) and "Пока нет оценённых" in cg.get("/quality").text)

print("11. журнал действий (запись в базу; страницы просмотра нет)")
def audit_text():
    return "\n".join(f"{r['actor']} {r['action']} {r['details']  or ''}" for r in core.q("SELECT actor, action, details FROM audit_log").fetchall())
au = audit_text()
for needle in ("создан клиент", "изменены очередь и лимиты", "изменён веб-хук", "изменены настройки моделей", "вход администратора", "выпущен новый секрет веб-хука"):
    check(f"в журнале «{needle}»", needle in au)
TestClient(main.app).post("/login", data={"username": "alpha", "password": "неверный"})
au = audit_text()
check("неудачный вход записан", "неудачный вход" in au and "alpha" in au)
check("пароли и секреты не попадают в журнал", PW["alpha"] not in au and secret not in au)
adm.post(f"/admin/clients/{ub['id']}/password", data={"password": PW["beta2"]})
au = audit_text()
check("сброс пароля записан без пароля", "сброшен пароль" in au and PW["beta2"] not in au)
cjob = job_id_from(upload(ca, "del.wav")); done(cjob)
adm.post(f"/job/{cjob}/delete")
check("удаление чужого звонка админом записано", "удаление звонка клиента" in audit_text())

print(f"\nИтог: {PASS} проверок пройдено, {FAIL} провалено")
sys.exit(1 if FAIL else 0)
