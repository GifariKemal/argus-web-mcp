import pytest

from argus.models import STAGE_COUNTS, err, record_stage


def test_record_stage_increments():
    STAGE_COUNTS.pop("test.stage", None)
    record_stage("test.stage")
    record_stage("test.stage")
    assert STAGE_COUNTS["test.stage"] == 2


def test_err_shape():
    assert err("ssrf_blocked", "blocked", "127.0.0.1") == {
        "error": "blocked",
        "code": "ssrf_blocked",
        "detail": "127.0.0.1",
    }


def test_err_rejects_unknown_code():
    with pytest.raises(ValueError):
        err("bogus_code", "x")
