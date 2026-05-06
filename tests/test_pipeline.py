"""tests/test_pipeline.py"""
import pytest
from unittest.mock import patch, MagicMock
from core.pipeline import Pipeline, register, get_engine
from core.context import JobContext
from core.exceptions import ProcessingError


def test_register_and_get_engine():
    @register("_test_op_123")
    def _fake(ctx): return {"ok": True}
    fn = get_engine("_test_op_123")
    assert fn is _fake


def test_get_engine_missing():
    with pytest.raises(ProcessingError):
        get_engine("__nonexistent_op__")


def test_pipeline_run_success(tmp_path):
    out = str(tmp_path / "out.pdf")

    @register("_test_run")
    def _engine(ctx):
        open(ctx.output_path, "wb").write(b"fake")
        return {"pages": 1}

    ctx = JobContext()
    ctx.operation   = "_test_run"
    ctx.input_path  = __file__
    ctx.output_path = out

    with patch("core.pipeline.redis_service") as mock_redis:
        mock_redis.job_set = MagicMock()
        result = Pipeline.run(ctx)

    assert result.status == "completed"
    assert result.result.get("pages") == 1


def test_pipeline_run_failure(tmp_path):
    @register("_test_fail")
    def _bad(ctx):
        raise ProcessingError("deliberate failure")

    ctx = JobContext()
    ctx.operation   = "_test_fail"
    ctx.input_path  = __file__
    ctx.output_path = str(tmp_path / "out.pdf")

    with patch("core.pipeline.redis_service"):
        with pytest.raises(ProcessingError):
            Pipeline.run(ctx)
