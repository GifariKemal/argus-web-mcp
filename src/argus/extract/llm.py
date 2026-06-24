"""LLM structured extraction tier (instructor + any OpenAI-compatible endpoint).

Provider-agnostic: configured purely via env so the same code can point at a
self-hosted vLLM/Groq/NVIDIA endpoint (owner's target) or OpenAI (dev default).
Uses ``instructor.from_openai(openai.AsyncOpenAI(...))`` so a single async
``client.chat.completions.create(model=..., response_model=Model, messages=[...])``
returns a validated pydantic instance.

Schema is a simple ``field -> type-name`` map (str/int/float/bool/list/number);
fields are built Optional so partial extraction still validates.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel, create_model

logger = logging.getLogger(__name__)

# Context budget (chars) to avoid blowing the model window. Truncation is LOGGED.
LLM_CONTENT_BUDGET = 24000

_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_PROMPT = (
    "You are a precise web-content extraction engine. Extract the requested "
    "fields from the provided content and return them as structured data. "
    "Use null for any field that is absent; never invent values."
)

# Schema type-name -> python type. Unknown names fall back to str.
_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "number": float,
}


class LLMUnavailable(Exception):
    """Raised when no LLM API key is configured and no client was injected."""


def llm_available() -> bool:
    """True only when Argus's OPTIONAL LLM tier is explicitly enabled AND a key is configured.

    Argus is tools-not-brain: the consuming agent (Claude Code's Opus / Codex) does synthesis
    from `research(deep)` raw-content bundles - Argus needs NO LLM by default. The LLM tier
    (research `answer` mode, extract_structured `llm`/`auto`-fallback) is opt-in for non-agent
    consumers: set ARGUS_ENABLE_LLM=1 plus a key (ARGUS_LLM_API_KEY/OPENAI_API_KEY) and optional
    ARGUS_LLM_BASE_URL. Mere key presence does NOT enable it (avoids wasteful/failing LLM calls
    when the agent already reasons).
    """
    enabled = os.getenv("ARGUS_ENABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
    return enabled and bool(os.getenv("ARGUS_LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))


def _config() -> dict:
    """Read env config used to build the AsyncOpenAI client.

    api_key:  ARGUS_LLM_API_KEY, else OPENAI_API_KEY.
    base_url: ARGUS_LLM_BASE_URL, else None (OpenAI default).
    model:    ARGUS_LLM_MODEL, else 'gpt-4o-mini'.
    """
    return {
        "api_key": os.getenv("ARGUS_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
        "base_url": os.getenv("ARGUS_LLM_BASE_URL") or None,
        "model": os.getenv("ARGUS_LLM_MODEL") or _DEFAULT_MODEL,
    }


def _build_model(schema: dict) -> type[BaseModel]:
    """Build a dynamic pydantic model from a ``field -> type-name`` map.

    Type names are case-insensitive; unknown names fall back to ``str``. Every
    field is ``Optional[T]`` defaulting to ``None`` so partial extraction validates.
    """
    fields: dict[str, Any] = {}
    for field, type_name in schema.items():
        py_type = _TYPE_MAP.get(str(type_name).lower(), str)
        fields[field] = (py_type | None, None)
    return create_model("ExtractionModel", **fields)


def _build_client():
    """Build an instructor-wrapped AsyncOpenAI client from env config."""
    import instructor
    import openai

    cfg = _config()
    openai_client = openai.AsyncOpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    return instructor.from_openai(openai_client)


async def extract_llm(
    content: str,
    schema: dict,
    prompt: str | None = None,
    client=None,
) -> dict:
    """Extract ``field -> value`` from ``content`` using an LLM constrained by ``schema``.

    Returns ``{"data": {...}, "valid": True}`` on success, or ``{"data": {}, "valid": False}``
    on any validation/parse failure. Raises :class:`LLMUnavailable` when no key is
    configured and no ``client`` is injected. Content over ``LLM_CONTENT_BUDGET`` chars
    is truncated (logged as a warning, never silent). ``client`` lets tests inject a mock.
    """
    if client is None:
        if not llm_available():
            raise LLMUnavailable(
                "No LLM API key configured (set ARGUS_LLM_API_KEY or OPENAI_API_KEY)."
            )
        client = _build_client()

    if len(content) > LLM_CONTENT_BUDGET:
        logger.warning(
            "Content truncated from %d to %d chars for LLM extraction.",
            len(content),
            LLM_CONTENT_BUDGET,
        )
        content = content[:LLM_CONTENT_BUDGET]

    model_cls = _build_model(schema)
    cfg = _config()

    try:
        result = await client.chat.completions.create(
            model=cfg["model"],
            response_model=model_cls,
            messages=[
                {"role": "system", "content": prompt or _DEFAULT_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        if not isinstance(result, BaseModel):
            logger.warning("LLM extraction returned a non-model result: %r", type(result))
            return {"data": {}, "valid": False}
        return {"data": result.model_dump(), "valid": True}
    except Exception as exc:  # noqa: BLE001 - any provider/validation error -> invalid result
        logger.warning("LLM extraction failed: %s", exc)
        return {"data": {}, "valid": False}
