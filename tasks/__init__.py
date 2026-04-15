# PDFWala V10.0 Tasks

from tasks.pdf_tasks import compress_pdf_task, merge_pdf_task, split_pdf_task, watermark_pdf_task
from tasks.ocr_tasks import ocr_pdf_task
from tasks.office_tasks import pdf_to_word_task, pdf_to_excel_task, word_to_pdf_task, excel_to_pdf_task

__all__ = [
    "compress_pdf_task",
    "merge_pdf_task",
    "split_pdf_task",
    "watermark_pdf_task",
    "ocr_pdf_task",
    "pdf_to_word_task",
    "pdf_to_excel_task",
    "word_to_pdf_task",
    "excel_to_pdf_task",
]
