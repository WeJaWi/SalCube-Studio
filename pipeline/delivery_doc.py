"""Create the title + thumbnail DOCX delivered with research-profile videos."""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


def _font(run, *, size: float, bold: bool = False,
          color: str = "111111") -> None:
    run.font.name = "Arial"
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Arial")
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), "Arial")
    run.font.size = Pt(size)
    run.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def create(title: str, thumbnail_path: Path, dest: Path) -> Path:
    """Build a simple one-page editorial delivery sheet.

    Style system: compact_reference_guide with a restrained editorial-cover
    opening. Named override: the single thumbnail is centered at 6.5 inches.
    """
    if not thumbnail_path.is_file():
        raise FileNotFoundError(f"thumbnail is missing: {thumbnail_path.name}")
    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.75)
    section.bottom_margin = Inches(0.75)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)

    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Arial")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Arial")
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25

    kicker = doc.add_paragraph()
    kicker.alignment = WD_ALIGN_PARAGRAPH.CENTER
    kicker.paragraph_format.space_before = Pt(8)
    kicker.paragraph_format.space_after = Pt(10)
    _font(kicker.add_run("VIDEO DELIVERY"), size=10, bold=True,
          color="9A6A16")

    heading = doc.add_paragraph()
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    heading.paragraph_format.space_before = Pt(0)
    heading.paragraph_format.space_after = Pt(20)
    heading.paragraph_format.keep_with_next = True
    _font(heading.add_run(title.strip()), size=24, bold=True,
          color="202A36")

    image = doc.add_paragraph()
    image.alignment = WD_ALIGN_PARAGRAPH.CENTER
    image.paragraph_format.space_before = Pt(0)
    image.paragraph_format.space_after = Pt(0)
    image.add_run().add_picture(str(thumbnail_path), width=Inches(6.5))

    dest.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dest)
    return dest
