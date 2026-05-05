"""
PDFWala V10.0
utils/validators.py — File validation, MIME detection, zip-bomb detection.
FIX #5: Streaming zip-bomb check (no full file.read()).
"""

import io
import zipfile
from typing import Optional, Set

from config import Config

# ── Extension allowlist aliases (re-exported for convenience) ─────────────────
ALLOWED_PDF   = Config.ALLOWED_PDF
ALLOWED_IMAGE = Config.ALLOWED_IMAGE
ALLOWED_DOC   = Config.ALLOWED_DOC
ALLOWED_XLS   = Config.ALLOWED_XLS
ALLOWED_HTML  = Config.ALLOWED_HTML
ALLOWED_WEBP  = Config.ALLOWED_WEBP
ALLOWED_PNG   = Config.ALLOWED_PNG
ALLOWED_JPG   = Config.ALLOWED_JPG


def detect_mime(file_obj) -> Optional[str]:
    """
    Detect MIME type from magic bytes, not extension.
    Formerly _detect_mime().
    """
    header = file_obj.read(512)
    file_obj.seek(0)
    if header[:4] == Config.OLE_MAGIC:
        return "application/msoffice"
    if header[:4] == b"PK\x03\x04":
        chunk = file_obj.read(2048)
        file_obj.seek(0)
        if b"word/"  in chunk: return "application/msword"
        if b"xl/"    in chunk: return "application/vnd.ms-excel"
        if b"ppt/"   in chunk: return "application/vnd.ms-powerpoint"
        return "application/zip"
    if header[:4]   == b"%PDF":                         return "application/pdf"
    if header[:3]   == b"\xff\xd8\xff":                return "image/jpeg"
    if header[:8]   == b"\x89PNG\r\n\x1a\n":          return "image/png"
    if header[:4]   == b"RIFF" and header[8:12] == b"WEBP": return "image/webp"
    if header[:6]   in (b"GIF87a", b"GIF89a"):         return "image/gif"
    if header[:2]   == b"BM":                          return "image/bmp"
    if header[:4]   in (b"II*\x00", b"MM\x00*"):      return "image/tiff"
    if b"<!DOCTYPE" in header or b"<html" in header.lower(): return "text/html"
    return None


def check_zip_bomb_streaming(file_obj, max_ratio: int = None) -> bool:
    """
    FIX #5: Streaming zip-bomb detection.
    Does NOT load the entire file into memory — reads central directory only.
    Returns True if the file is a suspected zip bomb.
    """
    if max_ratio is None:
        max_ratio = Config.ZIP_BOMB_RATIO
    try:
        pos = file_obj.tell()
        file_obj.seek(0, 2)
        compressed_size = file_obj.tell()
        if compressed_size == 0:
            file_obj.seek(pos)
            return False
        file_obj.seek(0)
        # ZipFile reads only the central directory, not file contents
        with zipfile.ZipFile(file_obj) as zf:
            uncompressed = sum(info.file_size for info in zf.infolist())
        file_obj.seek(pos)
        return (uncompressed / compressed_size) > max_ratio
    except Exception:
        try:
            file_obj.seek(0)
        except Exception:
            pass
        return False


def validate_file(file, allowed_ext: Set[str]) -> Optional[str]:
    """
    Validate uploaded file: extension, size, zip-bomb, MIME match.
    Returns error string on failure, None on success.
    """
    try:
        if not file or not file.filename:
            return "No file provided"

        ext = (
            file.filename.rsplit(".", 1)[-1].lower()
            if "." in file.filename
            else ""
        )
        if ext not in allowed_ext:
            return f"Invalid file type. Allowed: {', '.join(sorted(allowed_ext))}"

        file.seek(0, 2)
        size = file.tell()
        file.seek(0)

        if size == 0:
            return "File is empty"
        if size > Config.MAX_FILE_SIZE:
            return f"File too large (max {Config.MAX_FILE_SIZE // 1_048_576} MB)"

        if ext in {"html", "htm"}:
            return None

        # Zip-bomb check for container formats
        if ext in {"docx", "xlsx", "pptx", "zip"}:
            if check_zip_bomb_streaming(file):
                return "File rejected: zip bomb detected"

        mime = detect_mime(file)
        if mime in (
            "application/msoffice",
            "application/msword",
            "application/vnd.ms-excel",
            "application/vnd.ms-powerpoint",
            "application/zip",
        ):
            return None

        mime_ext_map = {
            "application/pdf": {"pdf"},
            "image/jpeg":      {"jpg", "jpeg"},
            "image/png":       {"png"},
            "image/webp":      {"webp"},
            "image/gif":       {"gif"},
            "image/bmp":       {"bmp"},
            "image/tiff":      {"tiff"},
            "text/html":       {"html", "htm"},
        }
        if mime and ext not in mime_ext_map.get(mime, {ext}):
            return f"File content does not match extension .{ext}"
        return None
    finally:
        try:
            file.seek(0)
        except Exception:
            pass


def validate_password(pw: str, pw2: str) -> Optional[str]:
    """Validate password presence and confirmation match."""
    if not pw:
        return "Password required"
    if pw != pw2:
        return "Passwords do not match"
    return None
