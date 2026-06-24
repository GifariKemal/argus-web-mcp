"""Argus benchmark runner.

Loads benchmark/testset.yaml, runs each selected URL item through every adapter
(argus + free baselines), scores each vs a curated gold file when one exists
(reference-based) else reference-free, and writes benchmark/report.md.

Resilient: a failing fetch for one item is caught and recorded (ok=False) - it
never crashes the run. Every skipped item is logged with a reason (no silent caps).

Usage:
  python benchmark/run_bench.py
  python benchmark/run_bench.py --categories docs,longform
  python benchmark/run_bench.py --ids longform-04,docs-03
  python benchmark/run_bench.py --limit 3
  python benchmark/run_bench.py --offline      # skip live network
  python benchmark/run_bench.py --quality      # formatting-invariant quality_f1 (FAIR gate)
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

import yaml

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))  # so `import scorer`/`adapters` works either way

import adapters as adapters_mod  # noqa: E402
import scorer  # noqa: E402

TESTSET = BENCH_DIR / "testset.yaml"
GOLD_DIR = BENCH_DIR / "gold"
QUALITY_GOLD = BENCH_DIR / "quality_gold.yaml"
REPORT = BENCH_DIR / "report.md"


def _load_testset() -> dict:
    return yaml.safe_load(TESTSET.read_text(encoding="utf-8"))


def _gold_text(item: dict) -> str | None:
    """Return the curated gold main-text for an item, or None if not curated.

    Strips the leading HTML-comment provenance line(s) so they don't pollute scoring.
    """
    ref = item.get("gold_reference")
    if not ref or ref == "TBD-curate-in-P1":
        return None
    path = (BENCH_DIR / ref) if not Path(ref).is_absolute() else Path(ref)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    # Drop a leading provenance comment block of the form <!-- ... -->.
    if text.lstrip().startswith("<!--"):
        end = text.find("-->")
        if end != -1:
            text = text[end + 3 :]
    return text.strip()


def _select(testset: dict, args) -> tuple[list[dict], list[tuple[str, str]]]:
    """Return (selected_items, skipped[(id, reason)]) honoring the CLI filters."""
    items = testset.get("urls", [])
    skipped: list[tuple[str, str]] = []
    cats = set(args.categories.split(",")) if args.categories else None
    ids = set(args.ids.split(",")) if args.ids else None

    selected: list[dict] = []
    for it in items:
        iid = it.get("id", "?")
        if ids is not None and iid not in ids:
            continue
        if cats is not None and it.get("category") not in cats:
            continue
        gold = _gold_text(it)
        # Default subset = curated items (have a real gold file). Explicit filters override.
        if ids is None and cats is None and gold is None:
            skipped.append((iid, "no gold (not in default curated subset)"))
            continue
        if args.offline and gold is None:
            skipped.append((iid, "offline mode + no checked-in snapshot"))
            continue
        selected.append(it)

    if args.limit is not None:
        for it in selected[args.limit :]:
            skipped.append((it.get("id", "?"), f"beyond --limit {args.limit}"))
        selected = selected[: args.limit]
    return selected, skipped


async def _run(args) -> int:
    testset = _load_testset()
    selected, skipped = _select(testset, args)

    for iid, why in skipped:
        print(f"[skip] {iid}: {why}")

    if not selected:
        print("[info] no items selected - validating harness wiring only, writing empty report.")
        _write_report({}, [], skipped, offline=args.offline)
        return 0

    # Offline: every adapter here needs the live network (Argus does a real fetch;
    # the baselines httpx.get). We keep only post-extraction gold, NOT raw HTML
    # snapshots, so no adapter can be replayed offline. Per spec, offline therefore
    # validates harness wiring and reports 0 scored items rather than running blind.
    if args.offline:
        print(f"[info] offline mode: {len(selected)} item(s) selected but no checked-in "
              "HTML snapshot exists to replay any adapter - validating wiring, scoring 0 items.")
        for it in selected:
            print(f"[skip] {it['id']}: offline (no raw-HTML snapshot to replay)")
        _write_report({}, [], skipped, offline=True)
        print(f"\n[done] wrote {REPORT}")
        return 0

    adapter_list = adapters_mod.free_adapters()
    for key, ad in adapters_mod.KEYED_ADAPTERS.items():
        import os

        if os.environ.get(key):
            adapter_list.append(ad)
        else:
            print(f"[skip] paid adapter {getattr(ad, 'name', key)}: env {key} unset")

    # rows[item_id] = {adapter_name: score_dict}
    rows: dict[str, dict] = {}
    item_meta: list[dict] = []

    use_argus = any(a.name == "argus" for a in adapter_list)
    if use_argus:
        print("[setup] starting Argus (real client + browser)...")
        await adapters_mod.setup_argus()
    try:
        for it in selected:
            iid = it["id"]
            gold = _gold_text(it)
            item_meta.append({"id": iid, "category": it.get("category"),
                              "url": it.get("url"), "has_gold": gold is not None})
            rows[iid] = {}
            for ad in adapter_list:
                try:
                    res = await ad.run(it)
                except Exception as exc:  # noqa: BLE001 - never crash the whole run
                    print(f"[error] {iid}/{ad.name}: {exc}")
                    res = {"content": "", "latency": 0.0, "ok": False, "detail": str(exc)}
                sc = scorer.score_item(res["content"], gold, res["ok"], res["latency"])
                if res.get("detail"):
                    sc["detail"] = res["detail"]
                rows[iid][ad.name] = sc
                tag = "ok" if res["ok"] else f"FAIL({res.get('detail', '?')})"
                rl = sc["rouge_l"]
                rl_s = f"{rl:.3f}" if rl is not None else "n/a"
                print(f"[run] {iid:<12} {ad.name:<16} {tag:<22} "
                      f"rougeL={rl_s} words={sc['pred_words']} {res['latency']:.2f}s")
    finally:
        if use_argus:
            await adapters_mod.teardown_argus()

    _write_report(rows, item_meta, skipped, offline=args.offline)
    print(f"\n[done] wrote {REPORT}")
    return 0


def _median(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _fmt(v: float | None, nd: int = 3) -> str:
    return f"{v:.{nd}f}" if v is not None else "n/a"


def _write_report(rows: dict, item_meta: list, skipped: list, offline: bool) -> None:
    adapter_names: list[str] = []
    for per in rows.values():
        for name in per:
            if name not in adapter_names:
                adapter_names.append(name)

    lines: list[str] = []
    lines.append("# Argus Benchmark Report\n")
    lines.append(f"Generated by `benchmark/run_bench.py`. Mode: "
                 f"{'offline' if offline else 'live'}.\n")
    lines.append(f"Items scored: {len(rows)} | Adapters: "
                 f"{', '.join(adapter_names) or '(none)'}\n")

    # --- leaderboard ---
    lines.append("\n## Leaderboard\n")
    if adapter_names:
        lines.append("| adapter | success rate | median ROUGE-L | median token-F1 | "
                     "median truncation | median latency (s) |")
        lines.append("|---|---|---|---|---|---|")
        for name in adapter_names:
            scores = [rows[i][name] for i in rows if name in rows[i]]
            n = len(scores)
            succ = sum(1 for s in scores if s["ok"]) / n if n else 0.0
            med_rl = _median([s["rouge_l"] for s in scores])
            med_f1 = _median([s["token_f1"] for s in scores])
            med_tr = _median([s["truncation"] for s in scores])
            med_lat = _median([s["latency"] for s in scores])
            lines.append(f"| {name} | {succ:.0%} ({n}) | {_fmt(med_rl)} | {_fmt(med_f1)} "
                         f"| {_fmt(med_tr)} | {_fmt(med_lat, 2)} |")
    else:
        lines.append("_No adapter results (harness-wiring validation run)._")

    # --- per-item ROUGE-L (gold items only) ---
    gold_ids = [m["id"] for m in item_meta if m["has_gold"]]
    if gold_ids and adapter_names:
        lines.append("\n## Per-item ROUGE-L (curated-gold items)\n")
        header = "| item | " + " | ".join(adapter_names) + " |"
        lines.append(header)
        lines.append("|" + "---|" * (len(adapter_names) + 1))
        for iid in gold_ids:
            cells = []
            for name in adapter_names:
                sc = rows.get(iid, {}).get(name)
                cells.append(_fmt(sc["rouge_l"]) if sc else "n/a")
            lines.append(f"| {iid} | " + " | ".join(cells) + " |")

    # --- where Argus loses ---
    lines.append("\n## Where Argus loses (ROUGE-L)\n")
    losses = []
    for iid, per in rows.items():
        argus_sc = per.get("argus")
        if not argus_sc or argus_sc["rouge_l"] is None:
            continue
        for name, sc in per.items():
            if name == "argus" or sc["rouge_l"] is None:
                continue
            if sc["rouge_l"] > argus_sc["rouge_l"] + 1e-9:
                losses.append((iid, name, argus_sc["rouge_l"], sc["rouge_l"]))
    if losses:
        lines.append("| item | beaten by | argus ROUGE-L | competitor ROUGE-L |")
        lines.append("|---|---|---|---|")
        for iid, name, a, c in losses:
            lines.append(f"| {iid} | {name} | {a:.3f} | {c:.3f} |")
    else:
        lines.append("_Argus was not beaten on ROUGE-L by any free baseline on the "
                     "curated-gold items._")

    # --- gate check ---
    if gold_ids and "argus" in adapter_names:
        argus_rls = [rows[i]["argus"]["rouge_l"] for i in gold_ids
                     if rows.get(i, {}).get("argus", {}).get("rouge_l") is not None]
        base_names = [n for n in adapter_names if n != "argus"]
        best_base = None
        for n in base_names:
            rls = [rows[i][n]["rouge_l"] for i in gold_ids
                   if rows.get(i, {}).get(n, {}).get("rouge_l") is not None]
            m = _median(rls)
            if m is not None and (best_base is None or m > best_base[1]):
                best_base = (n, m)
        argus_med = _median(argus_rls)
        lines.append("\n## Exit-gate: Argus ROUGE-L >= best free baseline\n")
        lines.append(f"- Argus median ROUGE-L: {_fmt(argus_med)}")
        if best_base:
            lines.append(f"- Best free baseline: {best_base[0]} @ {_fmt(best_base[1])}")
            if argus_med is not None:
                verdict = "PASS" if argus_med >= best_base[1] - 1e-9 else "FAIL"
                lines.append(f"- **Gate: {verdict}**")
        else:
            lines.append("- No baseline ROUGE-L available to compare.")

    # --- skipped ---
    if skipped:
        lines.append("\n## Skipped items\n")
        for iid, why in skipped:
            lines.append(f"- `{iid}`: {why}")

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run_quality(args) -> int:
    """Formatting-invariant extraction-quality mode (the FAIR gate).

    Loads quality_gold.yaml, runs every free adapter on those URLs, and scores
    content_recall / boilerplate_rejection / quality_f1 - all formatting-agnostic,
    so no adapter gets a home-field advantage from its output style. Resilient
    per item: a failing fetch is caught and recorded, never crashing the run.
    """
    if not QUALITY_GOLD.is_file():
        print(f"[error] {QUALITY_GOLD} not found")
        return 1
    gold_items = yaml.safe_load(QUALITY_GOLD.read_text(encoding="utf-8")) or []
    if not isinstance(gold_items, list) or not gold_items:
        print("[error] quality_gold.yaml is empty or malformed")
        return 1

    adapter_list = adapters_mod.free_adapters()  # argus + raw_trafilatura + readability_only
    use_argus = any(a.name == "argus" for a in adapter_list)
    if use_argus:
        print("[setup] starting Argus (real client + browser)...")
        await adapters_mod.setup_argus()

    # qrows[item_id] = {adapter_name: {recall, rejection, f1, ok, latency, words}}
    qrows: dict[str, dict] = {}
    qmeta: list[dict] = []
    try:
        for g in gold_items:
            iid = g.get("id", "?")
            mc = g.get("must_contain", []) or []
            mnc = g.get("must_not_contain", []) or []
            # category drives Argus read vs read_pdf; quality_gold items carry it.
            item = {"id": iid, "url": g["url"], "category": g.get("category")}
            qmeta.append({"id": iid, "url": g["url"], "n_mc": len(mc), "n_mnc": len(mnc)})
            qrows[iid] = {}
            for ad in adapter_list:
                try:
                    res = await ad.run(item)
                except Exception as exc:  # noqa: BLE001 - never crash the whole run
                    print(f"[error] {iid}/{ad.name}: {exc}")
                    res = {"content": "", "latency": 0.0, "ok": False, "detail": str(exc)}
                content = res.get("content", "") or ""
                recall = scorer.content_recall(content, mc)
                rejection = scorer.boilerplate_rejection(content, mnc)
                f1 = scorer.quality_f1(content, mc, mnc)
                qrows[iid][ad.name] = {
                    "recall": recall, "rejection": rejection, "f1": f1,
                    "ok": res["ok"], "latency": res["latency"],
                    "words": len(content.split()),
                }
                tag = "ok" if res["ok"] else f"FAIL({res.get('detail', '?')})"
                print(f"[quality] {iid:<12} {ad.name:<16} {tag:<22} "
                      f"recall={recall:.3f} reject={rejection:.3f} f1={f1:.3f} "
                      f"words={len(content.split())} {res['latency']:.2f}s")
    finally:
        if use_argus:
            await adapters_mod.teardown_argus()

    _write_quality_report(qrows, qmeta)
    print(f"\n[done] appended Extraction-quality section to {REPORT}")
    return 0


def _write_quality_report(qrows: dict, qmeta: list) -> None:
    """Append/refresh the 'Extraction-quality (formatting-invariant)' section.

    Preserves the existing (legacy ROUGE-L) report if present, marking it clearly
    as confounded; replaces only the quality section on re-run.
    """
    adapter_names: list[str] = []
    for per in qrows.values():
        for name in per:
            if name not in adapter_names:
                adapter_names.append(name)

    out: list[str] = []
    out.append("\n---\n")
    out.append("## Extraction-quality (formatting-invariant) - the FAIR gate\n")
    out.append("Source: `quality_gold.yaml`. Metric: `quality_f1` = harmonic mean of "
               "**content_recall** (fraction of gold main-content phrases captured, "
               ">=0.8 token-overlap) and **boilerplate_rejection** (fraction of known "
               "nav/ad/footer strings ABSENT). Formatting-agnostic - markdown vs raw "
               "text does not move the score, so no adapter gets a home-field advantage. "
               "A raw full-page DOM dump tanks on rejection; an over-trimmer tanks on recall.\n")
    out.append(f"Items scored: {len(qrows)} | Adapters: {', '.join(adapter_names) or '(none)'}\n")

    # --- leaderboard ---
    out.append("\n### Quality leaderboard\n")
    if adapter_names:
        out.append("| adapter | median quality_f1 | median content_recall | "
                   "median boilerplate_rejection | success rate | median latency (s) |")
        out.append("|---|---|---|---|---|---|")
        med = {}
        for name in adapter_names:
            scores = [qrows[i][name] for i in qrows if name in qrows[i]]
            n = len(scores)
            m_f1 = _median([s["f1"] for s in scores])
            m_rc = _median([s["recall"] for s in scores])
            m_rj = _median([s["rejection"] for s in scores])
            m_lat = _median([s["latency"] for s in scores])
            succ = sum(1 for s in scores if s["ok"]) / n if n else 0.0
            med[name] = m_f1
            out.append(f"| {name} | {_fmt(m_f1)} | {_fmt(m_rc)} | {_fmt(m_rj)} "
                       f"| {succ:.0%} ({n}) | {_fmt(m_lat, 2)} |")
    else:
        out.append("_No adapter results._")
        med = {}

    # --- per-item quality_f1 ---
    if adapter_names:
        out.append("\n### Per-item quality_f1\n")
        header = "| item | " + " | ".join(adapter_names) + " |"
        out.append(header)
        out.append("|" + "---|" * (len(adapter_names) + 1))
        for iid in qrows:
            cells = [_fmt(qrows[iid].get(name, {}).get("f1")) for name in adapter_names]
            out.append(f"| {iid} | " + " | ".join(cells) + " |")

        out.append("\n### Per-item content_recall / boilerplate_rejection\n")
        out.append("| item | adapter | recall | rejection |")
        out.append("|---|---|---|---|")
        for iid in qrows:
            for name in adapter_names:
                sc = qrows[iid].get(name)
                if sc:
                    out.append(f"| {iid} | {name} | {_fmt(sc['recall'])} | "
                               f"{_fmt(sc['rejection'])} |")

    # --- fair gate verdict ---
    if "argus" in adapter_names:
        argus_med = med.get("argus")
        base = {n: m for n, m in med.items() if n != "argus" and m is not None}
        best_base = max(base.items(), key=lambda kv: kv[1]) if base else None
        out.append("\n### Fair exit-gate: Argus quality_f1 >= best free baseline\n")
        out.append(f"- Argus median quality_f1: {_fmt(argus_med)}")
        if best_base:
            out.append(f"- Best free baseline: {best_base[0]} @ {_fmt(best_base[1])}")
            if argus_med is not None:
                verdict = "PASS" if argus_med >= best_base[1] - 1e-9 else "FAIL"
                out.append(f"- **Fair gate: {verdict}**")
        else:
            out.append("- No baseline quality_f1 available to compare.")

    # Splice into report.md: keep everything before our marker, replace from it on.
    marker = "## Extraction-quality (formatting-invariant)"
    existing = REPORT.read_text(encoding="utf-8") if REPORT.is_file() else ""
    if existing:
        # Mark the legacy ROUGE-L gate as confounded (idempotent).
        legacy_note = ("> **NOTE - the ROUGE-L sections below are LEGACY/CONFOUNDED.** ROUGE-L "
                       "against a raw-text gold rewards the least-transformed output (a raw DOM "
                       "dump) and penalises Argus's clean Markdown. See the formatting-invariant "
                       "**quality_f1** section at the end for the fair measure.\n")
        if "LEGACY/CONFOUNDED" not in existing:
            lines = existing.split("\n")
            # insert the note right after the first H1 + its blank line
            insert_at = 1
            for i, ln in enumerate(lines):
                if ln.startswith("# "):
                    insert_at = i + 1
                    break
            lines.insert(insert_at, "\n" + legacy_note)
            existing = "\n".join(lines)
        idx = existing.find("\n---\n## Extraction-quality (formatting-invariant)")
        if idx == -1:
            idx = existing.find(marker)
            if idx != -1:
                # trim back to a preceding --- separator if present
                sep = existing.rfind("\n---\n", 0, idx)
                idx = sep if sep != -1 else idx
        head = existing[:idx].rstrip() + "\n" if idx != -1 else existing.rstrip() + "\n"
    else:
        head = ("# Argus Benchmark Report\n\n_Quality-only run (no legacy ROUGE-L "
                "section present)._\n")
    REPORT.write_text(head + "\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Argus benchmark runner")
    ap.add_argument("--categories", help="comma-separated category filter (e.g. news,docs)")
    ap.add_argument("--ids", help="comma-separated item-id filter")
    ap.add_argument("--limit", type=int, help="max items to run")
    ap.add_argument("--offline", action="store_true",
                    help="skip live network (only items with checked-in gold)")
    ap.add_argument("--quality", action="store_true",
                    help="formatting-invariant extraction-quality mode (quality_f1, the FAIR gate)")
    args = ap.parse_args()
    if args.quality:
        return asyncio.run(_run_quality(args))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
