"""Create the cumulative titles-only Word document for a Drive profile."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


DOCX_NAME = "Titles and Thumbnails.docx"


def _style_font(style, name: str, size: float, color: str = "111111",
                *, bold: bool = False) -> None:
    style.font.name = name
    style._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), name)
    style._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), name)
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor.from_string(color)


def _configure_styles(doc: Document) -> None:
    """Resolve the compact_reference_guide preset into Word styles."""
    normal = doc.styles["Normal"]
    _style_font(normal, "Calibri", 11)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25

    for name, size, color, before, after in (
        ("Heading 1", 16, "2E74B5", 18, 10),
        ("Heading 2", 13, "2E74B5", 14, 7),
        ("Heading 3", 12, "1F4D78", 10, 5),
    ):
        style = doc.styles[name]
        _style_font(style, "Calibri", size, color, bold=True)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True


def normalize_titles(titles: Iterable[str]) -> list[str]:
    """Trim titles and preserve their first-seen order without duplicates."""
    if isinstance(titles, str):
        titles = [titles]
    clean: list[str] = []
    seen: set[str] = set()
    for value in titles:
        title = " ".join(str(value or "").split()).strip()
        key = title.casefold()
        if title and key not in seen:
            clean.append(title)
            seen.add(key)
    return clean


def read_titles(path: Path) -> list[str]:
    """Read the title paragraphs from an existing cumulative document."""
    path = Path(path)
    if not path.is_file():
        return []
    return normalize_titles(p.text for p in Document(path).paragraphs)


def create(titles: Iterable[str], dest: Path) -> Path:
    """Write one plain paragraph per title and no thumbnail images or metadata."""
    clean = normalize_titles(titles)
    if not clean:
        raise ValueError("at least one video title is required")

    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)
    _configure_styles(doc)

    # The owner asked for titles only: no document heading, dates, numbering,
    # job ids, tables, or embedded thumbnail images.
    for title in clean:
        paragraph = doc.add_paragraph(title)
        paragraph.paragraph_format.keep_together = True
        paragraph.paragraph_format.widow_control = True

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dest)
    return dest
