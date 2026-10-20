# SPDX-License-Identifier: AGPL-3.0-or-later
"""Upstream brave engine, minus the two things Brave now rejects from our exit IPs.

Brave answers 429 from the VPS IP since 2026-10-19. Through WARP it serves results,
except to (a) any curl_cffi browser TLS fingerprint - upstream brave never sets
``impersonate``, so it gets the ``chrome`` default - and (b) the full cookie set
brave.py sends: with ``ui_lang`` among them it is 429 every time, without it 200 every
time (4x4 probes each, 2026-10-20). This shim runs brave.py unchanged and only undoes
those two; ``ui_lang`` just picks Brave's UI language, results are unaffected.
"""
# ponytail: exec, not import, so settings.yml attributes land on the globals brave's
# functions read. Delete this file once upstream brave works from WARP again.
import os

with open(os.path.join(os.path.dirname(__file__), "brave.py"), encoding="utf-8") as f:
    exec(f.read())  # noqa: S102

_brave_request = request  # noqa: F821  # pyright: ignore[reportUndefinedVariable] - from the exec


def request(query, params):
    _brave_request(query, params)
    params["impersonate"] = "none"
    params["cookies"].pop("ui_lang", None)
    return params
