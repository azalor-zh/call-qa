"""Смена цветовой палитры интерфейса Call QA одной командой.

Цвета интерфейса (CSS, графики в Python, PDF и Excel) заданы одинаковыми hex-кодами по ролям.
Скрипт заменяет коды палитры --from на коды палитры --to во всех файлах app/.
Свою палитру добавьте в PALETTES (все роли обязательны) и запустите:

    python tools/palette.py --from graphite --to custom --apply
    python tools/make_icons.py custom          # перерисовать иконки

Без --apply только показывает, сколько замен будет.
"""
import argparse
import re
import sys
from pathlib import Path

ROLES = ["dark", "dark2", "accent", "accent_d", "accent_l", "accent_xl", "accent_m", "dark_m", "on_dark", "on_dark2", "on_dark3", "bg", "text", "grey", "line"]
# dark/dark2 — боковая панель и шапки таблиц; accent — кнопки, ссылки, графики; *_l/_xl/_m — светлые заливки акцента;
# bg/text/grey/line — фон страницы, основной и вторичный текст, линии; on_dark* — светлый текст на тёмной панели; dark_m — светлая заливка графика в цвет dark2.
PALETTES = {
    # прежняя палитра (бирюзовый + тёмно-синий)
    "legacy": dict(dark="122549", dark2="243C6B", accent="1FADA2", accent_d="14806F", accent_l="E3F4F2", accent_xl="F5FBFA",
                   accent_m="BFE3DF", dark_m="C9D2E3", on_dark="C9D4EA", on_dark2="9FB0CF", on_dark3="E6ECF7",
                   bg="F9FAFB", text="1B2232", grey="5B6573", line="E1E5EB"),
    # графит + индиго (промежуточная)
    "graphite": dict(dark="1E2330", dark2="313A52", accent="4B63E0", accent_d="3347B8", accent_l="E6EAFC", accent_xl="F4F6FE",
                     accent_m="C3CCF6", dark_m="CDD2E0", on_dark="D4D9E6", on_dark2="9EA6BC", on_dark3="EAEDF5",
                     bg="F9FAFB", text="1B2232", grey="5B6573", line="E1E5EB"),
    # почти чёрный + маджента (по скриншоту сайта-образца; оттенки подобраны на глаз)
    "magenta": dict(dark="141416", dark2="2A2A2F", accent="E5007E", accent_d="B5005F", accent_l="FCE3F0", accent_xl="FEF2F8",
                    accent_m="F5A3CE", dark_m="D6D6DB", on_dark="DCDCE0", on_dark2="9C9CA6", on_dark3="EFEFF1",
                    bg="F4F4F5", text="1A1A1D", grey="5E5E66", line="E2E2E5"),
}

ROOT = Path(__file__).resolve().parent.parent / "app"
EXT = {".css", ".py", ".svg", ".webmanifest", ".html", ".js"}


def convert(src: str, dst: str, apply: bool) -> int:
    a, b = PALETTES[src], PALETTES[dst]
    pairs = {a[r].upper(): b[r].upper() for r in ROLES}
    rx = re.compile('(?:#|fgColor=")(' + "|".join(pairs) + ")(?![0-9A-Fa-f])", re.I)  # только в виде #RRGGBB или fgColor="RRGGBB" (Excel)
    total = 0
    for f in sorted(ROOT.rglob("*")):
        if not f.is_file() or f.suffix not in EXT or "__pycache__" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        n = [0]

        def sub(m):
            n[0] += 1
            return m.group(0).replace(m.group(1), pairs[m.group(1).upper()])
        new = rx.sub(sub, text)
        if n[0]:
            print(f"{f.relative_to(ROOT.parent)}: {n[0]}")
            total += n[0]
            if apply:
                f.write_text(new, encoding="utf-8", newline="\n")
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True, choices=PALETTES)
    ap.add_argument("--to", dest="dst", required=True, choices=PALETTES)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    for name, p in PALETTES.items():
        if set(p) != set(ROLES):
            sys.exit(f"в палитре {name} не хватает ролей")
    n = convert(a.src, a.dst, a.apply)
    print(f"замен: {n}" + ("" if a.apply else " (пробный запуск, добавьте --apply)"))
