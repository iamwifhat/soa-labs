"""Збирання звітів ЛР2/ЛР3 на основі DOCX ЛР1: титульна сторінка, стилі SOA та колонтитули залишаються без змін.

Вміст задається списком блоків; посилання [[fig:id]] і [[tab:id]] у тексті замінюються номерами рисунків і таблиць.
"""
import copy
import re
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Emu, Pt
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "ЛР1_SOA_КН-61мп_Заєць.docx"
COVER_LAST_INDEX = 29          # абзац із розривом секції титульної сторінки ЛР1
MAX_IMAGE_HEIGHT = Cm(19.5)
ACCESS_DATE = "31.10.2026"
LINK_COLOR = "244A6B"


# ---------- Блоки вмісту ----------

def H(text): return ("h", text)
def SUB(text): return ("sub", text)
def P(text): return ("p", text)
def PC(text): return ("pc", text)
def PAGE(): return ("page",)
def CODE(text): return ("code", text)
def FIG(ref, path, caption, width=None): return ("fig", ref, path, caption, width)
def TAB(ref, caption, header, rows, widths): return ("tab", ref, caption, header, rows, widths)
def SRC(text, url=None): return ("src", text, url)


def _set_run(run, bold=None, size=14):
    run.font.name = "Times New Roman"
    run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    fonts = run._element.get_or_add_rPr().get_or_add_rFonts()
    for key in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn("w:" + key), "Times New Roman")
    return run


def _breakable_url(url):
    # Як у ЛР1: невидимий пробіл після «/», «-», «_» дозволяє переносити довгі адреси.
    scheme, rest = url.split("://", 1)
    return scheme + "://" + re.sub(r"([/\-_])", "\\1\u200b", rest)


class Report:
    def __init__(self, lab_no: int, title: str, subject: str):
        self.doc = Document(TEMPLATE)
        body = self.doc.element.body
        children = list(body.iterchildren())
        self._table_props = copy.deepcopy(children[36].find(qn("w:tblPr")))
        for element in children[COVER_LAST_INDEX + 1:-1]:
            body.remove(element)
        cover = self.doc.paragraphs[12].runs[0]
        cover.text = cover.text.replace("№1", f"№{lab_no}")
        props = self.doc.core_properties
        props.title, props.subject = title, subject
        props.created = props.modified = datetime.now(timezone.utc).replace(microsecond=0)
        props.revision = 1
        section = self.doc.sections[-1]
        self.text_width = section.page_width - section.left_margin - section.right_margin
        self.numbers: dict[str, int] = {}

    # ---------- Допоміжні ----------

    def _resolve(self, text: str) -> str:
        return re.sub(r"\[\[(fig|tab):([\w-]+)\]\]", lambda m: str(self.numbers[f"{m.group(1)}:{m.group(2)}"]), text)

    def _paragraph(self, style: str, text: str = "", bold=None):
        paragraph = self.doc.add_paragraph(style=style)
        paragraph.paragraph_format.line_spacing = 1.5
        if text:
            # **жирний фрагмент** — для коротких вступних слів у переліках.
            for index, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", self._resolve(text))):
                if chunk:
                    _set_run(paragraph.add_run(chunk), bold=True if index % 2 else bold)
        return paragraph

    def _page_break(self):
        paragraph = self.doc.add_paragraph()
        fmt = paragraph.paragraph_format
        fmt.space_before = fmt.space_after = Pt(0)
        fmt.line_spacing = Pt(1)
        _set_run(paragraph.add_run()).add_break(WD_BREAK.PAGE)

    def _table(self, caption: str, header: list[str], rows: list[list[str]], widths: list[float]):
        self._paragraph("SOA Table Caption", caption).paragraph_format.keep_with_next = True
        table = self.doc.add_table(rows=1 + len(rows), cols=len(header))
        tbl = table._tbl
        tbl.remove(tbl.tblPr)
        tbl.insert(0, copy.deepcopy(self._table_props))
        grid = tbl.tblGrid
        for col, width in zip(grid.findall(qn("w:gridCol")), widths):
            col.set(qn("w:w"), str(int(Cm(width) / 635)))
        for row_index, (row, values) in enumerate(zip(table.rows, [header] + rows)):
            tr_pr = row._tr.get_or_add_trPr()
            tr_pr.append(OxmlElement("w:cantSplit"))
            if row_index == 0:
                head = OxmlElement("w:tblHeader")
                head.set(qn("w:val"), "true")
                tr_pr.append(head)
            for cell, value, width in zip(row.cells, values, widths):
                cell.width = Cm(width)
                tc_pr = cell._tc.get_or_add_tcPr()
                if row_index == 0:
                    v_align = OxmlElement("w:vAlign")
                    v_align.set(qn("w:val"), "center")
                    tc_pr.append(v_align)
                    shade = OxmlElement("w:shd")
                    shade.set(qn("w:fill"), "E6EEF4")
                    tc_pr.append(shade)
                lines = self._resolve(value).split("\n")
                first = cell.paragraphs[0]
                for line_index, line in enumerate(lines):
                    paragraph = first if line_index == 0 else cell.add_paragraph()
                    paragraph.style = self.doc.styles["SOA Table"]
                    fmt = paragraph.paragraph_format
                    fmt.line_spacing, fmt.space_after = 1.5, Pt(0)
                    fmt.keep_with_next = row_index == 0
                    for index, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", line)):
                        if chunk:
                            _set_run(paragraph.add_run(chunk), bold=True if (row_index == 0 or index % 2) else None)
        spacer = self.doc.add_paragraph(style="SOA Compact")
        spacer.paragraph_format.space_after = Pt(3)
        spacer.paragraph_format.line_spacing = Pt(3)

    def _figure(self, path: Path, caption: str, width_cm=None):
        with Image.open(path) as image:
            px_w, px_h = image.size
        width = Cm(width_cm) if width_cm else self.text_width
        height = Emu(int(width * px_h / px_w))
        if height > MAX_IMAGE_HEIGHT:
            height = MAX_IMAGE_HEIGHT
            width = Emu(int(height * px_w / px_h))
        paragraph = self.doc.add_paragraph(style="SOA Body")
        fmt = paragraph.paragraph_format
        fmt.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fmt.first_line_indent = Pt(0)
        fmt.line_spacing = 1
        fmt.space_before, fmt.space_after = Pt(0), Pt(6)
        fmt.keep_with_next = True
        shape = _set_run(paragraph.add_run()).add_picture(str(path), width=width, height=height)
        shape._inline.docPr.set("descr", re.sub(r"^Рисунок \d+\. ", "", caption))
        cap = self._paragraph("SOA Caption", caption)
        cap_fmt = cap.paragraph_format
        cap_fmt.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap_fmt.first_line_indent = cap_fmt.left_indent = cap_fmt.right_indent = Pt(0)
        cap_fmt.space_before, cap_fmt.space_after = Pt(0), Pt(6)
        cap_fmt.keep_together = True

    def _code(self, text: str):
        paragraph = self.doc.add_paragraph(style="SOA Code")
        paragraph.paragraph_format.line_spacing = 1.15
        paragraph.paragraph_format.keep_together = True
        run = _set_run(paragraph.add_run())
        for index, line in enumerate(text.strip("\n").split("\n")):
            if index:
                run.add_break()
            run.add_text(line)

    def _source(self, text: str, url: str | None):
        paragraph = self.doc.add_paragraph(style="SOA Source")
        fmt = paragraph.paragraph_format
        fmt.line_spacing, fmt.space_before, fmt.space_after = 1.5, Pt(0), Pt(3)
        fmt.keep_together = True
        _set_run(paragraph.add_run(text))
        if url:
            _set_run(paragraph.add_run(" URL: "))
            r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
            link = OxmlElement("w:hyperlink")
            link.set(qn("r:id"), r_id)
            run = OxmlElement("w:r")
            r_pr = OxmlElement("w:rPr")
            fonts = OxmlElement("w:rFonts")
            for key in ("ascii", "hAnsi", "eastAsia", "cs"):
                fonts.set(qn("w:" + key), "Times New Roman")
            color = OxmlElement("w:color")
            color.set(qn("w:val"), LINK_COLOR)
            size, size_cs = OxmlElement("w:sz"), OxmlElement("w:szCs")
            size.set(qn("w:val"), "28")
            size_cs.set(qn("w:val"), "28")
            r_pr.extend([fonts, color, size, size_cs])
            text_el = OxmlElement("w:t")
            text_el.text = _breakable_url(url)
            run.extend([r_pr, text_el])
            link.append(run)
            paragraph._p.append(link)
            _set_run(paragraph.add_run(f" (дата звернення: {ACCESS_DATE})."))

    def _drop_orphans(self):
        """Прибирає зображення, колонтитули й посилання видаленого тексту ЛР1, на які більше ніхто не посилається."""
        xml = self.doc.element.xml
        removable = (RT.IMAGE, RT.HEADER, RT.FOOTER, RT.HYPERLINK)
        for r_id, rel in list(self.doc.part.rels.items()):
            if rel.reltype in removable and f'"{r_id}"' not in xml:
                self.doc.part.drop_rel(r_id)

    # ---------- Збирання ----------

    def render(self, blocks: list[tuple], output: Path):
        counters = {"fig": 0, "tab": 0}
        for block in blocks:
            if block[0] in ("fig", "tab"):
                kind = block[0]
                counters[kind] += 1
                self.numbers[f"{kind}:{block[1]}"] = counters[kind]
        for block in blocks:
            kind = block[0]
            if kind == "h":
                self._paragraph("SOA Heading", block[1])
            elif kind == "sub":
                self._paragraph("SOA Subheading", block[1])
            elif kind == "p":
                self._paragraph("SOA Body", block[1])
            elif kind == "pc":
                self._paragraph("SOA Compact", block[1])
            elif kind == "page":
                self._page_break()
            elif kind == "code":
                self._code(block[1])
            elif kind == "fig":
                _, ref, path, caption, width = block
                self._figure(Path(path), f"Рисунок {self.numbers['fig:' + ref]}. {caption}", width)
            elif kind == "tab":
                _, ref, caption, header, rows, widths = block
                self._table(f"Таблиця {self.numbers['tab:' + ref]}. {caption}", header, rows, widths)
            elif kind == "src":
                self._source(block[1], block[2])
        self._drop_orphans()
        self.doc.save(output)
        return counters
