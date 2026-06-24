import pytest

from argus.models import err


def test_err_shape():
    assert err("ssrf_blocked", "blocked", "127.0.0.1") == {
        "error": "blocked",
        "code": "ssrf_blocked",
        "detail": "127.0.0.1",
    }


def test_err_rejects_unknown_code():
    with pytest.raises(ValueError):
        err("bogus_code", "x")
