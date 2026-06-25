"""Tests for argus.extract.structured - parsel CSS/XPath selector extraction."""

from argus.extract.structured import extract_selectors

HTML = (
    "<h1>Title</h1>"
    "<span class='price' data-v='9.99'>$9.99</span>"
    "<ul><li>a</li><li>b</li></ul>"
)


def test_basic_css_attr_and_many():
    schema = {
        "title": "h1",
        "price": {"selector": ".price", "attr": "data-v"},
        "items": {"selector": "li", "many": True},
    }
    res = extract_selectors(HTML, schema)
    assert res["valid"] is True
    assert res["data"]["title"] == "Title"
    assert res["data"]["price"] == "9.99"
    assert res["data"]["items"] == ["a", "b"]


def test_missing_required_field_invalid():
    schema = {
        "title": "h1",
        "missing": {"selector": ".does-not-exist"},
    }
    res = extract_selectors(HTML, schema)
    assert res["data"]["title"] == "Title"
    assert res["data"]["missing"] is None
    assert res["valid"] is False


def test_missing_optional_field_still_valid():
    schema = {
        "title": "h1",
        "subtitle": {"selector": ".nope", "required": False},
    }
    res = extract_selectors(HTML, schema)
    assert res["data"]["subtitle"] is None
    assert res["valid"] is True


def test_xpath_selector():
    schema = {"title": "//h1/text()"}
    res = extract_selectors(HTML, schema)
    assert res["data"]["title"] == "Title"
    assert res["valid"] is True


def test_xpath_paren_form():
    schema = {"first_item": "(//li)[1]/text()"}
    res = extract_selectors(HTML, schema)
    assert res["data"]["first_item"] == "a"
    assert res["valid"] is True


def test_xpath_many_attr():
    html = "<a href='/x'>X</a><a href='/y'>Y</a>"
    schema = {"links": {"selector": "//a", "attr": "href", "many": True}}
    res = extract_selectors(html, schema)
    assert res["data"]["links"] == ["/x", "/y"]
    assert res["valid"] is True


def test_text_attr_none_default():
    schema = {"price_text": {"selector": ".price"}}
    res = extract_selectors(HTML, schema)
    assert res["data"]["price_text"] == "$9.99"


def test_empty_string_text_treated_as_missing():
    html = "<span class='empty'></span>"
    schema = {"e": {"selector": ".empty"}}
    res = extract_selectors(html, schema)
    assert res["data"]["e"] is None
    assert res["valid"] is False


def test_many_empty_list_when_required_is_invalid():
    schema = {"items": {"selector": ".no-match", "many": True}}
    res = extract_selectors(HTML, schema)
    assert res["data"]["items"] == []
    assert res["valid"] is False


def test_many_empty_list_optional_valid():
    schema = {"items": {"selector": ".no-match", "many": True, "required": False}}
    res = extract_selectors(HTML, schema)
    assert res["data"]["items"] == []
    assert res["valid"] is True


def test_xpath_text_node_with_attr_returns_none():
    # XPath text() yields a bare str node; requesting an attr on a text node has no
    # attribute -> value is None (and the required field is therefore invalid).
    schema = {"t": {"selector": "//h1/text()", "attr": "data-x"}}
    res = extract_selectors(HTML, schema)
    assert res["data"]["t"] is None
    assert res["valid"] is False


def test_xpath_text_node_with_attr_optional_still_valid():
    schema = {"t": {"selector": "//h1/text()", "attr": "data-x", "required": False}}
    res = extract_selectors(HTML, schema)
    assert res["data"]["t"] is None
    assert res["valid"] is True


def test_return_shape():
    res = extract_selectors(HTML, {"title": "h1"})
    assert set(res.keys()) == {"data", "valid"}
