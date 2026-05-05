"""tests/test_validators.py"""
import os
import pytest
from core.validators import (
    validate_file_size, validate_extension, validate_magic_bytes,
    validate_input_path,
)
from core.exceptions import ValidationError


def test_validate_file_size_ok(tmp_path):
    f = tmp_path / "test.pdf"
    f.write_bytes(b"x" * 1024)
    assert validate_file_size(str(f)) == 1024


def test_validate_file_size_too_large(tmp_path):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x" * 1024)
    with pytest.raises(ValidationError, match="too large"):
        validate_file_size(str(f), max_bytes=100)


def test_validate_extension_ok(tmp_path):
    f = tmp_path / "test.pdf"
    f.write_bytes(b"dummy")
    assert validate_extension(str(f), "pdf") == ".pdf"


def test_validate_extension_bad(tmp_path):
    f = tmp_path / "test.exe"
    f.write_bytes(b"dummy")
    with pytest.raises(ValidationError):
        validate_extension(str(f), "pdf")


def test_validate_magic_bytes_ok(tmp_path):
    f = tmp_path / "test.pdf"
    f.write_bytes(b"%PDF-1.4 rest of content")
    assert validate_magic_bytes(str(f), "pdf") is True


def test_validate_magic_bytes_fail(tmp_path):
    f = tmp_path / "fake.pdf"
    f.write_bytes(b"not a pdf at all")
    with pytest.raises(ValidationError, match="content does not match"):
        validate_magic_bytes(str(f), "pdf")


def test_validate_input_path_missing():
    with pytest.raises(ValidationError):
        validate_input_path("/nonexistent/file.pdf")
