"""Выгрузка в Excel (openpyxl) и PDF (reportlab): список звонков и карточка одного звонка.

Библиотеки подключаются при первом обращении: если их нет в окружении службы, приложение запускается, а выгрузка отвечает понятной ошибкой.
Модуль не знает про базу: main.py передаёт готовые словари (см. main.call_row / main.job_export).
"""
import io
import os
from pathlib import Path

from fastapi import HTTPException

FONTS = [  # (обычный, жирный): RHEL-сервер, Debian/Ubuntu, Windows (для локальных проверок)
    ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]
_font_ready = False


def _need(module: str, pkg: str):
    try:
        return __import__(module, fromlist=["x"])
    except ImportError:
        raise HTTPException(503, f"Выгрузка недоступна: в окружении службы не установлен {pkg}")


def safe(v):
    """Ячейка Excel: строка, начинающаяся с = + - @, иначе станет формулой (имена файлов и сотрудников вводят пользователи)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def sc(v) -> str:
    return "н/п" if v is None else str(v)


# ---------- Excel ----------
def _sheet(ws, headers: list, rows: list, widths: list):
    from openpyxl.styles import Alignment, Font, PatternFill
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2A2A2F")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append([safe(v) for v in r])
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions


def xlsx_calls(calls: list, admin: bool) -> bytes:
    openpyxl = _need("openpyxl", "openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Звонки"
    head = ["Дата и время"] + (["Клиент"] if admin else []) + ["Сотрудник", "Файл", "Чек-лист", "Длительность", "Балл", "Балл модели", "Статус", "Флаги", "Тема", "Исход", "Кратко", "Комментарий проверяющего"]
    rows = [[c["date"]] + ([c["org"]] if admin else []) + [c["employee"], c["filename"], c["rubric"], c["duration"], c["total"], c["model_total"], c["status"],
                                                           c["flags"], c["topic"], c["outcome"], c["summary"], c["comment"]] for c in calls]
    _sheet(ws, head, rows, [17] + ([20] if admin else []) + [20, 28, 18, 12, 8, 11, 16, 24, 24, 18, 50, 36])
    _xlsx_logo(ws, f"{ws.cell(1, len(head) + 2).column_letter}1")
    wb.properties.creator = wb.properties.title = "Call QA"
    ws2 = wb.create_sheet("Оценки по пунктам")
    rows2 = [[c["date"], c["employee"], c["filename"], c["rubric"], i["title"], i["weight"], sc(i["model"]), sc(i["human"]) if i["human"] != "" else "", i["reason"]]
             for c in calls for i in c["items"]]
    _sheet(ws2, ["Дата и время", "Сотрудник", "Файл", "Чек-лист", "Пункт", "Вес", "Модель (0–2)", "Человек (0–2)", "Обоснование модели"], rows2, [17, 20, 28, 18, 34, 7, 12, 12, 70])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_job(j: dict) -> bytes:
    openpyxl = _need("openpyxl", "openpyxl")
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Оценка"
    for k, v in j["head"]:
        ws.append([k, safe(v)])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.append([])
    start = ws.max_row + 1
    ws.append(["Пункт", "Вес", "Модель (0–2)", "Человек (0–2)", "Обоснование модели", "Цитаты"])
    for i in j["items"]:
        ws.append([safe(v) for v in (i["title"], i["weight"], sc(i["model"]), sc(i["human"]) if i["human"] != "" else "", i["reason"], "\n".join(i["evidence"]))])
    from openpyxl.styles import Alignment, PatternFill
    for c in ws[start]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2A2A2F")
    for row in ws.iter_rows(min_row=start + 1):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    for col, w in zip("ABCDEF", (36, 8, 14, 14, 60, 60)):
        ws.column_dimensions[col].width = w
    _xlsx_logo(ws, "H1")
    wb.properties.creator = wb.properties.title = "Call QA"
    ws2 = wb.create_sheet("Расшифровка")
    _sheet(ws2, ["Время", "Говорит", "Текст"], [[t, who, text] for t, who, text in j["transcript"]], [9, 16, 110])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- PDF ----------
def _register_fonts() -> tuple:
    global _font_ready
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if not _font_ready:
        cands = ([(os.environ["CALLQA_FONT"], os.environ.get("CALLQA_FONT_BOLD", os.environ["CALLQA_FONT"]))] if os.environ.get("CALLQA_FONT") else []) + FONTS
        found = next(((a, b) for a, b in cands if Path(a).is_file() and Path(b).is_file()), None)
        if not found:
            raise HTTPException(503, "Выгрузка в PDF недоступна: на сервере не найден шрифт с кириллицей (DejaVu Sans)")
        pdfmetrics.registerFont(TTFont("CQ", found[0]))
        pdfmetrics.registerFont(TTFont("CQ-B", found[1]))
        addMapping("CQ", 0, 0, "CQ")
        addMapping("CQ", 1, 0, "CQ-B")
        addMapping("CQ", 0, 1, "CQ")
        addMapping("CQ", 1, 1, "CQ-B")
        _font_ready = True
    return "CQ", "CQ-B"


def _pdf_kit():
    _need("reportlab", "reportlab")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    f, fb = _register_fonts()
    st = {"n": ParagraphStyle("n", fontName=f, fontSize=8.5, leading=11), "s": ParagraphStyle("s", fontName=f, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#5E5E66")),
          "h1": ParagraphStyle("h1", fontName=fb, fontSize=15, leading=19, textColor=colors.HexColor("#141416"), spaceAfter=4),
          "h2": ParagraphStyle("h2", fontName=fb, fontSize=10.5, leading=13, textColor=colors.HexColor("#2A2A2F"), spaceBefore=10, spaceAfter=4),
          "th": ParagraphStyle("th", fontName=fb, fontSize=8, leading=10, textColor=colors.white)}
    return colors, A4, landscape, mm, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, st


def _p(Paragraph, text, style):
    from xml.sax.saxutils import escape
    return Paragraph(escape(str(text if text is not None else "")).replace("\n", "<br/>"), style)


def _table(colors, Table, TableStyle, Paragraph, st, head: list, rows: list, widths: list):
    data = [[_p(Paragraph, h, st["th"]) for h in head]] + [[_p(Paragraph, c, st["n"]) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A2A2F")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")]), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E2E5")),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("CQ", 7)
    canvas.setFillGray(0.4)
    canvas.drawString(doc.leftMargin, 8 * 2.835, "Call QA · автоматическая оценка, возможны ошибки распознавания и разметки")
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 8 * 2.835, f"стр. {doc.page}")
    canvas.restoreState()


def _logo(mm):
    """Логотип в шапке PDF (фирменный комплект); если файла нет, отчёт строится без него."""
    from reportlab.platypus import Image
    try:
        from brand import LOGO_PNG
        if LOGO_PNG.is_file():  # свой логотип необязателен
            im = Image(str(LOGO_PNG), width=52 * mm, height=52 * mm * 155 / 800)  # пропорции файла: 800×155
            im.hAlign = "LEFT"
            return [im]
    except Exception:
        pass
    return []


def _xlsx_logo(ws, anchor: str):
    """Логотип справа от таблицы; нужен Pillow, без него лист остаётся без картинки."""
    try:
        from brand import LOGO_PNG
        from openpyxl.drawing.image import Image
        if not LOGO_PNG.is_file():
            return
        im = Image(str(LOGO_PNG))
        im.width, im.height = 200, 39  # пропорции файла: 800×155
        ws.add_image(im, anchor)
    except Exception:
        pass


def pdf_calls(calls: list, admin: bool, title: str) -> bytes:
    colors, A4, landscape, mm, Paragraph, Doc, Spacer, Table, TableStyle, st = _pdf_kit()
    buf = io.BytesIO()
    doc = Doc(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=15 * mm, title=title)
    head = ["Дата"] + (["Клиент"] if admin else []) + ["Сотрудник", "Файл", "Чек-лист", "Длит.", "Балл", "Статус", "Флаги", "Кратко"]
    w = [24, 32, 36, 26, 14, 13, 24, 30] if not admin else [24, 22, 30, 32, 24, 13, 13, 22, 26]
    rest = doc.width - sum(x * mm for x in w)
    widths = [x * mm for x in w] + [rest]
    rows = [[c["date"]] + ([c["org"]] if admin else []) + [c["employee"], c["filename"], c["rubric"], c["duration"], c["total"], c["status"], c["flags"], c["summary"]] for c in calls]
    story = _logo(mm) + [Spacer(1, 6), _p(Paragraph, title, st["h1"]), _p(Paragraph, f"Звонков: {len(calls)}", st["s"]), Spacer(1, 6),
             _table(colors, Table, TableStyle, Paragraph, st, head, rows, widths)]
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def pdf_job(j: dict) -> bytes:
    colors, A4, landscape, mm, Paragraph, Doc, Spacer, Table, TableStyle, st = _pdf_kit()
    buf = io.BytesIO()
    doc = Doc(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=16 * mm, title=j["title"])
    kv = Table([[_p(Paragraph, k, st["s"]), _p(Paragraph, v, st["n"])] for k, v in j["head"]], colWidths=[38 * mm, doc.width - 38 * mm])
    kv.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    items = [[i["title"], i["weight"], sc(i["model"]), sc(i["human"]) if i["human"] != "" else "—",
              i["reason"] + ("\n" + "\n".join(i["evidence"]) if i["evidence"] else "")] for i in j["items"]]
    tr = [[t, who, text] for t, who, text in j["transcript"]]
    story = _logo(mm) + [Spacer(1, 6), _p(Paragraph, j["title"], st["h1"]), kv, _p(Paragraph, "Оценка по пунктам", st["h2"]),
             _table(colors, Table, TableStyle, Paragraph, st, ["Пункт", "Вес", "Модель", "Человек", "Обоснование и цитаты"], items,
                    [42 * mm, 11 * mm, 18 * mm, 19 * mm, doc.width - 90 * mm]),
             _p(Paragraph, "Расшифровка", st["h2"]),
             _table(colors, Table, TableStyle, Paragraph, st, ["Время", "Говорит", "Текст"], tr, [14 * mm, 24 * mm, doc.width - 38 * mm])]
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
