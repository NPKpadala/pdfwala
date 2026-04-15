"""
PDFWala V10.0
tasks/office_tasks.py — Async Celery tasks for Office ↔ PDF conversions.
"""

import os
import logging
import subprocess

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
    from openpyxl import Workbook
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

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
                      f"{result.stderr.decode()[:500]}")
            return None
        import glob
        from pathlib import Path
        base     = Path(input_path).stem
        pattern  = os.path.join(out_dir, f"{base}.{fmt}")
        if os.path.exists(pattern):
            cb_libreoffice.record_success()
            return pattern
        matches  = list(Path(out_dir).glob(f"*.{fmt}"))
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


if celery_app is not None:

    @celery_app.task(
        bind=True,
        max_retries=3,
        name="pdfwala.tasks.office_tasks.pdf_to_word_task",
        queue="office",
    )
    def pdf_to_word_task(
        self, input_path: str, output_path: str, job_id: str
    ):
        """
        Async PDF → Word conversion using pdf2docx.
        Reports page-level progress via Redis.
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
            try:
                os.remove(input_path)
            except OSError:
                pass

    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.office_tasks.pdf_to_excel_task",
        queue="office",
    )
    def pdf_to_excel_task(
        self, input_path: str, output_path: str, job_id: str
    ):
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
                            if table and any(
                                any(c for c in r if c) for r in table
                            ):
                                tables_extracted += 1
                                ws = wb.create_sheet(f"Table_{tables_extracted}")
                                for row in table:
                                    ws.append(
                                        [str(c).strip() if c else "" for c in row]
                                    )

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

    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.office_tasks.word_to_pdf_task",
        queue="office",
    )
    def word_to_pdf_task(
        self, input_path: str, output_path: str, job_id: str
    ):
        """Async Word → PDF via LibreOffice."""
        import shutil, tempfile
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

    @celery_app.task(
        bind=True,
        max_retries=2,
        name="pdfwala.tasks.office_tasks.excel_to_pdf_task",
        queue="office",
    )
    def excel_to_pdf_task(
        self, input_path: str, output_path: str, job_id: str
    ):
        """Async Excel → PDF via LibreOffice."""
        import shutil, tempfile
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
    pdf_to_word_task  = None
    pdf_to_excel_task = None
    word_to_pdf_task  = None
    excel_to_pdf_task = None
