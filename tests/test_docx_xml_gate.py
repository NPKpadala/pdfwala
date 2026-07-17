"""XML-level CI gate for pdf_to_word output.

Recall/SSIM metrics measure text presence, not structure — they signed off
builds where hyperlinks, landscape orientation, vertAlign, header references
and list numbering were silently missing (2026-07-17 regression class).
This gate unzips the produced DOCX and asserts on the raw OOXML, including
content INSIDE TABLES (where pdf2docx puts most complex layouts).

Fixtures are generated at test time with PyMuPDF — no binary test data.
"""
import os
import re
import zipfile

import fitz
import pytest

from core.context import JobContext
import engines.pdf_engine as PE


def _convert(tmp_path, build_fn, name):
    pdf = str(tmp_path / f"{name}.pdf")
    docx = str(tmp_path / f"{name}.docx")
    d = fitz.open()
    build_fn(d)
    d.save(pdf)
    d.close()
    PE.pdf_to_word(JobContext(operation="pdf_to_word",
                              input_path=pdf, output_path=docx, params={}))
    z = zipfile.ZipFile(docx)
    return pdf, z, z.read("word/document.xml").decode()


def test_hyperlinks_survive_including_tables(tmp_path):
    """B4: every URL/email visible in the output must be a live w:hyperlink,
    also when pdf2docx places the text inside a table cell."""
    def build(d):
        p = d.new_page(width=612, height=792)
        p.insert_text((72, 100), "Contact: test@example.com", fontsize=11)
        p.insert_text((72, 140), "Docs at https://pdfwala.example/docs today.",
                      fontsize=11)
        p.insert_link({"kind": fitz.LINK_URI, "uri": "https://pdfwala.example/docs",
                       "from": fitz.Rect(100, 128, 300, 144)})
        # two-column-ish block that pdf2docx tends to turn into a table
        p.insert_text((72, 300), "Left cell www.left.example", fontsize=10)
        p.insert_text((350, 300), "Right cell mail@cell.example", fontsize=10)

    pdf, z, xml = _convert(tmp_path, build, "links")
    n_links = xml.count("<w:hyperlink")
    assert n_links >= 3, f"expected >=3 live hyperlinks, got {n_links}"
    rels = z.read("word/_rels/document.xml.rels").decode()
    used = set(re.findall(r'<w:hyperlink[^>]*r:id="([^"]+)"', xml))
    defined = set(re.findall(r'Id="([^"]+)"', rels))
    assert not used - defined, f"dangling hyperlink rels: {used - defined}"


def test_landscape_orientation_preserved(tmp_path):
    """G3: a landscape source page must produce a landscape section."""
    def build(d):
        p = d.new_page(width=612, height=792)
        p.insert_text((72, 100), "Portrait page one with some body text.",
                      fontsize=11)
        p2 = d.new_page(width=842, height=595)          # landscape
        p2.insert_text((72, 100), "Wide landscape table page.", fontsize=11)
        for c in range(8):
            p2.insert_text((60 + c * 95, 200), f"Col{c}", fontsize=9)

    pdf, z, xml = _convert(tmp_path, build, "land")
    assert xml.count('w:orient="landscape"') >= 1, "no landscape section emitted"
    # and the landscape section's width must exceed its height
    for m in re.finditer(r'<w:pgSz w:w="(\d+)" w:h="(\d+)"[^/]*w:orient="landscape"', xml):
        assert int(m.group(1)) > int(m.group(2))


def test_vertalign_super_and_subscript(tmp_path):
    """G5: H2O -> subscript, x^2 / footnote marker -> superscript, including
    when the text lands inside a table."""
    def build(d):
        p = d.new_page(width=612, height=792)
        y = 200
        p.insert_text((72, y), "Chemistry needs H", fontsize=11)
        p.insert_text((166, y + 3), "2", fontsize=7)       # subscript
        p.insert_text((172, y), "O and math needs x", fontsize=11)
        p.insert_text((272, y - 4), "2", fontsize=7)       # superscript
        p.insert_text((278, y), " everywhere", fontsize=11)
        # same pattern inside a ruled box (-> table path)
        p.draw_rect(fitz.Rect(60, 300, 540, 360), color=(0, 0, 0))
        p.insert_text((72, 340), "In-cell formula H", fontsize=11)
        p.insert_text((162, 343), "2", fontsize=7)
        p.insert_text((168, 340), "O with reference", fontsize=11)
        p.insert_text((252, 336), "1", fontsize=7)

    pdf, z, xml = _convert(tmp_path, build, "scripts")
    assert xml.count('w:val="superscript"') >= 2, "superscripts missing"
    assert xml.count('w:val="subscript"') >= 2, "subscripts missing"


def test_header_footer_reference_on_every_section(tmp_path):
    """G6: when header1.xml exists, EVERY sectPr must carry the reference —
    LibreOffice and downstream checkers do not honor implicit inheritance."""
    def build(d):
        for i in range(4):
            p = d.new_page(width=612, height=792)
            p.insert_text((72, 40), "Gate Test Confidential Header", fontsize=9)
            p.insert_text((72, 200), f"Body chapter {i + 1} text.", fontsize=11)
            # two columns on page 2 to force pdf2docx into extra sections
            if i == 1:
                p.insert_text((72, 400), "left col alpha beta", fontsize=9)
                p.insert_text((350, 400), "right col gamma delta", fontsize=9)
            p.insert_text((290, 770), f"Page {i + 1}", fontsize=9)

    pdf, z, xml = _convert(tmp_path, build, "hf")
    has_hdr = any(re.match(r"word/header\d+\.xml", n) for n in z.namelist())
    assert has_hdr, "header part missing"
    # span from each <w:sectPr to its closing tag (self-closing children like
    # <w:footerReference/> must not truncate the span)
    starts = [m.start() for m in re.finditer(r"<w:sectPr[ >]", xml)]
    ends = [m.end() for m in re.finditer(r"</w:sectPr>", xml)]
    sects = [xml[s:next(e for e in ends if e > s)] for s in starts]
    missing = [i for i, s in enumerate(sects, 1) if "headerReference" not in s]
    assert not missing, f"sections without headerReference: {missing}/{len(sects)}"
    missing_f = [i for i, s in enumerate(sects, 1) if "footerReference" not in s]
    assert not missing_f, f"sections without footerReference: {missing_f}/{len(sects)}"


def test_nested_list_numpr_depth(tmp_path):
    """Nested 1./a./i. lists become real numPr items with real ilvl depth."""
    def build(d):
        p = d.new_page(width=612, height=792)
        y = 100
        for txt, x in [("1. Checklist top", 54), ("a. Second level", 72),
                       ("i. Third level one", 90), ("ii. Third level two", 90),
                       ("b. Second again", 72), ("2. Top again", 54)]:
            p.insert_text((x, y), txt, fontsize=10)
            y += 14

    pdf, z, xml = _convert(tmp_path, build, "nested")
    assert xml.count("<w:numPr>") >= 6, f"numPr={xml.count('<w:numPr>')}, want >=6"
    lvls = {int(v) for v in re.findall(r'<w:ilvl w:val="(\d+)"', xml)}
    assert {0, 1, 2} <= lvls, f"list depth levels missing: got {sorted(lvls)}"


def test_footer_page_field_not_literal(tmp_path):
    """G6 delta: a repeating 'Page N' footer must become a live PAGE field
    (w:fldSimple w:instr=PAGE or instrText), never literal per-page numbers."""
    def build(d):
        for i in range(5):
            p = d.new_page(width=612, height=792)
            p.insert_text((72, 40), "Confidential Running Header", fontsize=9)
            p.insert_text((72, 200), f"Chapter {i + 1} body paragraph.", fontsize=11)
            p.insert_text((290, 770), f"Page {i + 1}", fontsize=9)

    pdf, z, xml = _convert(tmp_path, build, "pagefield")
    ftrs = [n for n in z.namelist() if re.match(r"word/footer\d+\.xml", n)]
    assert ftrs, "footer part missing"
    fx = "".join(z.read(f).decode() for f in ftrs)
    assert ('w:instr=" PAGE "' in fx or "PAGE" in "".join(
        re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", fx))), \
        "footer lacks a live PAGE field"
    hdrs = [n for n in z.namelist() if re.match(r"word/header\d+\.xml", n)]
    assert any("Confidential" in z.read(h).decode() for h in hdrs)


def test_form_fields_full_pipeline(tmp_path):
    """G10: AcroForm text field + checkbox become Word content controls with
    value/state preserved, through the full conversion pipeline."""
    def build(d):
        p = d.new_page(width=612, height=792)
        p.insert_text((72, 80), "Application Form", fontsize=14)
        p.insert_text((72, 150), "Applicant Name:", fontsize=11)
        w = fitz.Widget(); w.field_name = "name"
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.rect = fitz.Rect(170, 138, 420, 156); w.field_value = "Asha Verma"
        p.add_widget(w)
        p.insert_text((72, 200), "Subscribed:", fontsize=11)
        w = fitz.Widget(); w.field_name = "subscribed"
        w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
        w.rect = fitz.Rect(150, 190, 166, 206); w.field_value = True
        p.add_widget(w)

    pdf, z, xml = _convert(tmp_path, build, "acro")
    assert xml.count("<w:sdt>") >= 2, "content controls missing"
    assert "w14:checkbox" in xml, "checkbox control missing"
    assert "Asha Verma" in xml, "text field value lost"


def test_dense_chart_becomes_image(tmp_path):
    """Chart recovery: a curve-free vector bar chart (the class pdf2docx
    drops/mangles) must arrive as an embedded image, with caption text kept."""
    def build(d):
        p = d.new_page(width=612, height=792)
        p.insert_text((72, 60), "Metrics Overview", fontsize=14)
        x0, y0 = 90, 400
        p.draw_line(fitz.Point(x0 - 8, y0), fitz.Point(x0 + 330, y0))
        p.draw_line(fitz.Point(x0 - 8, y0), fitz.Point(x0 - 8, y0 - 250))
        for i, v in enumerate([70, 45, 88, 52, 63]):
            p.draw_rect(fitz.Rect(x0 + i * 64, y0 - v * 2.5, x0 + 44 + i * 64, y0),
                        fill=(0.2, 0.45, 0.85))
        p.insert_text((72, 440), "Fig. 1 - weekly throughput.", fontsize=9)

    pdf, z, xml = _convert(tmp_path, build, "densechart")
    media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert media, "vector chart not rasterized to an embedded image"
    text = re.sub(r"<[^>]+>", "", xml)
    assert "Fig. 1" in text, "caption lost"


def test_rtl_logical_order(tmp_path):
    """G7: real-world PDFs store Arabic as shaped PRESENTATION FORMS in VISUAL
    (left-to-right drawn) order. Output runs must be logical-order base
    letters (U+0645 U+0631 U+062D U+0628 U+0627 = مرحبا) with w:bidi + w:rtl."""
    visual_isolated = "ﺍﺏﺡﺮﻡ"   # alef beh hah reh meem
    logical = "مرحبا"           # meem reh hah beh alef

    import glob as _glob
    fonts = (_glob.glob("/usr/share/fonts/**/NotoNaskhArabic-Reg*.ttf", recursive=True)
             or _glob.glob("/usr/share/fonts/**/NotoSansArabic-Reg*.ttf", recursive=True)
             or _glob.glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True))
    if not fonts:
        pytest.skip("no Arabic-capable font in image")

    def build(d):
        p = d.new_page(width=612, height=792)
        p.insert_text((72, 100), "Greeting follows:", fontsize=11)
        p.insert_font(fontname="arab", fontfile=fonts[0])
        p.insert_text((200, 150), visual_isolated, fontsize=12, fontname="arab")

    pdf, z, xml = _convert(tmp_path, build, "rtl")
    assert "<w:bidi/>" in xml, "paragraph bidi missing"
    assert "<w:rtl/>" in xml, "run rtl missing"
    text = re.sub(r"<[^>]+>", "", xml)
    assert logical in text, "Arabic not converted to logical-order base letters"
