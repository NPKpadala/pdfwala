"""
PDFWala Enterprise V11.0.0 — Task Exports

This module re-exports all Celery tasks so callers can do:
    from tasks import compress_pdf_task
instead of drilling into submodules.

NOTE: app.py imports tasks *lazily* inside route handlers (CRIT-01 pattern).
      This __init__.py is used by worker processes and health-check introspection
      only.  It must NOT be imported at module level from app.py.
"""

from tasks.pdf_tasks import (
    compress_pdf_task,
    merge_pdf_task,
    split_pdf_task,
    watermark_pdf_task,
)
from tasks.ocr_tasks import ocr_pdf_task
from tasks.office_tasks import (
    pdf_to_word_task,
    pdf_to_excel_task,
    # FIX: excel_to_word_task added — app.py lazy-imports it; must exist here
    excel_to_word_task,
    # word_to_pdf_task / excel_to_pdf_task kept for backward compatibility
    word_to_pdf_task,
    excel_to_pdf_task,
)

__all__ = [
    "compress_pdf_task",
    "merge_pdf_task",
    "split_pdf_task",
    "watermark_pdf_task",
    "ocr_pdf_task",
    "pdf_to_word_task",
    "pdf_to_excel_task",
    "excel_to_word_task",
    "word_to_pdf_task",
    "excel_to_pdf_task",
]
