"""tests/conftest.py"""
import os
import pytest
from app import create_app

@pytest.fixture(scope="session")
def app():
    os.environ["FLASK_ENV"] = "testing"
    application = create_app("testing")
    yield application

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def sample_pdf(tmp_path):
    """Create a minimal valid 1-page PDF."""
    path = tmp_path / "sample.pdf"
    try:
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "PDFWala Test")
        doc.save(str(path))
        doc.close()
    except ImportError:
        path.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj "
                         b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
                         b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"
                         b"endobj\nxref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\n"
                         b"startxref\n0\n%%EOF")
    return str(path)

@pytest.fixture
def sample_image(tmp_path):
    path = tmp_path / "sample.jpg"
    try:
        from PIL import Image
        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        img.save(str(path))
    except ImportError:
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    return str(path)
