"""Argus 4-way comparison harness: Claude vs Codex, WITH vs WITHOUT Argus.

Measures token consumption + answer quality of one web-research task across four
conditions, to answer "how do Claude/Codex compare WITH vs WITHOUT Argus?":

  A `claude-native` - Claude Code CLI native WebSearch/WebFetch, NO Argus MCP.
  B `claude-argus`  - Claude Code CLI using ONLY the Argus MCP (no native web tools).
  C `codex-native`  - Codex CLI native `web_search=live`, NO Argus MCP.
  D `codex-argus`   - Codex CLI using the Argus MCP.

Scenarios come from `scenarios.COMPARE_IDS` by default (--limit N / --ids a,b,c).

Two kinds of code live here:
  (1) PURE functions (no I/O): token parsing + aggregation + report rendering.
      Unit-tested offline in tests/test_4way_scorer.py.
  (2) I/O runners that shell out to the CLIs. The orchestrator runs these later;
      they capture stdout/stderr, time each run, parse, and never crash the sweep.

Usage:
  python benchmark/run_4way.py run --out out.json [--conditions A,B] [--limit N] \\
      [--ids a,b] [--pace 2.0]
  python benchmark/run_4way.py score --in out.json [--out report.md]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import scenarios as scen_mod  # noqa: E402

DEFAULT_REPORT = BENCH_DIR / "compare-4way-report.md"
_URL_RE = re.compile(r"https?://[^\s<>()\]]+", re.IGNORECASE)
SUBPROCESS_TIMEOUT_S = 240

# Condition labels -> CLI family. Order is the canonical leaderboard order.
CONDITIONS: dict[str, str] = {
    "claude-native": "claude",
    "claude-argus": "claude",
    "codex-native": "codex",
    "codex-argus": "codex",
}

PROMPT_TEMPLATE = (
    'Research this question using web sources: "{query}". '
    "List the top sources you used (title + URL) and give a concise "
    "3-5 sentence synthesis."
)


# --- pure parsers / scoring helpers (unit-tested, no I/O) --------------------


def parse_claude_usage(stdout: str) -> dict:
    """Parse `claude --output-format json` output into a flat usage dict (pure).

    The CLI emits a JSON object with a top-level `usage` block, `total_cost_usd`,
    `result` (the answer text), and `num_turns`. Robust to missing keys / non-JSON
    input: returns Nones with answer_text="".
    """
    base = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "num_turns": None,
        "web_search_requests": None,
        "answer_text": "",
    }
    try:
        obj = json.loads(stdout)
    except (ValueError, TypeError):
        return base
    if not isinstance(obj, dict):
        return base

    usage = obj.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    total = inp + out if isinstance(inp, int) and isinstance(out, int) else None

    server = usage.get("server_tool_use") or {}
    web = server.get("web_search_requests") if isinstance(server, dict) else None

    answer = obj.get("result")
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "total_tokens": total,
        "cost_usd": obj.get("total_cost_usd"),
        "num_turns": obj.get("num_turns"),
        "web_search_requests": web,
        "answer_text": answer if isinstance(answer, str) else "",
    }


def parse_codex_tokens(text: str) -> dict:
    """Parse `codex exec` plaintext output (pure).

    Codex prints a `tokens used` line then a number like `14,375` (commas, possibly
    on the next line); the answer text follows the final `codex` marker line.
    Robust to commas and to the number being inline or on the next line.
    """
    result = {"total_tokens": None, "answer_text": ""}
    if not text:
        return result

    lines = text.splitlines()

    # tokens: find "tokens used" then the first number at/after it.
    for i, line in enumerate(lines):
        if "tokens used" in line.lower():
            # number may be on the same line after the phrase, or on a later line.
            tail = line.lower().split("tokens used", 1)[1]
            candidates = [tail] + lines[i + 1 : i + 3]
            for cand in candidates:
                m = re.search(r"[\d,]+", cand)
                if m and any(ch.isdigit() for ch in m.group(0)):
                    result["total_tokens"] = int(m.group(0).replace(",", ""))
                    break
            break

    # answer text: everything after the last line that is exactly the `codex` marker.
    last_marker = -1
    for i, line in enumerate(lines):
        if line.strip().lower() == "codex":
            last_marker = i
    if last_marker >= 0:
        body = lines[last_marker + 1 :]
        # drop a trailing `tokens used`/number block if it follows the answer.
        cleaned: list[str] = []
        for line in body:
            if "tokens used" in line.lower():
                break
            cleaned.append(line)
        result["answer_text"] = "\n".join(cleaned).strip()
    return result


def count_urls(text: str) -> int:
    """Distinct http(s) URLs in `text` (breadth proxy, pure)."""
    return len({m.group(0).rstrip(".,);") for m in _URL_RE.finditer(text or "")})


def found(text: str) -> bool:
    """A run "found" something: non-empty AND (>=1 URL or answer > 40 chars)."""
    if not text or not text.strip():
        return False
    return count_urls(text) >= 1 or len(text.strip()) > 40


def _mean(values: list) -> float | None:
    """Mean over non-None values; None if there are none."""
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 4) if vals else None


def aggregate_condition(records: list[dict]) -> dict:
    """Aggregate one condition's run records into summary metrics (pure).

    `records` are the per-scenario dicts written by the runners. None token/cost
    values are skipped in means; medians use only present token values.
    """
    n = len(records)
    tokens = [r.get("total_tokens") for r in records]
    present_tokens = [t for t in tokens if t is not None]
    return {
        "n": n,
        "found_count": sum(1 for r in records if r.get("found")),
        "mean_total_tokens": round(statistics.mean(present_tokens), 1)
        if present_tokens
        else None,
        "median_total_tokens": round(statistics.median(present_tokens), 1)
        if present_tokens
        else None,
        "mean_cost_usd": _mean([r.get("cost_usd") for r in records]),
        "mean_latency_s": _mean([r.get("latency_s") for r in records]),
        "mean_urls": _mean([r.get("urls") for r in records]),
        "mean_answer_words": _mean([r.get("words") for r in records]),
    }


def _fmt(value: object) -> str:
    """Render a metric for a markdown cell ('-' for None)."""
    return "-" if value is None else str(value)


def _pct_change(before: float | None, after: float | None) -> str:
    """% change after vs before (e.g. tokens with Argus vs without). '-' if N/A."""
    if before is None or after is None or before == 0:
        return "-"
    return f"{round(100.0 * (after - before) / before, 1)}%"


def _delta(before: float | None, after: float | None) -> str:
    """Absolute delta after-before, rounded; '-' if either is None."""
    if before is None or after is None:
        return "-"
    return str(round(after - before, 2))


def render_report(by_condition: dict[str, dict], rows: list[dict]) -> str:
    """Render the markdown report (pure).

    Sections: (1) per-condition leaderboard, (2) WITH vs WITHOUT Argus delta for
    Claude and Codex separately, (3) per-scenario table.
    """
    parts: list[str] = []
    parts.append("# Argus 4-way comparison - Claude/Codex, WITH vs WITHOUT Argus\n")
    parts.append(
        f"_Conditions: {', '.join(CONDITIONS)}. n="
        f"{by_condition.get(next(iter(CONDITIONS), ''), {}).get('n', 0)} scenarios._\n"
    )

    # (1) Leaderboard.
    parts.append("\n## Per-condition leaderboard\n")
    parts.append(
        "| condition | n | found | mean_tokens | median_tokens | mean_cost_usd | "
        "mean_latency_s | mean_urls | mean_words |\n"
    )
    parts.append("|---|---|---|---|---|---|---|---|---|\n")
    for cond in CONDITIONS:
        a = by_condition.get(cond)
        if not a:
            continue
        parts.append(
            f"| {cond} | {a['n']} | {a['found_count']} | "
            f"{_fmt(a['mean_total_tokens'])} | {_fmt(a['median_total_tokens'])} | "
            f"{_fmt(a['mean_cost_usd'])} | {_fmt(a['mean_latency_s'])} | "
            f"{_fmt(a['mean_urls'])} | {_fmt(a['mean_answer_words'])} |\n"
        )

    # (2) WITH vs WITHOUT Argus deltas, per CLI.
    parts.append("\n## WITH vs WITHOUT Argus\n")
    parts.append(
        "Delta = Argus condition vs native condition for the same CLI "
        "(negative token % = Argus uses fewer tokens).\n\n"
    )
    parts.append(
        "| CLI | native tokens | argus tokens | token change | "
        "url breadth delta | words delta |\n"
    )
    parts.append("|---|---|---|---|---|---|\n")
    for cli, native, argus in (
        ("Claude", "claude-native", "claude-argus"),
        ("Codex", "codex-native", "codex-argus"),
    ):
        nat = by_condition.get(native, {})
        arg = by_condition.get(argus, {})
        nat_tok = nat.get("mean_total_tokens")
        arg_tok = arg.get("mean_total_tokens")
        parts.append(
            f"| {cli} | {_fmt(nat_tok)} | {_fmt(arg_tok)} | "
            f"{_pct_change(nat_tok, arg_tok)} | "
            f"{_delta(nat.get('mean_urls'), arg.get('mean_urls'))} | "
            f"{_delta(nat.get('mean_answer_words'), arg.get('mean_answer_words'))} |\n"
        )

    # (3) Per-scenario table.
    parts.append("\n## Per-scenario\n")
    parts.append("| id | condition | tokens | cost | latency_s | urls | words | found | error |\n")
    parts.append("|---|---|---|---|---|---|---|---|---|\n")
    for r in rows:
        err = r.get("error") or ""
        if len(err) > 40:
            err = err[:37] + "..."
        parts.append(
            f"| {r.get('id')} | {r.get('condition')} | "
            f"{_fmt(r.get('total_tokens'))} | {_fmt(r.get('cost_usd'))} | "
            f"{_fmt(r.get('latency_s'))} | {_fmt(r.get('urls'))} | "
            f"{_fmt(r.get('words'))} | {r.get('found')} | {err} |\n"
        )
    return "".join(parts)


# --- I/O: MCP config + CLI runners (NOT run here; orchestrator runs later) ----


def argus_mcp_config() -> dict:
    """Read ~/.claude.json and return the `mcpServers.argus` block (I/O).

    Walks the config to find an `argus` entry under any `mcpServers` map, e.g.
    {"type":"http","url":...,"headers":{"Authorization":"Bearer ..."}}.
    Raises KeyError if not found. Never logs/commits the token.
    """
    cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))

    def _walk(node: object) -> dict | None:
        if isinstance(node, dict):
            servers = node.get("mcpServers")
            if isinstance(servers, dict) and isinstance(servers.get("argus"), dict):
                return servers["argus"]
            for v in node.values():
                hit = _walk(v)
                if hit is not None:
                    return hit
        return None

    found_cfg = _walk(cfg)
    if found_cfg is None:
        raise KeyError("mcpServers.argus not found in ~/.claude.json")
    return found_cfg


def _argus_url_and_token(argus: dict) -> tuple[str, str | None]:
    """Pull the HTTP url and bearer token out of an argus MCP config block."""
    url = argus.get("url", "")
    token: str | None = None
    auth = (argus.get("headers") or {}).get("Authorization", "")
    if isinstance(auth, str) and auth.lower().startswith("bearer "):
        token = auth[len("bearer ") :].strip()
    return url, token


def _build_command(condition: str, prompt: str, argus: dict | None) -> list[str]:
    """Build the argv for a CLI condition (pure given inputs; no execution).

    Claude conditions write a temp strict mcp-config and run with a fixed
    allowedTools set. Codex conditions pass live web-search or an HTTP MCP block.
    """
    if condition == "claude-argus":
        if argus is None:
            raise ValueError("claude-argus requires the argus MCP config")
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({"mcpServers": {"argus": argus}}, tmp)
        tmp.close()
        return [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--strict-mcp-config",
            "--mcp-config", tmp.name,
            "--allowedTools",
            "mcp__argus__research", "mcp__argus__search", "mcp__argus__read",
        ]
    if condition == "claude-native":
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({"mcpServers": {}}, tmp)
        tmp.close()
        return [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--strict-mcp-config",
            "--mcp-config", tmp.name,
            "--allowedTools", "WebSearch", "WebFetch",
        ]
    if condition == "codex-native":
        return [
            "codex", "exec",
            "-c", "web_search=live",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            prompt,
        ]
    if condition == "codex-argus":
        # NOTE: Codex's HTTP-MCP wiring via -c overrides is version-uncertain. The
        # config keys (mcp_servers.<name>.url / .bearer_token) may differ across
        # codex releases and might require a real ~/.codex/config.toml entry
        # instead. The runner captures stderr and records {error} on failure, so a
        # wiring mismatch degrades gracefully rather than crashing the sweep.
        if argus is None:
            raise ValueError("codex-argus requires the argus MCP config")
        url, token = _argus_url_and_token(argus)
        cmd = [
            "codex", "exec",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "-c", f"mcp_servers.argus.url={url}",
        ]
        if token:
            # Codex streamable_http MCP rejects a literal `bearer_token`; it takes
            # `bearer_token_env_var` (the NAME of an env var). The runner injects
            # ARGUS_TOKEN into the codex-argus subprocess env (never on argv).
            cmd += ["-c", "mcp_servers.argus.bearer_token_env_var=ARGUS_TOKEN"]
        cmd.append(prompt)
        return cmd
    raise ValueError(f"unknown condition: {condition}")


def _run_one(condition: str, scenario: dict, argus: dict | None) -> dict:
    """Execute a single CLI run and return a parsed record (I/O, never raises).

    Records {id, category, query, condition, total_tokens, cost_usd?, latency_s,
    urls, words, found, error?}. Any failure is captured into `error`.
    """
    prompt = PROMPT_TEMPLATE.format(query=scenario["query"])
    rec: dict = {
        "id": scenario["id"],
        "category": scenario["category"],
        "query": scenario["query"],
        "condition": condition,
        "total_tokens": None,
        "cost_usd": None,
        "latency_s": None,
        "urls": 0,
        "words": 0,
        "found": False,
        "error": None,
    }
    try:
        cmd = _build_command(condition, prompt, argus)
    except Exception as e:  # noqa: BLE001 - config issue, record + move on
        rec["error"] = f"build:{type(e).__name__}:{e}"
        return rec

    # Resolve the CLI to its real path (Windows npm shims are `claude.CMD` /
    # `codex.CMD`; a bare name fails CreateProcess with FileNotFoundError).
    cmd = [shutil.which(cmd[0]) or cmd[0], *cmd[1:]]

    # codex-argus authenticates via the ARGUS_TOKEN env var (see _build_command);
    # inject it into this child only, never onto the command line.
    env = None
    if condition == "codex-argus" and argus is not None:
        _, token = _argus_url_and_token(argus)
        if token:
            env = {**os.environ, "ARGUS_TOKEN": token}

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        rec["latency_s"] = round(time.perf_counter() - t0, 3)
        rec["error"] = "timeout"
        return rec
    except Exception as e:  # noqa: BLE001 - missing binary etc.; never crash sweep
        rec["latency_s"] = round(time.perf_counter() - t0, 3)
        rec["error"] = f"exec:{type(e).__name__}:{e}"
        return rec
    rec["latency_s"] = round(time.perf_counter() - t0, 3)

    family = CONDITIONS.get(condition, "")
    if family == "claude":
        parsed = parse_claude_usage(proc.stdout)
        rec["total_tokens"] = parsed["total_tokens"]
        rec["cost_usd"] = parsed["cost_usd"]
        answer = parsed["answer_text"]
    else:
        # Codex prints the clean answer to STDOUT and the "tokens used" line to
        # STDERR; parse tokens from both streams, prefer stdout for the answer.
        parsed = parse_codex_tokens((proc.stdout or "") + "\n" + (proc.stderr or ""))
        rec["total_tokens"] = parsed["total_tokens"]
        answer = (proc.stdout or "").strip() or parsed["answer_text"]

    rec["urls"] = count_urls(answer)
    rec["words"] = len(answer.split())
    rec["found"] = found(answer)
    if proc.returncode != 0 and not answer:
        # surface a snippet of stderr so wiring failures are diagnosable.
        rec["error"] = (proc.stderr or "").strip()[:500] or f"exit:{proc.returncode}"
    return rec


def _select_scenarios(args) -> list[dict]:
    items = scen_mod.compare_scenarios()
    if getattr(args, "ids", None):
        wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
        by = {s["id"]: s for s in scen_mod.SCENARIOS}
        items = [by[i] for i in wanted if i in by]
        missing = [i for i in wanted if i not in by]
        if missing:
            print(f"[warn] unknown ids skipped: {missing}", file=sys.stderr)
    if getattr(args, "limit", None) and args.limit < len(items):
        print(
            f"[cap] limiting {len(items)} -> {args.limit} scenarios (--limit)",
            file=sys.stderr,
        )
        items = items[: args.limit]
    return items


def _select_conditions(args) -> list[str]:
    if not getattr(args, "conditions", None):
        return list(CONDITIONS)
    wanted = [c.strip() for c in args.conditions.split(",") if c.strip()]
    bad = [c for c in wanted if c not in CONDITIONS]
    if bad:
        print(f"[warn] unknown conditions skipped: {bad}", file=sys.stderr)
    return [c for c in CONDITIONS if c in wanted]


def run_sweep(args) -> None:
    """Run the selected conditions x scenarios, writing records to --out (I/O)."""
    items = _select_scenarios(args)
    conds = _select_conditions(args)

    # Only resolve the Argus MCP config if an Argus condition is selected.
    argus: dict | None = None
    if any(c.endswith("-argus") for c in conds):
        try:
            argus = argus_mcp_config()
        except Exception as e:  # noqa: BLE001 - record once, Argus runs will error
            print(f"[warn] could not load argus MCP config: {e}", file=sys.stderr)

    records: list[dict] = []
    first = True
    total = len(items) * len(conds)
    out_path = Path(args.out)
    for s in items:
        for cond in conds:
            if not first:
                time.sleep(args.pace)
            first = False
            rec = _run_one(cond, s, argus)
            records.append(rec)
            # Write the JSON after EVERY record so a long sweep is partial-readable
            # and survives an interrupt (resume-friendly); flush progress so a
            # backgrounded run is monitorable (output is otherwise block-buffered).
            out_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
            print(
                f"  [{len(records)}/{total}] {s['id']} {cond} "
                f"tokens={rec['total_tokens']} lat={rec['latency_s']}s err={rec['error']}",
                file=sys.stderr,
                flush=True,
            )

    print(f"wrote {len(records)} records ({len(items)}x{len(conds)}) -> {args.out}", flush=True)


def run_score(args) -> None:
    """Aggregate a run JSON into the markdown report (I/O)."""
    records = json.loads(Path(args.in_path).read_text(encoding="utf-8"))
    by_cond: dict[str, list[dict]] = {}
    for r in records:
        by_cond.setdefault(r.get("condition", "?"), []).append(r)
    by_condition = {c: aggregate_condition(rs) for c, rs in by_cond.items()}
    report = render_report(by_condition, records)
    out = Path(args.out) if args.out else DEFAULT_REPORT
    out.write_text(report, encoding="utf-8")
    summary = " ".join(
        f"{c}={a.get('mean_total_tokens')}" for c, a in by_condition.items()
    )
    print(f"[score] mean_tokens: {summary} -> {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="Argus 4-way (Claude/Codex x Argus) harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the CLI conditions over the scenarios")
    r.add_argument("--out", required=True)
    r.add_argument(
        "--conditions",
        default=None,
        help=f"comma-separated subset of {', '.join(CONDITIONS)} (default: all)",
    )
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--ids", default=None, help="comma-separated scenario ids")
    r.add_argument("--pace", type=float, default=2.0, help="seconds between runs")

    s = sub.add_parser("score", help="aggregate a run JSON into a markdown report")
    s.add_argument("--in", dest="in_path", required=True)
    s.add_argument("--out", default=None)

    args = p.parse_args()
    if args.cmd == "run":
        run_sweep(args)
    elif args.cmd == "score":
        run_score(args)


if __name__ == "__main__":
    main()
