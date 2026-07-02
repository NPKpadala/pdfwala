"""tests/test_output_naming.py — output filename derivation (Bug 1–3 regression)."""
import os
from core.context import JobContext
from utils.helpers import generate_output_filename


def test_friendly_name_uses_original_and_label():
    assert generate_output_filename("invoice.pdf", "compress_pdf", output_ext="pdf") \
        == "invoice_compressed.pdf"


def test_conversion_uses_output_ext_not_original_suffix():
    name = generate_output_filename("invoice.pdf", "pdf_to_word", output_ext="docx")
    assert name.startswith("invoice")
    assert name.endswith(".docx")


def test_zip_operation_extension():
    name = generate_output_filename("report.pdf", "split_pdf", output_ext="zip")
    assert name.endswith(".zip")


def test_zip_operation_extension_without_explicit_ext():
    # Pipeline fallback path passes no output_ext — ZIP ops must still be .zip.
    name = generate_output_filename("report.pdf", "compare_pdf")
    assert name.endswith(".zip")


def test_label_suffix_is_idempotent():
    once = generate_output_filename("invoice.pdf", "compress_pdf", output_ext="pdf")
    twice = generate_output_filename(once, "compress_pdf", output_ext="pdf")
    assert twice == "invoice_compressed.pdf"


def test_resolve_output_path_contains_original_name():
    ctx = JobContext()
    ctx.operation = "compress_pdf"
    ctx.original_filename = "my_invoice.pdf"
    from services.file_service import file_service
    file_service.resolve_output_path(ctx, "pdf")
    base = os.path.basename(ctx.output_path)
    assert "my_invoice" in base
    assert base.endswith(".pdf")
    # Must NOT be the old "{operation}_{jobid}.ext" pattern.
    assert not base.startswith("compress_pdf_")


def test_resolve_output_path_conversion_extension():
    ctx = JobContext()
    ctx.operation = "pdf_to_word"
    ctx.original_filename = "report.pdf"
    from services.file_service import file_service
    file_service.resolve_output_path(ctx, "docx")
    assert os.path.basename(ctx.output_path).endswith(".docx")


def test_resolve_output_path_is_unique_per_job():
    from services.file_service import file_service
    a = JobContext(); a.operation = "compress_pdf"; a.original_filename = "x.pdf"
    b = JobContext(); b.operation = "compress_pdf"; b.original_filename = "x.pdf"
    file_service.resolve_output_path(a, "pdf")
    file_service.resolve_output_path(b, "pdf")
    assert a.output_path != b.output_path  # job_id keeps them distinct
