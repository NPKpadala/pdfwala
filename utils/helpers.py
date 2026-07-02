"""
PDFWala V10.0
utils/helpers.py — General-purpose helper utilities.
"""

import re
import uuid
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_UNSAFE_CHARS_RE = re.compile(r'[^\w\-_.]')


def generate_uuid() -> str:
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())


def sanitize_string(text: str, maxlen: int = 500) -> str:
    """Strip and truncate a string. Formerly sanitize()."""
    return (text or "").strip()[:maxlen]


def safe_int(val, default: int, lo: int = None, hi: int = None) -> int:
    """Safely parse an integer with optional range clamp. Formerly _safe_int()."""
    try:
        n = int(val)
        if lo is not None:
            n = max(lo, n)
        if hi is not None:
            n = min(hi, n)
        return n
    except (TypeError, ValueError):
        return default


def format_file_size(size_bytes: int) -> str:
    """Human-readable file size string."""
    if size_bytes > 1_048_576:
        return f"{size_bytes / 1_048_576:.2f} MB"
    return f"{size_bytes / 1024:.1f} KB"


def get_timestamp() -> str:
    """Return current UTC timestamp as ISO string."""
    return datetime.utcnow().isoformat()


def truncate_string(text: str, maxlen: int = 200, suffix: str = "...") -> str:
    """Truncate a string to maxlen, appending suffix if truncated."""
    if len(text) <= maxlen:
        return text
    return text[: maxlen - len(suffix)] + suffix


# Friendly, human-readable label for each registered operation. Used to build
# output filenames like "invoice_compressed.pdf" instead of leaking the internal
# operation name ("invoice_compress_pdf.pdf").
_OP_LABEL = {
    "compress_pdf": "compressed", "merge_pdf": "merged", "split_pdf": "split",
    "rotate_pdf": "rotated", "watermark_pdf": "watermarked",
    "protect_pdf": "protected", "unlock_pdf": "unlocked", "crop_pdf": "cropped",
    "sign_pdf": "signed", "redact_pdf": "redacted", "ocr_pdf": "ocr",
    "organize_pdf": "organized", "remove_pages": "pages_removed",
    "extract_pages": "extracted", "repair_pdf": "repaired",
    "linearize_pdf": "web_optimized", "page_numbers": "numbered",
    "edit_pdf": "edited", "pdf_info": "info",
    "pdf_to_word": "converted", "pdf_to_excel": "converted",
    "pdf_to_ppt": "converted", "pdf_to_pdfa": "pdfa",
    "pdf_to_image": "images", "pdf_to_jpg": "jpg", "pdf_to_png": "png",
    "compare_pdf": "comparison", "pdf_to_html": "html",
    "split_by_bookmarks": "chapters", "split_by_size": "parts",
    "alternate_mix": "interleaved", "remove_metadata": "cleaned",
    "add_header_footer": "stamped", "resize_pdf": "resized",
    "fill_form": "filled", "flatten_pdf": "flattened",
}

# Operations whose output is always a ZIP, used only when the caller doesn't
# pass an explicit output_ext (e.g. the Pipeline fallback resolver).
_ZIP_OPS = {
    "split_pdf", "pdf_to_jpg", "pdf_to_png", "compare_pdf", "pdf_to_image",
    "split_by_bookmarks", "split_by_size",
}

# Known label suffixes stripped from the stem so repeated operations don't
# stack ("invoice_compressed_compressed.pdf").
_KNOWN_SUFFIXES = ["_" + v for v in set(_OP_LABEL.values())]


def generate_output_filename(
    original: str,
    operation: str,
    output_ext: str = None,
    is_multi: bool = False,
    filenames: list = None,
) -> str:
    """
    Derive a friendly output filename from the original name + operation label.

    The extension is driven by `output_ext` when provided (so conversions get the
    correct extension, e.g. invoice.pdf → invoice_converted.docx). When it is not
    provided the original suffix is kept, except for known multi-file/ZIP
    operations which are forced to .zip.
    """
    if is_multi and filenames and len(filenames) > 1:
        stems = [Path(f).stem for f in filenames]
        common = _common_prefix(stems).rstrip("_-")
        name = common if len(common) > 2 else "documents"
    else:
        name = Path(original).stem
        for suffix in _KNOWN_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break

    name = _UNSAFE_CHARS_RE.sub("_", name).strip("_") or "file"
    label = _OP_LABEL.get(operation, operation)

    if output_ext:
        ext = output_ext.lstrip(".").lower()
    elif operation in _ZIP_OPS:
        ext = "zip"
    else:
        ext = (Path(original).suffix.lstrip(".") or "pdf").lower()

    return f"{name}_{label}.{ext}"


def _common_prefix(strings: List[str]) -> str:
    """Find the common prefix of a list of strings."""
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix
