"""Иконки приложения: favicon, manifest. Отдаются без входа: нужны и на странице входа.

Иконки рисует tools/make_icons.py (цвет берётся из палитры tools/palette.py); готовые файлы лежат в static/brand/.
Логотипа в интерфейсе нет: в шапке только название. Чтобы добавить свой, положите PNG в static/brand/logo.png —
он попадёт в шапку PDF и Excel-отчётов.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

DIR = Path(__file__).parent / "static" / "brand"
TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}
ROOT = ("favicon.ico", "favicon.svg", "apple-touch-icon.png", "site.webmanifest", "android-chrome-192x192.png", "android-chrome-512x512.png")  # браузеры ищут их в корне
APP_NAME = "Call QA"
LOGO_PNG = DIR / "logo.png"  # необязательный свой логотип для PDF и Excel (800×155); если файла нет, отчёты без логотипа
HEAD = ('<link rel="icon" href="/favicon.ico" sizes="48x48"><link rel="icon" href="/favicon.svg" type="image/svg+xml">'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/site.webmanifest">'
        f'<meta name="theme-color" content="#141416"><meta name="application-name" content="{APP_NAME}">')
router = APIRouter()


def send(name: str):
    p = DIR / name
    if "/" in name or "\\" in name or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type=TYPES.get(p.suffix, "application/octet-stream"), headers={"Cache-Control": "public, max-age=86400"})


@router.get("/static/brand/{name}")
def brand_file(name: str):
    return send(name)


def _root(name: str):
    router.add_api_route("/" + name, lambda: send(name), methods=["GET"], include_in_schema=False)


for _n in ROOT:
    _root(_n)
