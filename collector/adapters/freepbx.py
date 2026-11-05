"""Адаптер FreePBX 16/17: журнал звонков через GraphQL (модуль API) + скачивание записей по SSH.

Проверено 2026-11-05 на FreePBX 17.0.28:
- токен: POST <base>/token (client_credentials, scope gql:cdr), живёт 3600 с;
- GraphQL: <base>/gql, запрос fetchAllCdrs(first, after, orderby: date, startDate, endDate);
  даты — только 'ГГГГ-ММ-ДД' и только вместе (startDate без endDate возвращает пустой список);
- в самом API аудио нет, файл лежит в <monitor_dir>/ГГГГ/ММ/ДД/<recordingfile>, берём по SSH (scp);
- номер сотрудника надо брать из channel/dstchannel (PJSIP/152-0000…), а не из src/dst:
  у исходящих внешних src — CID транка.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from .base import Adapter, AudioNotReady, Call, read_kv

SAFE_PATH = re.compile(r"^[\w.\-/]+$")
FILE_DATE = re.compile(r"-(\d{4})(\d{2})(\d{2})-\d{6}-")
PAGE = 500


def direction_of(recordingfile: str, channel: str, dstchannel: str, ext_re) -> str:
    """Направление по префиксу файла FreePBX (out-/in-/internal-/exten-/q-/g-), запасной путь — по каналам."""
    name = recordingfile.rsplit("/", 1)[-1]
    if name.startswith("out-"):
        return "out"
    if name.startswith("internal-"):
        return "internal"
    if name.startswith(("in-", "q-", "g-", "rg-", "exten-")):
        return "in"
    a, b = bool(ext_re.match(channel or "")), bool(ext_re.match(dstchannel or ""))
    return "internal" if a and b else ("out" if a else "in")


def pick_extension(channel: str, dstchannel: str, ext_re, wanted: set[str]) -> str:
    """Добавочные в каналах звонка; берём первый из списка `wanted`, а без списка — первый найденный."""
    found = []
    for ch in (channel, dstchannel):
        m = ext_re.match(ch or "")
        if m:
            found.append(m.group(1))
    for e in found:
        if e in wanted:
            return e
    return "" if wanted else (found[0] if found else "")


class FreePBX(Adapter):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.api = cfg["api_base"].rstrip("/")
        self.creds = read_kv(cfg["api_credentials_file"])
        self.scope = cfg.get("scope", "gql:cdr")
        self.ext_re = re.compile(cfg.get("ext_regex", r"^(?:PJSIP|SIP|IAX2)/(\d{2,6})-"))
        self.monitor_dir = cfg.get("monitor_dir", "/var/spool/asterisk/monitor").rstrip("/")
        self.settle_s = int(cfg.get("settle_seconds", 120))
        self.wanted = {str(e) for e in cfg.get("_extensions", [])}
        self._tok, self._tok_exp = "", 0.0

    # ---------- API ----------
    def _token(self) -> str:
        if self._tok and time.time() < self._tok_exp - 60:
            return self._tok
        data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.creds["client_id"],
                                       "client_secret": self.creds["client_secret"], "scope": self.scope}).encode()
        with urllib.request.urlopen(urllib.request.Request(f"{self.api}/token", data=data), timeout=30) as r:
            j = json.load(r)
        self._tok = j["access_token"]
        self._tok_exp = time.time() + int(j.get("expires_in", 3600))
        return self._tok

    def _gql(self, query: str) -> dict:
        req = urllib.request.Request(f"{self.api}/gql", data=json.dumps({"query": query}).encode(),
                                     headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.load(r)
        if j.get("errors"):
            raise RuntimeError("GraphQL: " + "; ".join(e.get("message", "?") for e in j["errors"]))
        return j["data"]

    def list_calls(self, days_back: int) -> list[Call]:
        start = (date.today() - timedelta(days=max(0, days_back - 1))).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
        out, after = [], 0
        while True:
            q = ('{ fetchAllCdrs(first:%d, after:%d, orderby:date, startDate:"%s", endDate:"%s"){ cdrs{ uniqueid linkedid calldate src dst '
                 'channel dstchannel billsec disposition recordingfile } } }' % (PAGE, after, start, end))
            rows = (self._gql(q)["fetchAllCdrs"] or {}).get("cdrs") or []
            for r in rows:
                if r.get("recordingfile"):
                    out.append(self._to_call(r))
            if len(rows) < PAGE:
                return out
            after += PAGE

    def _to_call(self, r: dict) -> Call:
        d = direction_of(r["recordingfile"], r.get("channel", ""), r.get("dstchannel", ""), self.ext_re)
        ext = pick_extension(r.get("channel", ""), r.get("dstchannel", ""), self.ext_re, self.wanted)
        client = (r.get("dst") if d == "out" else r.get("src")) or ""
        return Call(call_id=r["recordingfile"], started_at=r["calldate"], direction=d, extension=ext, client_number=client,
                    duration_s=int(r.get("billsec") or 0), audio_ref=r["recordingfile"], answered=(r.get("disposition") == "ANSWERED"),
                    link_id=r.get("linkedid") or "", source_call_id=r.get("uniqueid") or "")

    # ---------- аудио ----------
    def remote_path(self, call: Call) -> str:
        f = call.audio_ref
        if not SAFE_PATH.match(f) or ".." in f:
            raise ValueError(f"недопустимое имя записи: {f!r}")
        if "/" in f:
            return f"{self.monitor_dir}/{f}" if not f.startswith("/") else f
        m = FILE_DATE.search(f)
        y, mo, dd = m.groups() if m else call.started_at[:10].split("-")
        return f"{self.monitor_dir}/{y}/{mo}/{dd}/{f}"

    def _ssh_base(self, port_flag: str = "-p") -> list[str]:
        c = self.cfg
        opts = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
        if c.get("ssh_key"):
            opts += ["-i", c["ssh_key"]]
        if c.get("ssh_port"):
            opts += [port_flag, str(c["ssh_port"])]
        return opts

    def fetch_audio(self, call: Call, dst: Path) -> None:
        path = self.remote_path(call)
        target = f'{self.cfg["ssh_user"]}@{self.cfg["ssh_host"]}'
        p = subprocess.run(["ssh", *self._ssh_base(), target, f"stat -c '%s %Y' '{path}' && date +%s"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise AudioNotReady(f"файла нет на АТС или нет доступа: {path}")
        try:
            size, mtime, now = (int(x) for x in p.stdout.split())
        except ValueError:
            raise AudioNotReady("не удалось прочитать размер файла")
        if size == 0 or now - mtime < self.settle_s:
            raise AudioNotReady("запись ещё дописывается")
        p = subprocess.run(["scp", "-q", *self._ssh_base("-P"), f"{target}:{path}", str(dst)], capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise AudioNotReady("scp: " + p.stderr.strip()[:200])
