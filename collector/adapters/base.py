"""Общий контракт адаптеров АТС (docs/10, раздел 6).

Адаптер умеет две вещи: перечислить звонки за период и скачать аудио одного звонка.
Всё остальное (фильтры, очередь, дедупликация, отправка в Call QA) делает ядро collector.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Call:
    call_id: str            # уникальный в АТС (для FreePBX — имя файла записи; не меняется между опросами)
    started_at: str         # 'ГГГГ-ММ-ДД ЧЧ:ММ:СС', время АТС
    direction: str          # in | out | internal
    extension: str          # добавочный сотрудника
    client_number: str      # номер собеседника
    duration_s: int         # длительность разговора, сек
    audio_ref: str          # то, по чему адаптер скачает файл (имя файла, URL и т.п.)
    answered: bool = True
    queue: str = ""
    link_id: str = ""
    source_call_id: str = ""  # исходный идентификатор АТС (uniqueid) — для трассировки
    channels: str = "unknown"  # stereo | mono | unknown


class Adapter(ABC):
    def __init__(self, cfg: dict):
        self.cfg = cfg

    @abstractmethod
    def list_calls(self, days_back: int) -> list[Call]:
        """Звонки за последние days_back суток (включая сегодня), любые, без фильтров."""

    @abstractmethod
    def fetch_audio(self, call: Call, dst: Path) -> None:
        """Скачивает запись в dst. Бросает AudioNotReady, если файл ещё пишется или не найден."""


class AudioNotReady(Exception):
    """Запись ещё не готова (дописывается) или не найдена; повторим в следующий цикл."""


def read_kv(path: str) -> dict:
    """Читает файл вида key=value (секреты хранятся только в файлах, не в конфиге)."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out
