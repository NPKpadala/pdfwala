"""
PDFWala Enterprise V11.1.0
utils/pdf_utils.py — PDF helpers + universal chunked parallel processor.

New in V11.1:
  - chunked_pdf_processor(): reusable parallel chunking engine used by all
    page-looping tools (OCR, Excel, watermark, rotate, page-numbers, redact,
    pdf-to-image).  Implements:
      * per-chunk retry (1 retry before job-level fallback)
      * disk-space pre-check (>= 2x input size in temp dir)
      * temp-file cleanup in finally even on crash
      * conservative worker counts (configurable)
      * Redis progress reporting after each chunk
  - check_disk_space(): standalone helper
  - merge_pdf_chunks() / merge_zip_chunks(): ready-to-use merge_func impls
"""

import io
import os
import shutil
import logging
import zipfile
from typing import List, Set, Optional, Tuple, Callable

from reportlab.pdfgen import canvas as rl_canvas

log = logging.getLogger("pdfwala.utils.pdf")

try:
    import fitz
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    FUTURES_AVAILABLE = True
except ImportError:
    FUTURES_AVAILABLE = False


# =============================================================================
# DISK SPACE GUARD
# =============================================================================

def check_disk_space(
    input_path: str,
    temp_dir: str,
    multiplier: float = 2.0,
) -> Tuple[bool, Optional[str]]:
    """
    Verify temp_dir has at least (multiplier * input file size) free bytes.

    Returns (ok, error_message).  error_message is None when ok is True.
    """
    try:
        input_size = os.path.getsize(input_path)
        required   = int(input_size * multiplier)
        free       = shutil.disk_usage(temp_dir).free
        if free < required:
            mb_req  = required // (1024 * 1024)
            mb_free = free     // (1024 * 1024)
            return False, (
                f"Insufficient temp disk space: need ~{mb_req} MB but only "
                f"{mb_free} MB free in {temp_dir}. Free disk space and retry."
            )
        return True, None
    except OSError as exc:
        return False, f"Disk space check failed: {exc}"


# =============================================================================
# UNIVERSAL CHUNKED PDF PROCESSOR
# =============================================================================

def chunked_pdf_processor(
    input_path: str,
    output_path: str,
    job_id: str,
    total_pages: int,
    chunk_size: int,
    max_workers: int,
    process_chunk_func: Callable,
    merge_func: Callable,
    redis_service,
    tool_name: str = "Processing",
    report_progress: bool = True,
    chunk_retry: int = 1,
) -> bool:
    """
    Universal chunked PDF processor.

    Workflow
    --------
    1. Disk-space pre-check  (>= 2x input file in temp dir)
    2. Split source PDF into N chunks of <= chunk_size pages via PyMuPDF
    3. Process chunks in parallel (ThreadPoolExecutor, max_workers)
    4. Per-chunk retry: each chunk retried chunk_retry times before the whole
       job falls back to single-pass (caller receives False)
    5. Redis progress reported after every completed chunk
    6. merge_func() assembles final output
    7. ALL temp files deleted in finally — guaranteed even on crash

    Parameters
    ----------
    input_path         : source PDF
    output_path        : final output (written by merge_func)
    job_id             : Redis key
    total_pages        : page count (caller already has it)
    chunk_size         : pages per chunk
    max_workers        : ThreadPoolExecutor concurrency (keep conservative: 2-4)
    process_chunk_func : Callable(chunk_pdf_path, chunk_index, start_page, end_page)
                         -> str output path  OR raises on failure
    merge_func         : Callable(list[str] ordered_output_paths, output_path)
    redis_service      : redis_service instance (None skips updates)
    tool_name          : label used in log/progress messages
    report_progress    : if False, skip Redis writes
    chunk_retry        : retries per chunk before job-level fallback

    Returns
    -------
    True  — success; output_path has been created
    False — caller should fallback to single-pass
    """
    if not FITZ_AVAILABLE or not FUTURES_AVAILABLE:
        log.warning(
            f"[{tool_name}] chunked_pdf_processor: missing dependency, skipping chunking"
        )
        return False

    base_temp   = os.path.dirname(input_path)
    chunk_pdfs  = []   # split input  chunks
    chunk_outs  = []   # output chunks from process_chunk_func

    try:
        # 1 — Disk space guard
        ok, disk_err = check_disk_space(input_path, base_temp, multiplier=2.0)
        if not ok:
            log.error(f"[{tool_name}] {job_id}: {disk_err}")
            if report_progress and redis_service:
                try:
                    redis_service.job_update(job_id, {"warning": disk_err})
                except Exception:
                    pass
            return False

        # 2 — Split source PDF into chunks
        chunk_ranges = []
        src_doc = fitz.open(input_path)
        try:
            start = 0
            while start < total_pages:
                end = min(start + chunk_size, total_pages)
                chunk_ranges.append((start, end))

                chunk_path = os.path.join(
                    base_temp,
                    f"_chunk_{job_id}_{len(chunk_ranges)-1:04d}_in.pdf",
                )
                c_doc = fitz.open()
                c_doc.insert_pdf(src_doc, from_page=start, to_page=end - 1)
                c_doc.save(chunk_path)
                c_doc.close()
                chunk_pdfs.append(chunk_path)

                start = end
        finally:
            src_doc.close()

        n_chunks  = len(chunk_pdfs)
        completed = [0]  # mutable counter for lambda closure

        def _run_chunk_with_retry(ci: int) -> Tuple[int, str]:
            cpath      = chunk_pdfs[ci]
            s_page, e_page = chunk_ranges[ci]
            last_exc   = None
            for attempt in range(chunk_retry + 1):
                try:
                    out = process_chunk_func(cpath, ci, s_page, e_page)
                    return ci, out
                except Exception as exc:
                    last_exc = exc
                    if attempt < chunk_retry:
                        log.warning(
                            f"[{tool_name}] {job_id}: chunk {ci} "
                            f"attempt {attempt+1} failed ({exc}), retrying…"
                        )
            log.error(
                f"[{tool_name}] {job_id}: chunk {ci} failed after "
                f"{chunk_retry+1} attempts: {last_exc}"
            )
            raise last_exc

        # 3 — Parallel processing
        ordered_outs = [None] * n_chunks
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_chunk_with_retry, ci): ci for ci in range(n_chunks)}
            for fut in as_completed(futures):
                ci = futures[fut]
                try:
                    _, out_path = fut.result()
                    ordered_outs[ci] = out_path
                    chunk_outs.append(out_path)
                    completed[0] += 1

                    # 5 — Redis progress (90% for chunk work, 10% reserved for merge)
                    if report_progress and redis_service:
                        pct = int(completed[0] / n_chunks * 90)
                        try:
                            redis_service.job_update(job_id, {
                                "progress":     str(pct),
                                "chunks_done":  str(completed[0]),
                                "chunks_total": str(n_chunks),
                            })
                        except Exception:
                            pass
                except Exception:
                    # already logged inside _run_chunk_with_retry
                    return False  # triggers finally cleanup

        if any(p is None for p in ordered_outs):
            log.error(f"[{tool_name}] {job_id}: missing chunk outputs — fallback")
            return False

        # 6 — Merge
        if report_progress and redis_service:
            try:
                redis_service.job_update(job_id, {"progress": "92", "status": "merging"})
            except Exception:
                pass

        merge_func(ordered_outs, output_path)

        if report_progress and redis_service:
            try:
                redis_service.job_update(job_id, {"progress": "100"})
            except Exception:
                pass

        return True

    except Exception as exc:
        log.error(f"[{tool_name}] {job_id}: chunked_pdf_processor error: {exc}")
        return False

    finally:
        # 7 — Guaranteed temp-file cleanup (crash-safe)
        for p in chunk_pdfs:
            _safe_remove(p)
        for p in chunk_outs:
            _safe_remove(p)


def _safe_remove(path: Optional[str]) -> None:
    """Delete a file silently — ignores all errors."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


# =============================================================================
# MERGE HELPERS
# =============================================================================

def merge_pdf_chunks(chunk_paths: List[str], output_path: str) -> None:
    """Merge a list of single-chunk PDF files into one output PDF (PyMuPDF)."""
    if not FITZ_AVAILABLE:
        raise RuntimeError("fitz required for merge_pdf_chunks")
    out_doc = fitz.open()
    try:
        for cp in chunk_paths:
            chunk_doc = fitz.open(cp)
            out_doc.insert_pdf(chunk_doc)
            chunk_doc.close()
        tmp = output_path + ".merge_tmp"
        out_doc.save(tmp, deflate=True, garbage=2)
        os.replace(tmp, output_path)
    finally:
        out_doc.close()


def merge_zip_chunks(chunk_paths: List[str], output_path: str) -> None:
    """Merge per-chunk ZIP files into one combined ZIP at output_path."""
    tmp = output_path + ".merge_tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out_zf:
            for cp in chunk_paths:
                with zipfile.ZipFile(cp, "r") as in_zf:
                    for name in in_zf.namelist():
                        out_zf.writestr(name, in_zf.read(name))
        os.replace(tmp, output_path)
    except Exception:
        _safe_remove(tmp)
        raise


# =============================================================================
# ORIGINAL HELPERS (V10 / V11.0 — unchanged API)
# =============================================================================

def parse_color_hex(hex_str: str):
    """Parse #RRGGBB hex to (r, g, b) float tuple."""
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
    """Create watermark overlay PDF bytes (ReportLab)."""
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
    """Create a page-number overlay PDF bytes (ReportLab)."""
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
    """Parse '1-3,5,7-9' into sorted 0-based page indices."""
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

    Returns (is_valid, error_message).  error_message is None when valid.
    """
    if not FITZ_AVAILABLE:
        return False, "PyMuPDF (fitz) is not available"

    try:
        if not os.path.exists(file_path):
            return False, "File does not exist"

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return False, "File is empty (0 bytes)"
        if file_size < 100:
            return False, f"File too small to be a valid PDF ({file_size} bytes)"

        with open(file_path, "rb") as f:
            header = f.read(8)
            if not header.startswith(b"%PDF-"):
                return False, "File does not have valid PDF header"

        doc = fitz.open(file_path)
        page_count = len(doc)

        try:
            doc.xref_get_keys(1)
        except Exception as xref_error:
            doc.close()
            return False, f"Corrupted cross-reference table: {xref_error}"

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
