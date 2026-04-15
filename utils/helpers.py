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


def generate_output_filename(
    original: str,
    operation: str,
    is_multi: bool = False,
    filenames: list = None,
) -> str:
    """
    Derive an output filename from the original name + operation tag.
    Produces .zip for multi-page export operations.
    """
    if is_multi and filenames and len(filenames) > 1:
        stems = [Path(f).stem for f in filenames]
        common = _common_prefix(stems).rstrip("_-")
        name = common if len(common) > 2 else "merged_documents"
        ext = ".pdf"
    else:
        name = Path(original).stem
        for suffix in [
            "_compressed", "_merged", "_rotated", "_watermarked",
            "_protected", "_unlocked", "_cropped", "_converted",
            "_to_jpg", "_to_png", "_to_txt", "_to_excel", "_to_ppt",
            "_to_html", "_to_json", "_edited",
        ]:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        ext = Path(original).suffix

    name = _UNSAFE_CHARS_RE.sub("_", name)
    final = f"{name}_{operation}{ext}"

    multi_zip = {"split_pages", "to_jpg", "to_png", "comparison", "to_image"}
    if operation in multi_zip:
        final = re.sub(r"\.\w+$", ".zip", final)
        if not final.endswith(".zip"):
            final = Path(final).stem + ".zip"
    return final


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
