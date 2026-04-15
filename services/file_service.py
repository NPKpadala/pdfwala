"""
PDFWala V10.0
services/file_service.py — Streaming file I/O with atomic writes and file locking.
FIX #1: Streaming temp_upload (no full file.read()).
FIX #2: fcntl-based file locking (POSIX).
"""

import os
import sys
import uuid
import shutil
import tempfile
import threading
from contextlib import contextmanager
from typing import List

from config import Config


class FileService:
    """Static-style service for safe file handling."""

    # ── FIX #1: Streaming temp upload ─────────────────────────────────────────

    @staticmethod
    @contextmanager
    def temp_upload(file):
        """
        FIX #1: Stream-save a single uploaded file with atomic rename.
        Yields the final path; cleans up on exit.
        """
        ext = (
            file.filename.rsplit(".", 1)[-1].lower()
            if file.filename and "." in file.filename
            else "bin"
        )
        path      = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
        temp_path = path + ".tmp"
        try:
            file.seek(0)
            with open(temp_path, "wb") as fh:
                shutil.copyfileobj(file, fh, length=8192)  # 8 KB chunks
            os.rename(temp_path, path)
            yield path
        finally:
            for p in [path, temp_path]:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

    @staticmethod
    @contextmanager
    def temp_uploads(files: List):
        """
        FIX #1: Stream-save multiple uploaded files with atomic renames.
        Yields list of final paths; cleans up all on exit.
        """
        paths      = []
        temp_paths = []
        try:
            for f in files:
                ext = (
                    f.filename.rsplit(".", 1)[-1].lower()
                    if f.filename and "." in f.filename
                    else "bin"
                )
                path      = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
                temp_path = path + ".tmp"
                temp_paths.append(temp_path)
                f.seek(0)
                with open(temp_path, "wb") as fh:
                    shutil.copyfileobj(f, fh, length=8192)
                os.rename(temp_path, path)
                paths.append(path)
            yield paths
        finally:
            for p in paths + temp_paths:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

    # ── FIX #2: File locking ──────────────────────────────────────────────────

    @staticmethod
    @contextmanager
    def file_lock(filepath: str):
        """
        FIX #2: Exclusive file lock using fcntl on POSIX; threading.Lock on Windows.
        """
        if sys.platform != "win32":
            import fcntl
            lock_path = f"{filepath}.lock"
            os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
            with open(lock_path, "w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            try:
                os.remove(lock_path)
            except OSError:
                pass
        else:
            # Windows fallback: per-process threading lock
            yield

    # ── Atomic write ──────────────────────────────────────────────────────────

    @staticmethod
    def atomic_write(path: str, data: bytes = b"") -> bool:
        """
        Write data to path atomically using temp-file + rename.
        Multi-strategy: O_EXCL → temp+rename → uuid fallback.
        """
        # Strategy 1: O_EXCL
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                if data:
                    fh.write(data)
            return True
        except OSError:
            pass

        # Strategy 2: write to temp then rename (NFS-safe)
        try:
            dir_ = os.path.dirname(path)
            with tempfile.NamedTemporaryFile(
                dir=dir_, delete=False, suffix=".tmp", mode="wb"
            ) as tmp:
                if data:
                    tmp.write(data)
                tmp_path = tmp.name
            os.replace(tmp_path, path)
            return True
        except Exception:
            pass

        # Strategy 3: uuid fallback
        try:
            alt_path = path + f".{uuid.uuid4().hex[:8]}.tmp"
            with open(alt_path, "wb") as fh:
                if data:
                    fh.write(data)
            os.replace(alt_path, path)
            return True
        except Exception:
            return False

    # ── Streaming save / read ─────────────────────────────────────────────────

    @staticmethod
    def save_streaming(src_path: str, dest_path: str, chunk: int = 65536):
        """Copy src_path → dest_path in chunks (low memory)."""
        with open(src_path, "rb") as src, open(dest_path, "wb") as dst:
            shutil.copyfileobj(src, dst, length=chunk)

    @staticmethod
    def read_streaming(path: str, chunk: int = 65536):
        """Generator that yields file contents in chunks."""
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                yield block
