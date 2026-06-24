"""Tests for argus.extract.llm - LLM structured extraction (instructor + OpenAI-compatible).

All tests are offline: the instructor/openai client is fully mocked. No real API call.
"""

from __future__ import annotations

import logging
from typing import get_args, get_origin
from unittest.mock import AsyncMock, MagicMock

import pytest

from argus.extract.llm import (
    LLM_CONTENT_BUDGET,
    LLMUnavailable,
    _build_model,
    _config,
    extract_llm,
    llm_available,
)

# --- env hygiene: never leak a real key into these tests ----------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ARGUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ARGUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ARGUS_LLM_MODEL", raising=False)


def _fake_client(return_value=None, side_effect=None):
    """Build a mock instructor async client: client.chat.completions.create is AsyncMock."""
    client = MagicMock()
    create = AsyncMock(return_value=return_value, side_effect=side_effect)
    client.chat.completions.create = create
    return client, create


# --- _build_model -------------------------------------------------------------


def test_build_model_optional_fields_and_types():
    Model = _build_model({"title": "str", "price": "float", "tags": "list"})
    # instantiable with no args (all Optional = None)
    inst = Model()
    assert inst.title is None
    assert inst.price is None
    assert inst.tags is None

    # accepts valid populated values
    populated = Model(title="x", price=1.5, tags=["a", "b"])
    assert populated.title == "x"
    assert populated.price == 1.5
    assert populated.tags == ["a", "b"]


def test_build_model_unknown_type_falls_back_to_str():
    Model = _build_model({"weird": "complex128", "n": "number"})
    inst = Model(weird="hello", n=3.2)
    assert inst.weird == "hello"
    assert inst.n == 3.2  # 'number' -> float
    # None allowed (Optional)
    assert Model().weird is None


def test_build_model_case_insensitive():
    Model = _build_model({"a": "INT", "b": "Bool", "c": "FLOAT"})
    inst = Model(a=5, b=True, c=2.0)
    assert inst.a == 5
    assert inst.b is True
    assert inst.c == 2.0


def test_build_model_empty_schema():
    Model = _build_model({})
    inst = Model()
    assert inst.model_dump() == {}


def test_build_model_fields_are_optional():
    Model = _build_model({"x": "int"})
    ann = Model.model_fields["x"].annotation
    # field annotation is int | None
    assert type(None) in get_args(ann)
    assert int in get_args(ann)
    assert get_origin(int | None) is get_origin(ann)


# --- _config ------------------------------------------------------------------


def test_config_defaults_and_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = _config()
    assert cfg["api_key"] == "openai-key"
    assert cfg["base_url"] is None
    assert cfg["model"] == "gpt-4o-mini"


def test_config_argus_overrides_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ARGUS_LLM_API_KEY", "argus-key")
    monkeypatch.setenv("ARGUS_LLM_BASE_URL", "https://self-hosted/v1")
    monkeypatch.setenv("ARGUS_LLM_MODEL", "llama-3.3-70b")
    cfg = _config()
    assert cfg["api_key"] == "argus-key"
    assert cfg["base_url"] == "https://self-hosted/v1"
    assert cfg["model"] == "llama-3.3-70b"


# --- llm_available ------------------------------------------------------------


def test_llm_available_true_with_argus_key(monkeypatch):
    monkeypatch.setenv("ARGUS_ENABLE_LLM", "1")  # opt-in required (Argus is no-LLM by default)
    monkeypatch.setenv("ARGUS_LLM_API_KEY", "k")
    assert llm_available() is True


def test_llm_available_true_with_openai_key(monkeypatch):
    monkeypatch.setenv("ARGUS_ENABLE_LLM", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert llm_available() is True


def test_llm_available_false_when_unset():
    assert llm_available() is False


def test_llm_available_false_when_key_but_not_enabled(monkeypatch):
    # Key present but ARGUS_ENABLE_LLM unset -> LLM tier stays OFF (agent brings the LLM).
    monkeypatch.delenv("ARGUS_ENABLE_LLM", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert llm_available() is False


def test_llm_available_false_when_empty(monkeypatch):
    monkeypatch.setenv("ARGUS_ENABLE_LLM", "1")
    monkeypatch.setenv("ARGUS_LLM_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert llm_available() is False


# --- extract_llm: no key, no client -> raise ----------------------------------


@pytest.mark.asyncio
async def test_extract_llm_raises_when_unavailable_and_no_client():
    with pytest.raises(LLMUnavailable):
        await extract_llm("content", {"title": "str"})


# --- extract_llm: happy path --------------------------------------------------


@pytest.mark.asyncio
async def test_extract_llm_happy_path():
    schema = {"title": "str", "price": "float"}
    Model = _build_model(schema)
    populated = Model(title="Widget", price=9.99)
    client, create = _fake_client(return_value=populated)

    res = await extract_llm("some page text", schema, client=client)

    assert res["valid"] is True
    assert res["data"] == {"title": "Widget", "price": 9.99}

    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    # response_model is a dynamic model with the same fields as our schema
    assert set(kwargs["response_model"].model_fields) == set(schema)
    # messages: system + user
    roles = [m["role"] for m in kwargs["messages"]]
    assert roles == ["system", "user"]
    assert "some page text" in kwargs["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_extract_llm_custom_prompt_used_as_system():
    schema = {"x": "str"}
    Model = _build_model(schema)
    client, create = _fake_client(return_value=Model(x="v"))

    await extract_llm("c", schema, prompt="Extract the X field only.", client=client)

    msgs = create.await_args.kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "Extract the X field only."


# --- extract_llm: truncation --------------------------------------------------


@pytest.mark.asyncio
async def test_extract_llm_truncates_oversized_content(caplog):
    schema = {"x": "str"}
    Model = _build_model(schema)
    client, create = _fake_client(return_value=Model(x="v"))

    big = "A" * (LLM_CONTENT_BUDGET + 5000)
    with caplog.at_level(logging.WARNING, logger="argus.extract.llm"):
        await extract_llm(big, schema, client=client)

    user_content = create.await_args.kwargs["messages"][-1]["content"]
    assert len(user_content) == LLM_CONTENT_BUDGET
    # truncation is logged, not silent
    assert any("truncat" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_extract_llm_no_truncation_under_budget(caplog):
    schema = {"x": "str"}
    Model = _build_model(schema)
    client, create = _fake_client(return_value=Model(x="v"))

    small = "B" * 100
    with caplog.at_level(logging.WARNING, logger="argus.extract.llm"):
        await extract_llm(small, schema, client=client)

    assert create.await_args.kwargs["messages"][-1]["content"] == small
    assert not any("truncat" in r.message.lower() for r in caplog.records)


# --- extract_llm: failures -> valid False -------------------------------------


@pytest.mark.asyncio
async def test_extract_llm_create_raises_returns_invalid():
    schema = {"x": "str"}
    client, _ = _fake_client(side_effect=ValueError("validation/parse failed"))

    res = await extract_llm("c", schema, client=client)
    assert res == {"data": {}, "valid": False}


@pytest.mark.asyncio
async def test_extract_llm_create_returns_non_model_returns_invalid():
    schema = {"x": "str"}
    client, _ = _fake_client(return_value="not a pydantic model")

    res = await extract_llm("c", schema, client=client)
    assert res["valid"] is False
    assert res["data"] == {}
