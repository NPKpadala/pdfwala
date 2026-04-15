"""
PDFWala V10.0
services/storage_service.py — FIX #3: Storage abstraction layer.
LocalStorage is the default; swap for S3Storage etc. by changing get_storage().
"""

import os
import shutil
from abc import ABC, abstractmethod

from config import Config
from services.file_service import FileService
from utils.security import generate_signed_url


class StorageBackend(ABC):
    """Abstract storage backend."""

    @abstractmethod
    def save(self, source_path: str, filename: str) -> str:
        """Copy source_path to permanent storage. Returns final path."""

    @abstractmethod
    def get_url(self, path: str, expiry: int = None) -> str:
        """Return a (possibly signed) download URL for path."""

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return True if the file exists in storage."""

    @abstractmethod
    def delete(self, path: str):
        """Delete a file from storage."""


class LocalStorage(StorageBackend):
    """Default local-disk storage backend."""

    def save(self, source_path: str, filename: str) -> str:
        dest = os.path.join(Config.OUTPUT_FOLDER, filename)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with FileService.file_lock(dest):
            shutil.copy2(source_path, dest)
        return dest

    def get_url(self, path: str, expiry: int = None) -> str:
        return generate_signed_url(path, expiry)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def delete(self, path: str):
        try:
            os.remove(path)
        except OSError:
            pass


# ── Factory ────────────────────────────────────────────────────────────────────

def get_storage() -> StorageBackend:
    """
    Return the configured storage backend.
    Override STORAGE_BACKEND env var to plug in alternatives (e.g. 's3').
    """
    backend = os.environ.get("STORAGE_BACKEND", "local").lower()
    if backend == "local":
        return LocalStorage()
    raise ValueError(f"Unknown STORAGE_BACKEND: {backend}")
