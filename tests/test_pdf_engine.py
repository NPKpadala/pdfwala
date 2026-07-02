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


# ── shared fitz guard + builders ───────────────────────────────────────────────

fitz_missing = not __import__("importlib").util.find_spec("fitz")
requires_fitz = pytest.mark.skipif(fitz_missing, reason="PyMuPDF not installed")


def _multi_page_pdf(path, n=3, with_toc=False):
    import fitz
    doc = fitz.open()
    for i in range(n):
        p = doc.new_page()
        p.insert_text((72, 72), f"Page {i + 1} content sample")
    if with_toc:
        # one top-level bookmark per page
        doc.set_toc([[1, f"Chapter {i + 1}", i + 1] for i in range(n)])
    doc.save(str(path))
    doc.close()
    return str(path)


def _ctx(op, input_path, out_path, params=None, input_paths=None):
    ctx = JobContext()
    ctx.operation = op
    ctx.input_path = input_path or ""
    ctx.input_paths = input_paths or []
    ctx.output_path = str(out_path)
    ctx.params = params or {}
    return ctx


# ── Security-critical operations ───────────────────────────────────────────────

@requires_fitz
def test_protect_pdf_output_is_encrypted(tmp_path, sample_pdf):
    import fitz
    from engines.pdf_engine import protect_pdf
    out = tmp_path / "protected.pdf"
    protect_pdf(_ctx("protect_pdf", sample_pdf, out, {"password": "Test1234"}))
    assert out.exists()
    d = fitz.open(str(out))
    assert d.needs_pass or d.is_encrypted
    d.close()


@requires_fitz
def test_protect_then_unlock_reports_was_encrypted(tmp_path, sample_pdf):
    import fitz
    from engines.pdf_engine import protect_pdf, unlock_pdf
    prot = tmp_path / "protected.pdf"
    protect_pdf(_ctx("protect_pdf", sample_pdf, prot, {"password": "Secret99"}))
    out = tmp_path / "unlocked.pdf"
    res = unlock_pdf(_ctx("unlock_pdf", str(prot), out, {"password": "Secret99"}))
    # This is the regression: it must report True, not the old always-True/garbage.
    assert res["was_encrypted"] is True
    d = fitz.open(str(out))
    assert not d.needs_pass
    d.close()


@requires_fitz
def test_unlock_unencrypted_reports_false(tmp_path, sample_pdf):
    from engines.pdf_engine import unlock_pdf
    out = tmp_path / "passthrough.pdf"
    res = unlock_pdf(_ctx("unlock_pdf", sample_pdf, out, {"password": "irrelevant"}))
    assert res["was_encrypted"] is False


@requires_fitz
def test_redact_pdf_text_mode_counts_matches(tmp_path, sample_pdf):
    from engines.pdf_engine import redact_pdf
    out = tmp_path / "redacted.pdf"
    res = redact_pdf(_ctx("redact_pdf", sample_pdf, out,
                          {"mode": "text", "search_text": "PDFWala"}))
    assert res["redaction_count"] > 0
    assert out.exists()


@requires_fitz
def test_sign_pdf(tmp_path, sample_pdf):
    from engines.pdf_engine import sign_pdf
    out = tmp_path / "signed.pdf"
    res = sign_pdf(_ctx("sign_pdf", sample_pdf, out, {"name": "Alice", "page": "last"}))
    assert res["pages_signed"] == 1
    assert out.exists()


@requires_fitz
def test_protect_rejects_overlong_password(tmp_path, sample_pdf):
    from engines.pdf_engine import protect_pdf
    from core.exceptions import ValidationError
    out = tmp_path / "x.pdf"
    with pytest.raises(ValidationError):
        protect_pdf(_ctx("protect_pdf", sample_pdf, out, {"password": "a" * 200}))


# ── New operations ─────────────────────────────────────────────────────────────

@requires_fitz
def test_split_by_bookmarks(tmp_path):
    import zipfile
    from engines.pdf_engine import split_by_bookmarks
    src = _multi_page_pdf(tmp_path / "book.pdf", n=3, with_toc=True)
    out = tmp_path / "sections.zip"
    res = split_by_bookmarks(_ctx("split_by_bookmarks", src, out))
    assert res["sections"] == 3
    with zipfile.ZipFile(str(out)) as z:
        assert len(z.namelist()) == 3


@requires_fitz
def test_split_by_bookmarks_without_toc_raises(tmp_path):
    from engines.pdf_engine import split_by_bookmarks
    from core.exceptions import ValidationError
    src = _multi_page_pdf(tmp_path / "plain.pdf", n=2, with_toc=False)
    with pytest.raises(ValidationError):
        split_by_bookmarks(_ctx("split_by_bookmarks", src, tmp_path / "o.zip"))


@requires_fitz
def test_split_by_size(tmp_path):
    import zipfile
    from engines.pdf_engine import split_by_size
    src = _multi_page_pdf(tmp_path / "big.pdf", n=5)
    out = tmp_path / "parts.zip"
    # Tiny cap forces one page per part.
    res = split_by_size(_ctx("split_by_size", src, out, {"max_mb": 0.001}))
    assert res["parts"] >= 1
    with zipfile.ZipFile(str(out)) as z:
        assert len(z.namelist()) == res["parts"]


@requires_fitz
def test_alternate_mix(tmp_path):
    import fitz
    from engines.pdf_engine import alternate_mix
    a = _multi_page_pdf(tmp_path / "a.pdf", n=2)
    b = _multi_page_pdf(tmp_path / "b.pdf", n=2)
    out = tmp_path / "mixed.pdf"
    res = alternate_mix(_ctx("alternate_mix", None, out, input_paths=[a, b]))
    assert res["pages_total"] == 4
    d = fitz.open(str(out)); assert len(d) == 4; d.close()


@requires_fitz
def test_remove_metadata(tmp_path):
    import fitz
    from engines.pdf_engine import remove_metadata
    src = tmp_path / "meta.pdf"
    doc = fitz.open(); doc.new_page()
    doc.set_metadata({"author": "Bob", "title": "Secret"})
    doc.save(str(src)); doc.close()
    out = tmp_path / "clean.pdf"
    res = remove_metadata(_ctx("remove_metadata", str(src), out))
    assert "author" in res["fields_cleared"]
    d = fitz.open(str(out))
    assert not (d.metadata or {}).get("author")
    d.close()


@requires_fitz
def test_add_header_footer(tmp_path):
    from engines.pdf_engine import add_header_footer
    src = _multi_page_pdf(tmp_path / "hf.pdf", n=2)
    out = tmp_path / "stamped.pdf"
    res = add_header_footer(_ctx("add_header_footer", src, out,
                                 {"footer_text": "Page {page} of {total}"}))
    assert res["pages_stamped"] == 2
    assert out.exists()


@requires_fitz
def test_resize_pdf(tmp_path):
    import fitz
    from engines.pdf_engine import resize_pdf
    src = _multi_page_pdf(tmp_path / "r.pdf", n=2)
    out = tmp_path / "resized.pdf"
    res = resize_pdf(_ctx("resize_pdf", src, out, {"page_size": "A4"}))
    assert res["pages_resized"] == 2
    d = fitz.open(str(out))
    assert round(d[0].rect.width) == 595 and round(d[0].rect.height) == 842
    d.close()


@requires_fitz
def test_pdf_to_html(tmp_path):
    from engines.pdf_engine import pdf_to_html
    src = _multi_page_pdf(tmp_path / "h.pdf", n=2)
    out = tmp_path / "out.html"
    res = pdf_to_html(_ctx("pdf_to_html", src, out))
    assert res["pages"] == 2
    html = out.read_text(encoding="utf-8")
    assert "pdf-page" in html and html.lstrip().startswith("<!DOCTYPE")


@requires_fitz
def test_fill_form(tmp_path):
    import fitz, json
    from engines.pdf_engine import fill_form
    src = tmp_path / "form.pdf"
    doc = fitz.open(); page = doc.new_page()
    w = fitz.Widget()
    w.field_name = "Name"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(72, 72, 272, 96)
    page.add_widget(w)
    doc.save(str(src)); doc.close()
    out = tmp_path / "filled.pdf"
    res = fill_form(_ctx("fill_form", str(src), out,
                         {"fields": json.dumps({"Name": "John Doe"})}))
    assert res["fields_found"] >= 1
    assert res["fields_filled"] == 1
