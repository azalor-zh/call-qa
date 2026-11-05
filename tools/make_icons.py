"""Рисует иконки приложения (favicon.svg/.ico, apple-touch, android-chrome) цветом акцента выбранной палитры.

Знак — монограмма «Q» (кольцо с хвостиком, пересекающим его, как у буквы Q) на плашке цвета акцента.
    python tools/make_icons.py [палитра]      # по умолчанию magenta; список палитр — в tools/palette.py
Нужен Pillow (pip install pillow). Результат — в app/static/brand/.
Чтобы поставить свой знак, замените файлы в app/static/brand/ или измените функции svg()/png() ниже.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from palette import PALETTES  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "brand"
# геометрия в координатах 100×100: кольцо (центр, радиус), хвостик (от, до), толщина линии
CX, CY, R = 47.0, 47.0, 22.0
TAIL = ((51.0, 50.0), (73.0, 72.0))
W = 9.0


def svg(accent: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="#{accent}"/>'
            f'<circle cx="{CX}" cy="{CY}" r="{R}" fill="none" stroke="#fff" stroke-width="{W}"/>'
            f'<line x1="{TAIL[0][0]}" y1="{TAIL[0][1]}" x2="{TAIL[1][0]}" y2="{TAIL[1][1]}" stroke="#fff" stroke-width="{W}" stroke-linecap="round"/></svg>\n')


def png(size: int, accent: str) -> Image.Image:
    k = 4  # рисуем в 4× и уменьшаем: гладкие края
    s = size * k
    f = s / 100
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill="#" + accent)
    d.ellipse([(CX - R) * f, (CY - R) * f, (CX + R) * f, (CY + R) * f], outline="#ffffff", width=int(W * f))
    (x1, y1), (x2, y2) = TAIL
    d.line([x1 * f, y1 * f, x2 * f, y2 * f], fill="#ffffff", width=int(W * f))
    for x, y in ((x1, y1), (x2, y2)):  # круглые концы хвостика
        d.ellipse([(x - W / 2) * f, (y - W / 2) * f, (x + W / 2) * f, (y + W / 2) * f], fill="#ffffff")
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "magenta"
    accent = PALETTES[name]["accent"]
    (OUT / "favicon.svg").write_text(svg(accent), encoding="utf-8")
    png(192, accent).save(OUT / "android-chrome-192x192.png")
    png(512, accent).save(OUT / "android-chrome-512x512.png")
    png(180, accent).save(OUT / "apple-touch-icon.png")
    png(256, accent).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("иконки обновлены, цвет акцента #" + accent)
