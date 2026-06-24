"""Schema-driven structured extraction via parsel (CSS + XPath)."""

from __future__ import annotations

from typing import Any

from parsel import Selector


def _is_xpath(selector: str) -> bool:
    s = selector.lstrip()
    return s.startswith("//") or s.startswith("(")


def _query(sel: Selector, selector: str):
    return sel.xpath(selector) if _is_xpath(selector) else sel.css(selector)


def _extract_one(node, attr: str | None) -> str | None:
    if attr is None:
        # When XPath selects text()/@attr the node wraps a bare str; otherwise it
        # wraps an element and we want its full descendant text.
        val = node.get() if isinstance(node.root, str) else node.xpath("string(.)").get()
    elif isinstance(node.root, str):
        # text node has no attributes
        val = None
    else:
        val = node.attrib.get(attr)
    if val is None:
        return None
    val = val.strip()
    return val or None


def _normalise_spec(spec: str | dict) -> dict:
    if isinstance(spec, str):
        return {"selector": spec, "attr": None, "many": False, "required": True}
    return {
        "selector": spec["selector"],
        "attr": spec.get("attr"),
        "many": spec.get("many", False),
        "required": spec.get("required", True),
    }


def extract_selectors(html: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Extract ``field -> value`` from ``html`` per ``schema``.

    A spec is a CSS string, or a dict ``{"selector", "attr", "many", "required"}``.
    Selectors starting with ``//`` or ``(`` are treated as XPath, otherwise CSS.
    Returns ``{"data": {...}, "valid": bool}``; ``valid`` is False when any
    required field is missing/None/empty (or an empty list for ``many``).
    """
    sel = Selector(text=html)
    data: dict[str, Any] = {}
    valid = True

    for field, raw in schema.items():
        spec = _normalise_spec(raw)
        nodes = _query(sel, spec["selector"])

        if spec["many"]:
            values = [v for n in nodes if (v := _extract_one(n, spec["attr"])) is not None]
            data[field] = values
            if spec["required"] and not values:
                valid = False
        else:
            value = _extract_one(nodes[0], spec["attr"]) if nodes else None
            data[field] = value
            if spec["required"] and not value:
                valid = False

    return {"data": data, "valid": valid}
