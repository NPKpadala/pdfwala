"""tests/test_pdf_engine.py"""
import io
import os
import pytest
from unittest.mock import patch, MagicMock
from core.context import JobContext


def _make_ctx(tmp_path, op, input_bytes=None):
    inp = tmp_path / "input.pdf"
    if input_bytes:
        inp.write_bytes(input_bytes)
    ctx = JobContext()
    ctx.operation   = op
    ctx.input_path  = str(inp)
    ctx.output_path = str(tmp_path / "output.pdf")
    return ctx


@pytest.mark.skipif(not __import__("importlib").util.find_spec("fitz"),
                    reason="PyMuPDF not installed")
def test_pdf_info(tmp_path, sample_pdf):
    from engines.pdf_engine import pdf_info
    ctx = JobContext()
    ctx.operation   = "pdf_info"
    ctx.input_path  = sample_pdf
    ctx.output_path = str(tmp_path / "info.json")
    result = pdf_info(ctx)
    assert "metadata" in result
    assert result["metadata"]["page_count"] >= 1


@pytest.mark.skipif(not __import__("importlib").util.find_spec("PyPDF2"),
                    reason="PyPDF2 not installed")
def test_rotate_pdf(tmp_path, sample_pdf):
    from engines.pdf_engine import rotate_pdf
    ctx = JobContext()
    ctx.operation   = "rotate_pdf"
    ctx.input_path  = sample_pdf
    ctx.output_path = str(tmp_path / "rotated.pdf")
    ctx.params      = {"angle": 90, "pages": "all"}
    result = rotate_pdf(ctx)
    assert result.get("angle") == 90
    assert os.path.exists(ctx.output_path)
