"""
engines/office_engine.py — PDFWala Enterprise V12.0
All Word / Excel / PowerPoint processing.
Registered to Pipeline via @register("operation_name").
"""

import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from config import Config
from core.context import JobContext
from core.exceptions import (
    ProcessingError, ValidationError, UnsupportedOperation
)
from core.pipeline import register
from utils.helpers import format_file_size
from utils.office_utils import coerce_cell_value, coerce_cell_for_csv

log = logging.getLogger("pdfwala.engines.office")

# ── Library flags ─────────────────────────────────────────────────────────────
try:
    from docx import Document as DocxDocument
    from docx.shared import Inches, Pt
    DOCX_OK = True
except ImportError:
    DOCX_OK = False

try:
    from openpyxl import load_workbook, Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    OPENPYXL_OK = True
except ImportError:
    OPENPYXL_OK = False

try:
    from pptx import Presentation
    from pptx.util import Inches as PptxInches, Pt as PptxPt
    PPTX_OK = True
except ImportError:
    PPTX_OK = False

try:
    import msoffcrypto
    MSOFFCRYPTO_OK = True
except ImportError:
    MSOFFCRYPTO_OK = False

try:
    import fitz
    FITZ_OK = True
except ImportError:
    FITZ_OK = False

try:
    import pytesseract
    from pytesseract import Output as TesseractOutput
    from PIL import Image
    TESSERACT_OK = True
except ImportError:
    TESSERACT_OK = False

try:
    from openpyxl.drawing.image import Image as XlImage
    XLIMAGE_OK = True
except ImportError:
    XLIMAGE_OK = False


# ── Internal: LibreOffice subprocess ─────────────────────────────────────────

def _libre(input_path: str, fmt: str, out_dir: str,
           timeout: int = None) -> str | None:
    """Run LibreOffice headless conversion. Returns output path or None."""
    if fmt not in Config.LIBRE_ALLOWED_FMTS:
        raise ValidationError(f"LibreOffice format '{fmt}' not in allowlist")
    timeout = timeout or Config.SUBPROCESS_TIMEOUT
    try:
        result = subprocess.run(
            [Config.LIBREOFFICE, "--headless", "--convert-to", fmt,
             "--outdir", out_dir, input_path],
            capture_output=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.error(f"LibreOffice rc={result.returncode}: "
                      f"{result.stderr.decode()[:300]}")
            return None
        base    = Path(input_path).stem
        pattern = os.path.join(out_dir, f"{base}.{fmt}")
        if os.path.exists(pattern):
            return pattern
        matches = list(Path(out_dir).glob(f"*.{fmt}"))
        return str(matches[0]) if matches else None
    except subprocess.TimeoutExpired:
        log.error("LibreOffice timed out")
        return None
    except Exception as ex:
        log.error(f"LibreOffice exception: {ex}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# WORD TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@register("word_to_pdf")
def word_to_pdf(ctx: JobContext) -> dict:
    out_dir   = tempfile.mkdtemp()
    converted = None
    try:
        converted = _libre(ctx.input_path, "pdf", out_dir)
        if not converted or not os.path.exists(converted):
            raise ProcessingError("LibreOffice Word→PDF conversion failed")
        os.replace(converted, ctx.output_path)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return {}


@register("word_to_txt")
def word_to_txt(ctx: JobContext) -> dict:
    if DOCX_OK and ctx.input_path.endswith(".docx"):
        doc  = DocxDocument(ctx.input_path)
        text = "\n".join(p.text for p in doc.paragraphs)
        with open(ctx.output_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return {"characters": len(text)}
    # .doc fallback via LibreOffice
    out_dir = tempfile.mkdtemp()
    try:
        result = _libre(ctx.input_path, "txt", out_dir)
        if not result:
            raise ProcessingError("LibreOffice Word→TXT failed")
        os.replace(result, ctx.output_path)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return {}


@register("word_to_html")
def word_to_html(ctx: JobContext) -> dict:
    out_dir = tempfile.mkdtemp()
    try:
        result = _libre(ctx.input_path, "html", out_dir)
        if not result:
            raise ProcessingError("LibreOffice Word→HTML failed")
        os.replace(result, ctx.output_path)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return {}


@register("word_to_json")
def word_to_json(ctx: JobContext) -> dict:
    if not DOCX_OK:
        raise UnsupportedOperation("word_to_json", "python-docx")
    doc  = DocxDocument(ctx.input_path)
    data = {
        "paragraphs": [p.text for p in doc.paragraphs],
        "tables": [
            [[cell.text for cell in row.cells] for row in table.rows]
            for table in doc.tables
        ],
    }
    with open(ctx.output_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return {"paragraphs": len(data["paragraphs"]), "tables": len(data["tables"])}


@register("word_to_excel")
def word_to_excel(ctx: JobContext) -> dict:
    if not (DOCX_OK and OPENPYXL_OK):
        raise UnsupportedOperation("word_to_excel", "python-docx + openpyxl")
    doc    = DocxDocument(ctx.input_path)
    wb     = Workbook()
    wb.remove(wb.active)
    n_tabs = len(doc.tables)
    for ti, table in enumerate(doc.tables):
        ws = wb.create_sheet(f"Table_{ti + 1}")
        for ri, row in enumerate(table.rows):
            for ci, cell in enumerate(row.cells):
                co = ws.cell(ri + 1, ci + 1, value=cell.text)
                if ri == 0:
                    co.font = Font(bold=True)
    ws_text = wb.create_sheet("Document_Text")
    ws_text.append(["Line", "Style", "Text"])
    for cell in ws_text[1]:
        cell.font = Font(bold=True)
    pr = 2
    for p in doc.paragraphs:
        if p.text.strip():
            ws_text.cell(pr, 1, pr - 1)
            ws_text.cell(pr, 2, p.style.name)
            ws_text.cell(pr, 3, p.text)
            pr += 1
    wb.save(ctx.output_path)
    return {"tables_found": n_tabs}


@register("word_to_ppt")
def word_to_ppt(ctx: JobContext) -> dict:
    if not (DOCX_OK and PPTX_OK):
        raise UnsupportedOperation("word_to_ppt", "python-docx + python-pptx")
    doc  = DocxDocument(ctx.input_path)
    prs  = Presentation()
    prs.slide_width  = PptxInches(10)
    prs.slide_height = PptxInches(7.5)
    layout = prs.slide_layouts[1]
    paras  = [p for p in doc.paragraphs if p.text.strip()]
    if not paras:
        prs.slides.add_slide(prs.slide_layouts[6])
    else:
        slide = prs.slides.add_slide(layout)
        if slide.shapes.title:
            slide.shapes.title.text = Path(ctx.input_path).stem
        if len(slide.placeholders) > 1:
            tf = slide.placeholders[1].text_frame
            tf.clear()
            for p in paras:
                tf.add_paragraph().text = p.text
    prs.save(ctx.output_path)
    return {}


@register("word_to_jpg")
def word_to_jpg(ctx: JobContext) -> dict:
    if not FITZ_OK:
        raise UnsupportedOperation("word_to_jpg", "PyMuPDF")
    out_dir  = tempfile.mkdtemp()
    pdf_path = None
    try:
        pdf_path = _libre(ctx.input_path, "pdf", out_dir)
        if not pdf_path:
            raise ProcessingError("LibreOffice Word→PDF failed")
        doc = fitz.open(pdf_path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=150)
                zf.writestr(f"page_{i + 1:04d}.jpg", pix.tobytes("jpeg"))
        doc.close()
        with open(ctx.output_path, "wb") as fh:
            fh.write(buf.getvalue())
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return {}


@register("word_to_png")
def word_to_png(ctx: JobContext) -> dict:
    if not FITZ_OK:
        raise UnsupportedOperation("word_to_png", "PyMuPDF")
    out_dir  = tempfile.mkdtemp()
    pdf_path = None
    try:
        pdf_path = _libre(ctx.input_path, "pdf", out_dir)
        if not pdf_path:
            raise ProcessingError("LibreOffice Word→PDF failed")
        doc = fitz.open(pdf_path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=150)
                zf.writestr(f"page_{i + 1:04d}.png", pix.tobytes("png"))
        doc.close()
        with open(ctx.output_path, "wb") as fh:
            fh.write(buf.getvalue())
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return {}


@register("edit_word")
def edit_word(ctx: JobContext) -> dict:
    if not DOCX_OK:
        raise UnsupportedOperation("edit_word", "python-docx")
    find_text    = ctx.params.get("find_text", "")
    replace_text = ctx.params.get("replace_text", "")
    if not find_text:
        raise ValidationError("find_text required")
    doc   = DocxDocument(ctx.input_path)
    count = 0
    for para in doc.paragraphs:
        for run in para.runs:
            if find_text in run.text:
                count += run.text.count(find_text)
                run.text = run.text.replace(find_text, replace_text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for run in para.runs:
                        if find_text in run.text:
                            count += run.text.count(find_text)
                            run.text = run.text.replace(find_text, replace_text)
    doc.save(ctx.output_path)
    return {"replacements": count}


@register("compress_word")
def compress_word(ctx: JobContext) -> dict:
    quality    = ctx.params.get("quality", "medium")
    force_jpeg = ctx.params.get("force_jpeg", False)
    jpeg_q     = {"low": 50, "medium": 70, "high": 85}.get(quality, 70)
    orig_size  = os.path.getsize(ctx.input_path)
    work_path  = ctx.input_path

    if ctx.input_path.endswith(".doc"):
        out_dir   = tempfile.mkdtemp()
        converted = _libre(ctx.input_path, "docx", out_dir)
        if not converted:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise ProcessingError("LibreOffice required for .doc compression")
        work_path = converted

    try:
        tmp_dir = tempfile.mkdtemp()
        compressed = 0
        try:
            with zipfile.ZipFile(work_path, "r") as zin:
                zin.extractall(tmp_dir)
            media_dir = os.path.join(tmp_dir, "word", "media")
            if os.path.isdir(media_dir):
                from PIL import Image
                for fname in os.listdir(media_dir):
                    img_path = os.path.join(media_dir, fname)
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".bmp"}:
                        continue
                    try:
                        img = Image.open(img_path)
                        w, h = img.size
                        if w > 1200 or h > 1200:
                            ratio = min(1200 / w, 1200 / h)
                            img = img.resize((max(1, int(w * ratio)),
                                             max(1, int(h * ratio))), Image.LANCZOS)
                        if img.mode not in ("RGB",): img = img.convert("RGB")
                        img.save(img_path, "JPEG", quality=jpeg_q, optimize=True)
                        compressed += 1
                    except Exception as ex:
                        log.warning(f"compress-word img {fname}: {ex}")
            with zipfile.ZipFile(ctx.output_path, "w",
                                 zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
                for root, _, files in os.walk(tmp_dir):
                    for fi in files:
                        abs_p = os.path.join(root, fi)
                        zout.write(abs_p, os.path.relpath(abs_p, tmp_dir))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if work_path != ctx.input_path:
                try: os.remove(work_path)
                except OSError: pass
    except Exception as ex:
        raise ProcessingError(str(ex))

    new_size  = os.path.getsize(ctx.output_path)
    reduction = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0
    return {"reduction_pct": reduction, "images_compressed": compressed}


@register("unlock_word")
def unlock_word(ctx: JobContext) -> dict:
    if not MSOFFCRYPTO_OK:
        raise UnsupportedOperation("unlock_word", "msoffcrypto-tool")
    pw = ctx.params.get("password", "")
    if not pw:
        raise ValidationError("Password required")
    with open(ctx.input_path, "rb") as fp:
        of = msoffcrypto.OfficeFile(fp)
        of.load_key(password=pw)
        with open(ctx.output_path, "wb") as fout:
            of.decrypt(fout)
    return {}


@register("protect_word")
def protect_word(ctx: JobContext) -> dict:
    if not MSOFFCRYPTO_OK:
        raise UnsupportedOperation("protect_word", "msoffcrypto-tool")
    pw  = ctx.params.get("password", "")
    pw2 = ctx.params.get("password2", "")
    if not pw:
        raise ValidationError("Password required")
    if pw != pw2:
        raise ValidationError("Passwords do not match")
    with open(ctx.input_path, "rb") as fp:
        of = msoffcrypto.OfficeFile(fp)
        try:
            of.encrypt(pw, ctx.output_path, cipher_algorithm="AES")
        except TypeError:
            of.encrypt(pw, ctx.output_path)
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# EXCEL TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@register("excel_to_pdf")
def excel_to_pdf(ctx: JobContext) -> dict:
    out_dir = tempfile.mkdtemp()
    try:
        result = _libre(ctx.input_path, "pdf", out_dir)
        if not result:
            raise ProcessingError("LibreOffice Excel→PDF conversion failed")
        os.replace(result, ctx.output_path)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return {}


@register("excel_to_csv")
def excel_to_csv(ctx: JobContext) -> dict:
    if not OPENPYXL_OK:
        raise UnsupportedOperation("excel_to_csv", "openpyxl")
    sheet_name = ctx.params.get("sheet", "")
    all_sheets = ctx.params.get("all_sheets", False)
    wb = load_workbook(ctx.input_path, data_only=True, read_only=True)
    total_rows = 0
    if all_sheets:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for sname in wb.sheetnames:
                ws  = wb[sname]
                cb  = io.StringIO()
                w   = csv.writer(cb, quoting=csv.QUOTE_MINIMAL)
                cnt = 0
                for row in ws.iter_rows(values_only=True):
                    w.writerow([coerce_cell_for_csv(v) for v in row])
                    cnt += 1
                total_rows += cnt
                safe = re.sub(r"[^\w]", "_", sname)
                zf.writestr(f"{safe}.csv", ("\ufeff" + cb.getvalue()).encode("utf-8"))
        wb.close()
        with open(ctx.output_path, "wb") as fh:
            fh.write(buf.getvalue())
        return {"sheets": len(wb.sheetnames), "rows": total_rows}
    else:
        ws  = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        cnt = 0
        with open(ctx.output_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
            for row in ws.iter_rows(values_only=True):
                w.writerow([coerce_cell_for_csv(v) for v in row])
                cnt += 1
        wb.close()
        return {"rows": cnt}


@register("excel_to_word")
def excel_to_word(ctx: JobContext) -> dict:
    if not (OPENPYXL_OK and DOCX_OK):
        raise UnsupportedOperation("excel_to_word", "openpyxl + python-docx")
    row_limit = int(ctx.params.get("row_limit", Config.EXCEL_ROW_LIMIT))
    wb  = load_workbook(ctx.input_path, data_only=True)
    doc = DocxDocument()
    for sheet_name in wb.sheetnames:
        ws   = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True, max_row=row_limit))
        doc.add_heading(sheet_name, level=1)
        if not rows:
            doc.add_paragraph("(empty sheet)")
            continue
        n_cols = max(len(r) for r in rows)
        table  = doc.add_table(rows=len(rows), cols=n_cols)
        for ri, row_data in enumerate(rows):
            for ci in range(n_cols):
                val  = row_data[ci] if ci < len(row_data) else None
                cell = table.cell(ri, ci)
                cell.text = coerce_cell_for_csv(val)
                if ri == 0:
                    for para in cell.paragraphs:
                        for run in para.runs:
                            run.bold = True
    wb.close()
    doc.save(ctx.output_path)
    return {}


@register("excel_to_json")
def excel_to_json(ctx: JobContext) -> dict:
    if not OPENPYXL_OK:
        raise UnsupportedOperation("excel_to_json", "openpyxl")
    use_headers = ctx.params.get("header", True)
    wb   = load_workbook(ctx.input_path, data_only=True, read_only=True)
    data = {}
    for sname in wb.sheetnames:
        ws   = wb[sname]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            data[sname] = []
            continue
        if use_headers:
            seen, headers = {}, []
            for i, h in enumerate(rows[0]):
                base = str(h).strip() if h is not None else f"col_{i}"
                cnt  = seen.get(base, 0)
                seen[base] = cnt + 1
                headers.append(base if cnt == 0 else f"{base}_{cnt}")
            data[sname] = [
                {headers[c]: coerce_cell_value(v)
                 for c, v in enumerate(row) if c < len(headers)}
                for row in rows[1:]
            ]
        else:
            data[sname] = [[coerce_cell_value(v) for v in row] for row in rows]
    wb.close()
    with open(ctx.output_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
    return {"sheets": len(data)}


@register("compress_excel")
def compress_excel(ctx: JobContext) -> dict:
    if not OPENPYXL_OK:
        raise UnsupportedOperation("compress_excel", "openpyxl")
    orig = os.path.getsize(ctx.input_path)
    wb   = load_workbook(ctx.input_path, data_only=True)
    for ws in wb.worksheets:
        max_r = max_c = 0
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    max_r = max(max_r, cell.row)
                    max_c = max(max_c, cell.column)
        if max_r > 0 and ws.max_row > max_r:
            try: ws.delete_rows(max_r + 1, ws.max_row - max_r)
            except Exception: pass
    tmp = ctx.output_path + ".tmp.xlsx"
    wb.save(tmp)
    wb.close()
    try:
        with zipfile.ZipFile(tmp, "r") as zin:
            with zipfile.ZipFile(ctx.output_path, "w",
                                 zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
                for item in zin.infolist():
                    zout.writestr(item, zin.read(item.filename))
    except Exception:
        shutil.copy(tmp, ctx.output_path)
    finally:
        try: os.remove(tmp)
        except OSError: pass
    new_size  = os.path.getsize(ctx.output_path)
    reduction = round((1 - new_size / orig) * 100, 1) if orig else 0
    return {"reduction_pct": reduction}


@register("unlock_excel")
def unlock_excel(ctx: JobContext) -> dict:
    if not MSOFFCRYPTO_OK:
        raise UnsupportedOperation("unlock_excel", "msoffcrypto-tool")
    pw = ctx.params.get("password", "")
    if not pw: raise ValidationError("Password required")
    with open(ctx.input_path, "rb") as fp:
        of = msoffcrypto.OfficeFile(fp)
        of.load_key(password=pw)
        with open(ctx.output_path, "wb") as fout:
            of.decrypt(fout)
    return {}


@register("protect_excel")
def protect_excel(ctx: JobContext) -> dict:
    if not MSOFFCRYPTO_OK:
        raise UnsupportedOperation("protect_excel", "msoffcrypto-tool")
    pw  = ctx.params.get("password", "")
    pw2 = ctx.params.get("password2", "")
    if not pw: raise ValidationError("Password required")
    if pw != pw2: raise ValidationError("Passwords do not match")
    with open(ctx.input_path, "rb") as fp:
        of = msoffcrypto.OfficeFile(fp)
        try: of.encrypt(pw, ctx.output_path, cipher_algorithm="AES")
        except TypeError: of.encrypt(pw, ctx.output_path)
    return {}


@register("excel_to_jpg")
def excel_to_jpg(ctx: JobContext) -> dict:
    if not FITZ_OK:
        raise UnsupportedOperation("excel_to_jpg", "PyMuPDF")
    out_dir  = tempfile.mkdtemp()
    pdf_path = None
    try:
        pdf_path = _libre(ctx.input_path, "pdf", out_dir)
        if not pdf_path:
            raise ProcessingError("LibreOffice Excel→PDF failed")
        doc = fitz.open(pdf_path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=150)
                zf.writestr(f"sheet_{i + 1:04d}.jpg", pix.tobytes("jpeg"))
        doc.close()
        with open(ctx.output_path, "wb") as fh:
            fh.write(buf.getvalue())
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return {}


@register("excel_to_ppt")
def excel_to_ppt(ctx: JobContext) -> dict:
    if not (OPENPYXL_OK and PPTX_OK):
        raise UnsupportedOperation("excel_to_ppt", "openpyxl + python-pptx")
    wb  = load_workbook(ctx.input_path, data_only=True)
    prs = Presentation()
    prs.slide_width  = PptxInches(10)
    prs.slide_height = PptxInches(7.5)
    layout = prs.slide_layouts[1]
    for sname in wb.sheetnames:
        ws   = wb[sname]
        rows = [r for r in ws.iter_rows(values_only=True)
                if any(c is not None for c in r)]
        if not rows: continue
        slide = prs.slides.add_slide(layout)
        if slide.shapes.title: slide.shapes.title.text = sname
        max_cols = max(len(r) for r in rows)
        max_rows = min(len(rows), 25)
        tbl = slide.shapes.add_table(
            max_rows, max_cols,
            PptxInches(0.5), PptxInches(1.5), PptxInches(9), PptxInches(5)
        ).table
        for ri in range(max_rows):
            for ci in range(max_cols):
                val  = rows[ri][ci] if ci < len(rows[ri]) else ""
                cell = tbl.cell(ri, ci)
                cell.text = coerce_cell_for_csv(val)
                if ri == 0:
                    cell.text_frame.paragraphs[0].font.bold = True
    wb.close()
    prs.save(ctx.output_path)
    return {}


@register("repair_excel")
def repair_excel(ctx: JobContext) -> dict:
    if not OPENPYXL_OK:
        raise UnsupportedOperation("repair_excel", "openpyxl")
    ext = ctx.input_path.rsplit(".", 1)[-1].lower()
    if ext == "xlsm":
        shutil.copy(ctx.input_path, ctx.output_path)
        return {"method": "passthrough_xlsm"}
    try:
        wb = load_workbook(ctx.input_path, data_only=False)
        wb.save(ctx.output_path)
        wb.close()
        if os.path.getsize(ctx.output_path) > 0:
            return {"method": "openpyxl"}
    except Exception as ex:
        log.warning(f"openpyxl repair: {ex}")
    out_dir = tempfile.mkdtemp()
    try:
        result = _libre(ctx.input_path, "xlsx", out_dir)
        if result and os.path.exists(result) and os.path.getsize(result) > 0:
            os.replace(result, ctx.output_path)
            return {"method": "libreoffice"}
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    raise ProcessingError("Could not repair Excel — file severely corrupted")
