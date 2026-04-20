"""
PDFWala V10.0
utils/pdf_utils.py — PDF-specific helper functions.
"""

import io
import os
from typing import List, Set, Optional, Tuple

from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import letter, A4

try:
    import fitz
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False


def parse_color_hex(hex_str: str):
    """Parse #RRGGBB hex to (r, g, b) float tuple. Formerly _parse_color_hex()."""
    try:
        h = hex_str.lstrip("#")
        return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)
    except Exception:
        return (0.5, 0.5, 0.5)


def create_watermark_pdf(
    text: str,
    opacity: float,
    color_hex: str,
    pw: float,
    ph: float,
    position: str = "diagonal",
    rotation: float = 45.0,
) -> bytes:
    """
    Create watermark overlay PDF bytes.
    Formerly _make_watermark_with_rotation().
    """
    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=(pw, ph))
    r, g_val, b = parse_color_hex(color_hex)
    alpha = max(0.05, min(opacity, 0.95))
    c.setFillColorRGB(r, g_val, b, alpha=alpha)
    font_size = min(pw, ph) * 0.08
    c.setFont("Helvetica-Bold", font_size)

    if position == "center":
        c.drawCentredString(pw / 2, ph / 2, text)
    elif position == "top":
        c.drawCentredString(pw / 2, ph * 0.95 - font_size, text)
    elif position == "bottom":
        c.drawCentredString(pw / 2, ph * 0.05, text)
    elif position == "tile":
        for row_i in range(3):
            for col_i in range(3):
                x = pw * (col_i + 0.5) / 3
                y = ph * (row_i + 0.5) / 3
                c.saveState()
                c.translate(x, y)
                c.rotate(rotation)
                c.drawCentredString(0, 0, text)
                c.restoreState()
    else:  # diagonal
        c.saveState()
        c.translate(pw / 2, ph / 2)
        c.rotate(rotation)
        c.drawCentredString(0, 0, text)
        c.restoreState()

    c.save()
    buf.seek(0)
    return buf.read()


def create_page_number_pdf(label: str, position: str, pw: float, ph: float) -> bytes:
    """
    Create a page-number overlay PDF bytes.
    Formerly _make_page_num().
    """
    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=(pw, ph))
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.2, 0.2, 0.2)
    y = ph - 30 if position == "top" else 15
    c.drawCentredString(pw / 2, y, label)
    c.save()
    buf.seek(0)
    return buf.read()


def parse_page_ranges(spec: str, total: int) -> List[int]:
    """
    Parse a page-range spec like "1-3,5,7-9" into 0-based indices.
    Formerly _parse_pages().
    """
    indices: Set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a_s, b_s = part.split("-", 1)
                a, b = int(a_s.strip()), int(b_s.strip())
                if a < 1 or b < 1:
                    continue
                for i in range(max(1, a), min(b, total) + 1):
                    indices.add(i - 1)
            except ValueError:
                pass
        else:
            try:
                n = int(part)
                if n < 1:
                    continue
                if 1 <= n <= total:
                    indices.add(n - 1)
            except ValueError:
                pass
    return sorted(indices)


def get_pdf_page_count(path: str) -> int:
    """Return the number of pages in a PDF file."""
    if not FITZ_AVAILABLE:
        raise RuntimeError("PyMuPDF (fitz) is required")
    doc = fitz.open(path)
    n   = len(doc)
    doc.close()
    return n


def extract_pdf_metadata(path: str) -> dict:
    """Extract metadata dict from a PDF."""
    if not FITZ_AVAILABLE:
        raise RuntimeError("PyMuPDF (fitz) is required")
    doc  = fitz.open(path)
    meta = doc.metadata.copy()
    meta["page_count"] = len(doc)
    doc.close()
    return meta


def compress_pdf_images(doc, dpi: int = 120, quality: int = 72):
    """
    In-place image compression for a PyMuPDF document.
    Returns True if any images were modified.
    """
    from PIL import Image
    modified = False
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base = doc.extract_image(xref)
                if not base:
                    continue
                pil = Image.open(io.BytesIO(base["image"]))
                ow, oh = pil.size
                src_dpi = max(base.get("xres", 150), base.get("yres", 150), 1)
                scale   = min(1.0, dpi / src_dpi)
                if scale >= 0.95:
                    continue
                nw = max(1, int(ow * scale))
                nh = max(1, int(oh * scale))
                pil = pil.resize((nw, nh), Image.LANCZOS)
                if pil.mode in ("RGBA", "P", "LA"):
                    bg   = Image.new("RGB", pil.size, (255, 255, 255))
                    if pil.mode == "P":
                        pil = pil.convert("RGBA")
                    mask = pil.split()[-1] if pil.mode in ("RGBA", "LA") else None
                    bg.paste(pil, mask=mask)
                    pil = bg
                elif pil.mode != "RGB":
                    pil = pil.convert("RGB")
                buf_img = io.BytesIO()
                pil.save(buf_img, "JPEG", quality=quality, optimize=True, progressive=True)
                doc.update_stream(xref, buf_img.getvalue())
                modified = True
            except Exception:
                pass
    return modified


def is_valid_pdf(file_path: str, min_pages: int = 1) -> Tuple[bool, Optional[str]]:
    """
    Strict PDF validation.
    
    Args:
        file_path: Path to the PDF file
        min_pages: Minimum number of pages required (default: 1)
    
    Returns:
        (is_valid: bool, error_message: str or None)
    """
    if not FITZ_AVAILABLE:
        return False, "PyMuPDF (fitz) is not available"
    
    try:
        # Basic filesystem checks
        if not os.path.exists(file_path):
            return False, "File does not exist"
        
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return False, "File is empty (0 bytes)"
        
        if file_size < 100:  # Absolute minimum for a valid PDF
            return False, f"File too small to be a valid PDF ({file_size} bytes)"
        
        # Check PDF header
        with open(file_path, 'rb') as f:
            header = f.read(8)
            if not header.startswith(b'%PDF-'):
                return False, "File does not have valid PDF header"
        
        # Open with PyMuPDF
        doc = fitz.open(file_path)
        page_count = len(doc)
        
        # Verify cross-reference table is intact
        try:
            doc.xref_get_keys(1)
        except Exception as xref_error:
            doc.close()
            return False, f"Corrupted cross-reference table: {xref_error}"
        
        # Try to read first page metadata (catches truncated files)
        if page_count > 0:
            try:
                _ = doc[0].rect
            except Exception as page_error:
                doc.close()
                return False, f"Cannot read page data: {page_error}"
        
        doc.close()
        
        if page_count < min_pages:
            return False, f"PDF has {page_count} page(s), minimum required: {min_pages}"
        
        return True, None
        
    except Exception as e:
        return False, f"Validation exception: {str(e)}"
