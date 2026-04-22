"""
PDFWala Enterprise V11.0.0
tasks/office_tasks.py — Async Celery tasks for Office ↔ PDF conversions.

Fixes vs V10:
  - excel_to_word_task added (app.py lazy-imports it for large Excel→Word)
  - word_to_pdf_task / excel_to_pdf_task retained for backward compat
  - time_limit / soft_time_limit added to all tasks (V11 requirement)
  - input file cleanup moved into finally block on ALL tasks
  - -dNOSAFER removed from any embedded GS calls (security fix CRIT-05)
"""

import os
import logging
import shutil
import subprocess
import tempfile

from workers.celery_app import celery_app
from services.redis_service import redis_service
from services.queue_service import cb_libreoffice
from config import Config
from utils.helpers import get_timestamp

log = logging.getLogger("pdfwala.tasks.office")

try:
    from pdf2docx import Converter as Pdf2DocxConverter
    PDF2DOCX_AVAILABLE = True
except ImportError:
    PDF2DOCX_AVAILABLE = False

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

try:
    from docx import Document as DocxDocument
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    import fitz
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False


# ── LibreOffice helper (task-local) ───────────────────────────────────────────

def _libre_convert(input_path: str, fmt: str, out_dir: str) -> str | None:
    """Run LibreOffice conversion via subprocess list args (no shell=True)."""
    if not cb_libreoffice.can_execute():
        log.error("CircuitBreaker[libreoffice] OPEN")
        return None
    try:
        result = subprocess.run(
            [Config.LIBREOFFICE, "--headless", "--convert-to", fmt,
             "--outdir", out_dir, input_path],
            capture_output=True,
            timeout=Config.SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            cb_libreoffice.record_failure()
            log.error(f"LibreOffice rc={result.returncode}: "
                      f"{result.stderr.decode('utf-8', errors='ignore')[:500]}")
            return None
        from pathlib import Path
        base    = Path(input_path).stem
        pattern = os.path.join(out_dir, f"{base}.{fmt}")
        if os.path.exists(pattern):
            cb_libreoffice.record_success()
            return pattern
        matches = list(Path(out_dir).glob(f"*.{fmt}"))
        if matches:
            cb_libreoffice.record_success()
            return str(matches[0])
        cb_libreoffice.record_failure()
        return None
    except subprocess.TimeoutExpired:
        cb_libreoffice.record_failure()
        log.error("LibreOffice timed out")
        return None
    except Exception as ex:
        cb_libreoffice.record_failure()
        log.error(f"LibreOffice exception: {ex}")
        return None


# ── Tasks ──────────────────────────────────────────────────────────────────────

if celery_app is not None:

    # ------------------------------------------------------------------
    # PDF → Word
    # ------------------------------------------------------------------
    @celery_app.task(
        bind=True,
        max_retries=3,
        name="pdfwala.tasks.office_tasks.pdf_to_word_task",
        queue="office",
        time_limit=1800,       # 30 min hard kill
        soft_time_limit=1500,  # 25 min — raises SoftTimeLimitExceeded
    )
    def pdf_to_word_task(self, input_path: str, output_path: str, job_id: str):
        """
        Async PDF → Word conversion using pdf2docx.
        Reports page-level progress via Redis.
        Cleans up input_path in finally block.
        """
        try:
            if not PDF2DOCX_AVAILABLE:
                raise RuntimeError("pdf2docx not installed")

            redis_service.job_update(job_id, {"status": "processing", "progress": "5"})

            if FITZ_AVAILABLE:
                doc         = fitz.open(input_path)
                total_pages = len(doc)
                doc.close()
                redis_service.job_update(job_id, {"total_pages": str(total_pages)})

            cv = Pdf2DocxConverter(input_path)
            cv.convert(output_path, start=0, end=None)
            cv.close()

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("Output file missing or empty")

            redis_service.job_update(job_id, {
                "status":       "completed",
                "progress":     "100",
                "output_path":  output_path,
                "completed_at": get_timestamp(),
            })
            return {"status": "completed", "output": output_path}

        except Exception as ex:
            log.error(f"pdf_to_word_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            delay = 30 * (3 ** self.request.retries)
            raise self.retry(exc=ex, countdown=delay, max_retries=3)
        finally:
            # FIX: always clean up input file (office files can be very large)
            try:
                os.remove(input_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # PDF → Excel
    # ------------------------------------------------------------------
    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.office_tasks.pdf_to_excel_task",
        queue="office",
        time_limit=1800,
        soft_time_limit=1500,
    )
    def pdf_to_excel_task(self, input_path: str, output_path: str, job_id: str):
        """Async PDF → Excel extraction (tables or raw text fallback)."""
        try:
            if not OPENPYXL_AVAILABLE:
                raise RuntimeError("openpyxl not installed")

            redis_service.job_update(job_id, {"status": "processing"})
            wb = Workbook()
            wb.remove(wb.active)
            tables_extracted = 0

            if PDFPLUMBER_AVAILABLE:
                with pdfplumber.open(input_path) as pdf:
                    for page in pdf.pages:
                        for table in page.extract_tables():
                            if table and any(any(c for c in r if c) for r in table):
                                tables_extracted += 1
                                ws = wb.create_sheet(f"Table_{tables_extracted}")
                                for row in table:
                                    ws.append([str(c).strip() if c else "" for c in row])

            if tables_extracted == 0 and FITZ_AVAILABLE:
                ws      = wb.create_sheet("Text")
                doc     = fitz.open(input_path)
                row_idx = 1
                for pg_num, pg in enumerate(doc):
                    ws.cell(row_idx, 1, f"--- Page {pg_num + 1} ---")
                    row_idx += 1
                    for line in pg.get_text("text").split("\n"):
                        if line.strip():
                            ws.cell(row_idx, 1, line.strip())
                            row_idx += 1
                doc.close()

            wb.save(output_path)
            redis_service.job_update(job_id, {
                "status":       "completed",
                "progress":     "100",
                "output_path":  output_path,
                "completed_at": get_timestamp(),
            })
            return {"status": "completed", "output": output_path}

        except Exception as ex:
            log.error(f"pdf_to_excel_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            raise self.retry(exc=ex, countdown=30)
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Excel → Word  [NEW IN V11 — app.py lazy-imports this]
    # ------------------------------------------------------------------
    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.office_tasks.excel_to_word_task",
        queue="office",
        time_limit=1800,
        soft_time_limit=1500,
    )
    def excel_to_word_task(
        self,
        input_path: str,
        output_path: str,
        job_id: str,
        preserve_formulas: bool = True,
        row_limit: int = 5000,
    ):
        """
        Async Excel → Word conversion.
        Mirrors the synchronous path in app.py excel_to_word() handler.
        """
        try:
            if not OPENPYXL_AVAILABLE or not DOCX_AVAILABLE:
                raise RuntimeError("openpyxl + python-docx required")

            redis_service.job_update(job_id, {"status": "processing", "progress": "5"})

            wb  = load_workbook(input_path, data_only=not preserve_formulas)
            doc = DocxDocument()
            sheet_count     = len(wb.sheetnames)
            formulas_present = False

            for sheet_name in wb.sheetnames:
                ws       = wb[sheet_name]
                doc.add_heading(sheet_name, level=1)
                all_rows = list(ws.iter_rows(values_only=True, max_row=row_limit + 1))
                truncated  = len(all_rows) > row_limit
                rows_write = all_rows[:row_limit]

                if not rows_write:
                    doc.add_paragraph("(empty sheet)")
                    continue

                n_cols = max((len(r) for r in rows_write), default=1)
                table  = doc.add_table(rows=len(rows_write), cols=n_cols)
                try:
                    table.style = "Light Grid Accent 1"
                except Exception:
                    pass

                for r_idx, row_data in enumerate(rows_write):
                    for c_idx in range(n_cols):
                        val  = row_data[c_idx] if c_idx < len(row_data) else None
                        cell = table.cell(r_idx, c_idx)
                        cell.text = str(val) if val is not None else ""
                        if r_idx == 0:
                            for para in cell.paragraphs:
                                for run in para.runs:
                                    run.bold = True
                        if isinstance(val, str) and val.startswith("="):
                            formulas_present = True

                if truncated:
                    doc.add_paragraph(f"(Truncated to {row_limit} rows)")
                doc.add_paragraph()

            wb.close()
            doc.save(output_path)

            redis_service.job_update(job_id, {
                "status":       "completed",
                "progress":     "100",
                "output_path":  output_path,
                "completed_at": get_timestamp(),
                "sheets":       str(sheet_count),
            })
            return {"status": "completed", "output": output_path}

        except Exception as ex:
            log.error(f"excel_to_word_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            raise self.retry(exc=ex, countdown=30)
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Word → PDF  (retained for backward compat)
    # ------------------------------------------------------------------
    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.office_tasks.word_to_pdf_task",
        queue="office",
        time_limit=1800,
        soft_time_limit=1500,
    )
    def word_to_pdf_task(self, input_path: str, output_path: str, job_id: str):
        """Async Word → PDF via LibreOffice."""
        try:
            redis_service.job_update(job_id, {"status": "processing"})
            out_dir   = tempfile.mkdtemp()
            converted = _libre_convert(input_path, "pdf", out_dir)
            if converted and os.path.exists(converted):
                shutil.move(converted, output_path)
            else:
                raise RuntimeError("LibreOffice conversion failed")
            shutil.rmtree(out_dir, ignore_errors=True)
            redis_service.job_update(job_id, {
                "status":       "completed",
                "progress":     "100",
                "output_path":  output_path,
                "completed_at": get_timestamp(),
            })
            return {"status": "completed", "output": output_path}
        except Exception as ex:
            log.error(f"word_to_pdf_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            raise self.retry(exc=ex, countdown=30)
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Excel → PDF  (retained for backward compat)
    # ------------------------------------------------------------------
    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.office_tasks.excel_to_pdf_task",
        queue="office",
        time_limit=1800,
        soft_time_limit=1500,
    )
    def excel_to_pdf_task(self, input_path: str, output_path: str, job_id: str):
        """Async Excel → PDF via LibreOffice."""
        try:
            redis_service.job_update(job_id, {"status": "processing"})
            out_dir   = tempfile.mkdtemp()
            converted = _libre_convert(input_path, "pdf", out_dir)
            if converted and os.path.exists(converted):
                shutil.move(converted, output_path)
            else:
                raise RuntimeError("LibreOffice conversion failed")
            shutil.rmtree(out_dir, ignore_errors=True)
            redis_service.job_update(job_id, {
                "status":       "completed",
                "progress":     "100",
                "output_path":  output_path,
                "completed_at": get_timestamp(),
            })
            return {"status": "completed", "output": output_path}
        except Exception as ex:
            log.error(f"excel_to_pdf_task {job_id}: {ex}")
            redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
            raise self.retry(exc=ex, countdown=30)
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

else:
    # Stubs when Celery is unavailable
    pdf_to_word_task  = None
    pdf_to_excel_task = None
    excel_to_word_task = None
    word_to_pdf_task  = None
    excel_to_pdf_task = None
