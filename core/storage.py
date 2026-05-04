"""
core/storage.py — PDFWala Enterprise V13.0
Atomic file operations, temp file management, and cleanup.

All disk I/O goes through this module. No file is ever written directly
from engines or tasks — they call storage helpers instead.

Key guarantees:
  - Atomic writes: output never partially written (write to .tmp → rename)
  - No leaks: temp files tracked and cleaned in finally blocks
  - Thread-safe: temp file registry protected by lock
"""

import os
import shutil
import time
import uuid
import threading
from pathlib import Path
from typing import List, Optional
from contextlib import contextmanager

from core.logger import log
from core.exceptions import ResourceError


# ── Temp file registry (thread-safe) ───────────────────────────────────────
_temp_registry: List[str] = []
_registry_lock = threading.Lock()


def register_temp(path: str) -> None:
    """Add a file to the cleanup registry."""
    with _registry_lock:
        if path not in _temp_registry:
            _temp_registry.append(path)


def unregister_temp(path: str) -> None:
    """Remove a file from the cleanup registry (called after successful move)."""
    with _registry_lock:
        if path in _temp_registry:
            _temp_registry.remove(path)


def cleanup_temp_files(paths: Optional[List[str]] = None) -> int:
    """
    Delete all tracked temp files (or a specific list).
    Returns count of files deleted.
    Always succeeds — never raises.
    """
    deleted = 0
    targets = paths if paths is not None else _temp_registry.copy()

    for p in targets:
        try:
            if os.path.exists(p):
                os.remove(p)
                deleted += 1
        except OSError:
            pass

    if paths is None:
        with _registry_lock:
            _temp_registry.clear()

    return deleted


# ═════════════════════════════════════════════════════════════════════════════
# ATOMIC WRITE
# ═════════════════════════════════════════════════════════════════════════════

@contextmanager
def atomic_write(final_path: str, job_id: str = None):
    """
    Context manager for atomic file writes.

    Usage:
        with atomic_write("/outputs/result.pdf", job_id="abc") as tmp:
            doc.save(tmp)  # write to temp file
        # temp is atomically renamed to final_path on exit
        # temp is deleted if exception occurs
    """
    final = Path(final_path).resolve()
    parent = final.parent

    # Ensure parent directory exists
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        raise ResourceError(f"Cannot create output directory {parent}: {e}")

    # Create temp file alongside final to ensure same filesystem (atomic rename)
    tmp_prefix = f".tmp_{job_id}_" if job_id else ".tmp_"
    tmp_path = parent / f"{tmp_prefix}{final.name}.{int(time.time() * 1000000)}"

    try:
        yield str(tmp_path)

        # On success: atomic rename
        if tmp_path.exists() and tmp_path.stat().st_size > 0:
            os.replace(str(tmp_path), str(final))
            log.info("atomic_write_success", job_id=job_id, path=str(final))
        else:
            raise ResourceError("Output file is empty after write")

    except Exception:
        # On failure: clean up temp file, then re-raise
        try:
            if tmp_path.exists():
                os.remove(str(tmp_path))
        except OSError:
            pass
        raise


# ═════════════════════════════════════════════════════════════════════════════
# DISK SPACE CHECK
# ═════════════════════════════════════════════════════════════════════════════

def check_disk_space(required_bytes: int, path: str = None,
                     job_id: str = None) -> bool:
    """
    Verify at least `required_bytes` are free in the target directory.
    Raises ResourceError if insufficient space.

    Args:
        required_bytes: minimum free space needed
        path: check the directory containing this path (default: temp dir)
    """
    if path:
        target_dir = os.path.dirname(os.path.abspath(path))
    else:
        target_dir = str(Path(__file__).parent.parent / "temp")

    try:
        stat = shutil.disk_usage(target_dir)
        free_bytes = stat.free

        if free_bytes < required_bytes:
            free_mb = free_bytes / (1024 * 1024)
            req_mb = required_bytes / (1024 * 1024)
            log.warning("disk_space_low", job_id=job_id,
                         free_mb=round(free_mb, 1),
                         required_mb=round(req_mb, 1))
            raise ResourceError(
                f"Insufficient disk space: {free_mb:.1f} MB free, "
                f"{req_mb:.1f} MB required in {target_dir}"
            )

        return True

    except ResourceError:
        raise
    except OSError as e:
        log.warning("disk_check_failed", job_id=job_id, error=str(e))
        return True  # Can't check — assume OK rather than blocking


# ═════════════════════════════════════════════════════════════════════════════
# TEMP FILE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def create_temp_path(prefix: str = "pdfwala", suffix: str = "",
                     job_id: str = None) -> str:
    """
    Generate a unique temp file path in the temp directory.
    Registers it for automatic cleanup.
    """
    temp_dir = Path(__file__).parent.parent / "temp"
    os.makedirs(temp_dir, exist_ok=True)

    jid = job_id or uuid.uuid4().hex[:8]
    fname = f"{prefix}_{jid}_{uuid.uuid4().hex[:8]}{suffix}"
    path = str(temp_dir / fname)

    register_temp(path)
    return path


def save_upload_to_temp(file_data: bytes, original_filename: str,
                        job_id: str = None) -> str:
    """
    Save an uploaded file to a temp location.
    Returns the temp file path.
    """
    ext = os.path.splitext(original_filename)[1].lower() or ".bin"
    path = create_temp_path(prefix="upload", suffix=ext, job_id=job_id)

    with open(path, "wb") as f:
        f.write(file_data)

    log.info("upload_saved", job_id=job_id, path=path,
             size_bytes=len(file_data))
    return path


def copy_to_temp(source_path: str, prefix: str = "copy",
                 job_id: str = None) -> str:
    """
    Copy a file to the temp directory (for async processing where original
    may be cleaned up before the worker starts).
    """
    ext = os.path.splitext(source_path)[1]
    path = create_temp_path(prefix=prefix, suffix=ext, job_id=job_id)
    shutil.copy2(source_path, path)
    return path
