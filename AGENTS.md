<div align="center">

# AGENTS.md - working on Argus

<img src="https://img.shields.io/badge/tests-753_offline-3fb950?style=flat-square&logo=pytest&logoColor=white" alt=""/>
<img src="https://img.shields.io/badge/SSRF-100%25_required-16a34a?style=flat-square" alt=""/>
<img src="https://img.shields.io/badge/lint-ruff_clean-d29922?style=flat-square" alt=""/>
<img src="https://img.shields.io/badge/style-Ponytail_minimal-0ea5e9?style=flat-square" alt=""/>

<sub>Conventions &amp; commands for any AI agent (Claude Code, Codex, ...) editing this repo.</sub>

</div>

---

> Read [`SOUL.md`](SOUL.md) for *why*, [`docs/00-DESIGN.md`](docs/00-DESIGN.md) for the architecture, [`docs/02-ROADMAP.md`](docs/02-ROADMAP.md) for the plan. This file is *how to work here*.

## Contents

- [Setup](#setup)
- [Commands](#commands)
- [Hard gates - never compromise](#hard-gates---never-compromise)
- [How to build a change](#how-to-build-a-change)
- [Layout & conventions](#layout--conventions)
- [Secrets & deploy](#secrets--deploy)
- [Don't](#dont)

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"          # base + test/lint deps
# optional extras only when touching those tiers:
# ".[semantic]"     -> fastembed (find_similar, hybrid rerank)
# ".[pdf-quality]"  -> docling (read_pdf mode='quality')
crawl4ai-setup && crawl4ai-doctor   # one-time Chromium for the browser tier
```
Use the venv Python explicitly (Windows): `./.venv/Scripts/python.exe`, `./.venv/Scripts/ruff.exe`.

## Commands

| Task | Command |
|---|---|
| Fast offline suite (default) | `./.venv/Scripts/python.exe -m pytest -q -m 'not browser and not slow and not network'` |
| SSRF hard gate (must be 100%) | `pytest tests/test_ssrf.py --cov=argus.security.ssrf --cov-branch --cov-fail-under=100 -q` |
| Browser tier (real Chromium) | `pytest -q -m browser` |
| Slow (Docling) | `pytest -q -m slow` |
| Lint | `./.venv/Scripts/ruff.exe check src tests benchmark` |
| Run (stdio) | `python -m argus.server` |
| Run (HTTP) | `uvicorn argus.server:app --host 127.0.0.1 --port 8090` |
| SearXNG backend | `cd deploy/searxng && docker compose up -d` |

Test markers: `browser` (needs Chromium), `slow` (Docling model), `network` (live internet). The default run excludes all three and is fully offline + deterministic.

## Hard gates - never compromise

1. **SSRF coverage = 100%** on `argus.security.ssrf` (line + branch). Every outbound fetch of a user-influenced URL goes through `validate_url` + the IP-pinning safe client / `_guard`.
2. **Tools never raise to the client** - they return a structured `err(code, msg, detail)` dict (`argus.models`). Every server tool has a final `except Exception` -> `err(...)`.
3. **No silent truncation** of content. Size caps must error, not quietly cut.
4. **Trading parsers >=99% field accuracy** (golden-file) before any live Aurix use.
5. **ruff clean** + the offline suite green before claiming done.

## How to build a change

- **TDD.** Write the failing test, see it fail, implement minimal, green. New non-trivial logic leaves a runnable check behind.
- **Ponytail (minimal code).** stdlib -> native -> installed dep -> one line -> minimal new code. No speculative abstractions. But never trim validation / SSRF / error handling.
- **Per-component loop:** write -> syntax-check -> test -> validate, then move on. No one giant turn.
- **Multi-agent for speed:** fan out independent modules (disjoint files) to parallel agents; the orchestrator integrates `server.py` and verifies.
- **Commit + update status** (`README.md`, `CLAUDE.md`, `docs/02-ROADMAP.md`, `CHANGELOG.md`) after each milestone.

## Layout & conventions

```
src/argus/
  server.py            # FastMCP app + the 20 tools + lifespan + /health + /metrics + auth
  models.py            # err() structured-error helper + ERROR_CODES
  security/ssrf.py     # the trust boundary (100% covered)
  fetch/               # static.py / render.py / core.py / crawl.py / fallback.py / throttle.py
  extract/             # article.py / pdf.py / structured.py / llm.py / links.py
  search.py / scholar.py / gh_search.py / router.py / semantic.py / cache.py / watch.py
  trading/             # forexfactory.py / cot.py / news.py
tests/                 # mirror per module; conftest.py = offline MockTransport fixture-server
```

- New tool -> add the async fn in `server.py`, wrap all failures in `err(...)`, register it in the `mcp.tool(_fn)` loop, add it to `INSTRUCTIONS` (keep **< 2 KB**), and update the tool-count test.
- External APIs (GitHub, Semantic Scholar, ...) go through the SSRF-safe `s.client`; add a `User-Agent`.
- Optional/heavy features are **lazy + default-off** (LLM via `ARGUS_ENABLE_LLM`, semantic via the `[semantic]` extra). Argus must stay fully functional with none of them.

## Secrets & deploy

- Manage VPS secrets via SSH/scp directly - **not** via Hermes tools (`redact_secrets` masks them).
- Never commit a real `secret_key`/token. `*.bak` and benchmark run-artifacts are gitignored.
- **Already live** at `https://argus.gifariksuryo.xyz/mcp` (`103.172.172.29`). A systemd timer auto-deploys `main`: poll every 5 min -> fast-forward only -> restart -> `/health` gate -> auto-rollback (docs/benchmark-only commits skip the restart). So a merged change to `main` ships itself - keep `main` green. Runbook: [`deploy/README.md`](deploy/README.md).

## Don't

- Don't add a paid web API to "fix" search - Argus is self-hosted by design (SearXNG + proxy).
- Don't make Argus depend on an LLM - the consuming agent is the brain.
- Don't leave TODO/placeholder without a logged note. Don't weaken a hard gate "temporarily".
