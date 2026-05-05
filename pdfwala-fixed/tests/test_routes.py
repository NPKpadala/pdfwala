"""tests/test_routes.py"""
import io
import pytest
from unittest.mock import patch, MagicMock


def test_health(client):
    with patch("app.routes.system_routes.redis_service") as mock_r:
        mock_r.ping.return_value = True
        resp = client.get("/health")
    assert resp.status_code in (200, 503)
    data = resp.get_json()
    assert "status" in data


def test_ready(client):
    resp = client.get("/ready")
    assert resp.status_code == 200


def test_job_not_found(client):
    with patch("app.routes.system_routes.redis_service") as mock_r:
        mock_r.job_get.return_value = None
        resp = client.get("/jobs/nonexistent-job-id")
    assert resp.status_code == 404


def test_compress_pdf_no_file(client):
    resp = client.post("/api/pdf/compress")
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_download_invalid_filename(client):
    resp = client.get("/download/../etc/passwd")
    assert resp.status_code in (400, 404)
