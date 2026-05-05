"""
core/validators.py — PDFWala Enterprise V13.0
File and path validation — called by pipeline before any processing.

All validators raise ValidationError with a user-facing message on failure.
"""

import os
from pathlib import Path
from typing import Optional

from core.exceptions import ValidationError
from core.logger import log
from config import Config


# ── Allowed extensions by category ──────────────────────────────────────────
ALLOWED_EXTENSIONS = {
    "pdf":   {".pdf"},
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"},
    "doc":   {".doc", ".docx"},
    "xls":   {".xls", ".xlsx"},
    "ppt":   {".ppt", ".pptx"},
    "html":  {".html", ".htm"},
}

# ── Magic bytes for common formats ──────────────────────────────────────────
MAGIC_BYTES = {
    "pdf":  b"%PDF-",
    "png":  b"\x89PNG\r\n\x1a\n",
    "jpg":  b"\xff\xd8\xff",
    "webp": b"RIFF",
    "gif":  b"GIF8",
    "zip":  b"PK\x03\x04",      # DOCX/XLSX/PPTX are ZIP-based
    "ole":  b"\xd0\xcf\x11\xe0", # DOC/XLS/PPT (legacy)
}

# ── Size limits ─────────────────────────────────────────────────────────────
MAX_FILE_SIZE_BYTES = getattr(Config, "MAX_FILE_SIZE", 200 * 1024 * 1024)
MIN_FILE_SIZE_BYTES = 10  # Reject 0-byte and near-empty files


# ═════════════════════════════════════════════════════════════════════════════
# PATH VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

def validate_path_safe(path: str, label: str = "path") -> str:
    """
    Resolve path and verify it's within an allowed directory.
    Prevents path traversal attacks.
    Returns the resolved absolute path.
    """
    if not path:
        raise ValidationError(f"{label} is required")

    try:
        resolved = Path(path).resolve()
    except Exception:
        raise ValidationError(f"Invalid {label}: {path}")

    # Path traversal guard
    str_path = str(resolved)
    if ".." in str_path:
        raise ValidationError(f"Invalid characters in {label}")

    return str_path


def validate_input_path(input_path: str) -> str:
    """Validate and resolve the input file path."""
    resolved = validate_path_safe(input_path, "input_path")

    if not os.path.exists(resolved):
        raise ValidationError(f"Input file not found")

    if not os.path.isfile(resolved):
        raise ValidationError(f"Input path is not a file")

    return resolved


def validate_output_path(output_path: str) -> str:
    """Validate output path — file doesn't need to exist yet, but dir must."""
    resolved = validate_path_safe(output_path, "output_path")

    parent = os.path.dirname(resolved)
    if not os.path.isdir(parent):
        raise ValidationError(f"Output directory does not exist: {parent}")

    return resolved


# ═════════════════════════════════════════════════════════════════════════════
# FILE VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

def validate_file_exists(input_path: str) -> None:
    """Verify file exists and is readable."""
    if not os.path.exists(input_path):
        raise ValidationError("Input file not found")
    if not os.path.isfile(input_path):
        raise ValidationError("Input path is not a file")
    if not os.access(input_path, os.R_OK):
        raise ValidationError("Input file is not readable")


def validate_file_size(input_path: str,
                       max_bytes: int = MAX_FILE_SIZE_BYTES,
                       job_id: str = None) -> int:
    """
    Check file size within limits.
    Returns file size in bytes.
    """
    try:
        size = os.path.getsize(input_path)
    except OSError:
        raise ValidationError("Cannot read input file")

    if size < MIN_FILE_SIZE_BYTES:
        raise ValidationError("Input file is empty or too small")

    if size > max_bytes:
        size_mb = size / (1024 * 1024)
        max_mb = max_bytes / (1024 * 1024)
        log.warning("file_too_large", job_id=job_id,
                     size_mb=round(size_mb, 2), max_mb=round(max_mb, 2))
        raise ValidationError(
            f"File too large: {size_mb:.1f} MB (max {max_mb:.0f} MB)"
        )

    return size


def validate_extension(input_path: str, file_type: str) -> str:
    """
    Check file extension matches expected type.
    Returns the lowercase extension.
    """
    ext = os.path.splitext(input_path)[1].lower()

    if file_type not in ALLOWED_EXTENSIONS:
        raise ValidationError(f"Unsupported file type: {file_type}")

    if ext not in ALLOWED_EXTENSIONS[file_type]:
        allowed = ", ".join(ALLOWED_EXTENSIONS[file_type])
        raise ValidationError(
            f"Invalid file extension '{ext}' for {file_type}. Allowed: {allowed}"
        )

    return ext


def validate_magic_bytes(input_path: str, expected_type: str,
                         job_id: str = None) -> bool:
    """
    Check file starts with expected magic bytes.
    Prevents file-extension-spoofing attacks.
    """
    magic = MAGIC_BYTES.get(expected_type)
    if not magic:
        return True  # No magic bytes defined for this type — skip check

    try:
        with open(input_path, "rb") as f:
            header = f.read(len(magic))
    except OSError:
        raise ValidationError("Cannot read input file for validation")

    if not header.startswith(magic):
        log.warning("magic_bytes_mismatch", job_id=job_id,
                     expected=expected_type, path=input_path)
        raise ValidationError(
            f"File content does not match expected type ({expected_type}). "
            f"The file may be corrupted or have a wrong extension."
        )

    return True


def validate_pdf(input_path: str, job_id: str = None) -> int:
    """
    Full PDF validation: exists + size + extension + magic bytes + fitz open.
    Returns page count.
    """
    validate_file_exists(input_path)
    validate_file_size(input_path, job_id=job_id)
    validate_extension(input_path, "pdf")
    validate_magic_bytes(input_path, "pdf", job_id=job_id)

    # Deep validation: try opening with PyMuPDF
    try:
        import fitz
        doc = fitz.open(input_path)
        page_count = len(doc)
        doc.close()

        if page_count == 0:
            raise ValidationError("PDF has no pages")

        return page_count

    except ValidationError:
        raise
    except Exception as e:
        log.warning("pdf_validation_failed", job_id=job_id, error=str(e))
        raise ValidationError(
            f"Cannot open PDF — the file may be corrupted or password-protected"
        )


# ═════════════════════════════════════════════════════════════════════════════
# CONTEXT VALIDATION (used by pipeline)
# ═════════════════════════════════════════════════════════════════════════════

def validate_context(ctx) -> None:
    """
    Validate a JobContext before pipeline processing.
    Called by Pipeline.validate() phase.
    """
    from core.context import JobContext

    if not ctx.operation:
        raise ValidationError("Operation name is required")

    if not ctx.input_path and not ctx.input_paths:
        raise ValidationError("No input file(s) specified")

    if ctx.input_path:
        ctx.input_path = validate_input_path(ctx.input_path)
        validate_file_size(ctx.input_path, job_id=ctx.job_id)

    for i, p in enumerate(ctx.input_paths):
        ctx.input_paths[i] = validate_input_path(p)
        validate_file_size(ctx.input_paths[i], job_id=ctx.job_id)

    if ctx.output_path:
        ctx.output_path = validate_output_path(ctx.output_path)
