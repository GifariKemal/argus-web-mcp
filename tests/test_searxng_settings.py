"""Structural guard for deploy/searxng/settings.yml.

A malformed edit here does not fail any import or unit test - it fails at runtime, in
production, as an HTTP 500 from SearXNG. This happened once: a comment block landed
inside `use_default_settings`, which pushed `engines:` to the top level as a mapping,
left `use_default_settings` empty, and stopped the upstream defaults from merging
(`KeyError: 'default_doi_resolver'` on every search). YAML hid it, because a duplicate
top-level key is not an error - the last one silently wins.
"""

import collections
import pathlib

import yaml

SETTINGS = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "searxng" / "settings.yml"


def _raw() -> str:
    return open(SETTINGS, encoding="utf-8").read()


def test_no_duplicate_top_level_keys():
    keys = [
        line.split(":")[0]
        for line in _raw().splitlines()
        if line and not line[0].isspace() and not line.startswith("#") and ":" in line
    ]
    dupes = [k for k, n in collections.Counter(keys).items() if n > 1]
    assert not dupes, f"duplicate top-level keys silently override each other: {dupes}"


def test_defaults_are_merged_and_onion_engines_removed():
    cfg = yaml.safe_load(_raw())
    uds = cfg["use_default_settings"]
    # Either form is valid upstream; what matters is that defaults still merge.
    assert uds is True or isinstance(uds, dict), f"use_default_settings is {uds!r}"
    if isinstance(uds, dict):
        assert uds.get("engines", {}).get("remove") == ["ahmia", "torch"]


def test_json_format_stays_enabled():
    # Argus calls SearXNG with ?format=json; without this the whole search tool is dead.
    assert "json" in yaml.safe_load(_raw())["search"]["formats"]


def test_engine_list_is_well_formed():
    for engine in yaml.safe_load(_raw())["engines"]:
        assert "name" in engine and isinstance(engine.get("disabled"), bool), engine
