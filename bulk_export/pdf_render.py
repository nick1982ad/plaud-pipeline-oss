"""
pdf_render.py — renders markdown summary into a visually polished A4 PDF
using reportlab. Cyrillic support via Calibri TTF from C:\\Windows\\Fonts.

Public API:
    render_md_to_pdf(markdown_text, out_path, header={"title": ..., "subtitle": ..., "footer": ...})
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

_FONTS_REGISTERED = False
_FONT_BASE = "Calibri"
_FONT_BOLD = "Calibri-Bold"
_FONT_ITALIC = "Calibri-Italic"

_WIN_FONTS = Path(r"C:\Windows\Fonts")
_FONT_FILES = {
    _FONT_BASE: _WIN_FONTS / "calibri.ttf",
    _FONT_BOLD: _WIN_FONTS / "calibrib.ttf",
    _FONT_ITALIC: _WIN_FONTS / "calibrii.ttf",
}

_BRAND_COLOR = colors.HexColor("#1f4e79")
_SUBTLE = colors.HexColor("#5a6772")
_RULE_COLOR = colors.HexColor("#c9d2db")


def _register_fonts():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    for name, path in _FONT_FILES.items():
        if not path.exists():
            raise RuntimeError(f"Font file not found: {path}")
        pdfmetrics.registerFont(TTFont(name, str(path)))
    pdfmetrics.registerFontFamily(
        _FONT_BASE,
        normal=_FONT_BASE,
        bold=_FONT_BOLD,
        italic=_FONT_ITALIC,
        boldItalic=_FONT_BOLD,
    )
    _FONTS_REGISTERED = True


def _styles():
    return {
        "title": ParagraphStyle(
            "Title",
            fontName=_FONT_BOLD,
            fontSize=20,
            leading=24,
            textColor=_BRAND_COLOR,
            spaceAfter=4,
            alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle",
            fontName=_FONT_BASE,
            fontSize=10,
            leading=13,
            textColor=_SUBTLE,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "H2",
            fontName=_FONT_BOLD,
            fontSize=13,
            leading=18,
            textColor=_BRAND_COLOR,
            spaceBefore=10,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "Body",
            fontName=_FONT_BASE,
            fontSize=11,
            leading=15,
            textColor=colors.black,
            spaceAfter=4,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            fontName=_FONT_BASE,
            fontSize=11,
            leading=15,
            textColor=colors.black,
            spaceAfter=2,
            leftIndent=0,
        ),
        "footer": ParagraphStyle(
            "Footer",
            fontName=_FONT_ITALIC,
            fontSize=8,
            leading=10,
            textColor=_SUBTLE,
        ),
    }


# ---- Markdown → flowables (simple parser for our fixed schema) -----

_INLINE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_INLINE_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_INLINE_CODE = re.compile(r"`([^`]+)`")


def _inline_md_to_html(text: str) -> str:
    """Convert bold/italic/code markdown inline to reportlab-supported HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _INLINE_BOLD.sub(r"<b>\1</b>", text)
    text = _INLINE_ITALIC.sub(r"<i>\1</i>", text)
    text = _INLINE_CODE.sub(r'<font face="Courier">\1</font>', text)
    return text


def _md_to_flowables(md_text: str, styles: dict) -> list:
    """Parse our fixed schema markdown into reportlab flowables.

    Supports: H2 (## Heading), bullet lists (- item), paragraphs.
    Ignores H1 (we have our own title in header).
    """
    flowables = []
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            flowables.append(Paragraph(_inline_md_to_html(heading), styles["h2"]))
            i += 1
            continue
        if line.startswith("# "):
            heading = line[2:].strip()
            flowables.append(Paragraph(_inline_md_to_html(heading), styles["h2"]))
            i += 1
            continue
        if line.startswith("- ") or line.startswith("* "):
            items = []
            while i < len(lines) and (lines[i].startswith("- ") or lines[i].startswith("* ")):
                item_text = lines[i][2:].strip()
                items.append(
                    ListItem(
                        Paragraph(_inline_md_to_html(item_text), styles["bullet"]),
                        leftIndent=12,
                    )
                )
                i += 1
            flowables.append(
                ListFlowable(
                    items,
                    bulletType="bullet",
                    start="•",
                    leftIndent=14,
                    bulletFontName=_FONT_BASE,
                    bulletFontSize=11,
                    bulletColor=_BRAND_COLOR,
                )
            )
            continue
        # Paragraph (collect until blank line / heading / bullet)
        para_lines = [line]
        i += 1
        while i < len(lines):
            nxt = lines[i].rstrip()
            if not nxt.strip():
                break
            if nxt.startswith(("## ", "# ", "- ", "* ")):
                break
            para_lines.append(nxt)
            i += 1
        text = " ".join(para_lines)
        flowables.append(Paragraph(_inline_md_to_html(text), styles["body"]))
    return flowables


# ---- Public render -------------------------------------------------

def render_md_to_pdf(markdown_text: str, out_path: Path, header: dict | None = None) -> None:
    """
    markdown_text: summary in markdown (## Тема / ## Ключевые тезисы / etc.)
    out_path: absolute path to .pdf to write
    header: {
        "title": "Display title at the top (Paragraph)",
        "subtitle": "Date, duration, source — small grey line",
        "footer": "Generated by ... · timestamp · transcript source",
    }
    """
    _register_fonts()
    styles = _styles()
    header = header or {}

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=header.get("title", "Summary"),
        author="Claude Sonnet 4.6",
    )

    flow = []
    if header.get("title"):
        flow.append(Paragraph(_inline_md_to_html(header["title"]), styles["title"]))
    if header.get("subtitle"):
        flow.append(Paragraph(_inline_md_to_html(header["subtitle"]), styles["subtitle"]))
    flow.append(
        HRFlowable(
            width="100%",
            thickness=0.7,
            color=_RULE_COLOR,
            spaceBefore=2,
            spaceAfter=8,
        )
    )

    flow.extend(_md_to_flowables(markdown_text, styles))

    if header.get("footer"):
        flow.append(Spacer(1, 12))
        flow.append(
            HRFlowable(
                width="100%",
                thickness=0.4,
                color=_RULE_COLOR,
                spaceBefore=4,
                spaceAfter=4,
            )
        )
        flow.append(Paragraph(_inline_md_to_html(header["footer"]), styles["footer"]))

    doc.build(flow)


# ---- Self-test -----------------------------------------------------

if __name__ == "__main__":
    sample_md = """## Тема
Рабочая встреча по обновлению магистратуры УрФУ.

## Ключевые тезисы
- Новый формат программы запускается с сентября 2026.
- Партнёры: Sber, Касперский, Газпромнефть.
- Технологическое ядро — нейроморфные вычисления и embedded AI.
- Бюджет согласован, ставки распределены.

## Договорённости и задачи
- Иванов — подготовить таблицу практик до 25.05.2026.
- Петров — согласовать с индустрией прототип портрета выпускника.
- (не зафиксировано)

## Открытые вопросы
- Кто закрывает кафедру по машинному обучению с 1.09?
- Финансирование лабораторного оборудования.

## Участники
- Иванов, Петров, Сидорова (РОП), представители Sber
"""
    out = Path("_self_test_pdf_render.pdf")
    render_md_to_pdf(
        sample_md,
        out,
        header={
            "title": "Обновление магистратуры УрФУ — пилотный запуск 2026",
            "subtitle": "20.05.2026 · 45 мин · transcript: Plaud",
            "footer": "Сгенерировано Claude Sonnet 4.6 · "
                      + datetime.now().strftime("%Y-%m-%d %H:%M")
                      + " · transcript: Plaud",
        },
    )
    print(f"Wrote {out.resolve()}")
