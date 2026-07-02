"""Large-scale search benchmark scenarios for Argus.

`SCENARIOS` is the test instrument: 160 real, diverse, specific web-search queries
spread across 8 non-trading SURIOTA-relevant categories (~20 each). Queries deliberately mix
factual lookups, entity/name searches, how-to questions, recency-sensitive (2026)
topics, and obscure technical terms, with varied length - so the benchmark exercises
the search/relevance pipeline broadly, not a single query shape.

`COMPARE_IDS` is a 40-id stratified sample (exactly 5 per category) for the live
3-way comparison (Argus research vs Claude WebSearch vs Codex CLI), which is far
more expensive per scenario than the 160-scenario SearXNG sweep.

Run `python benchmark/scenarios.py` for the self-check.
"""

from __future__ import annotations

# Each category contributes 20 queries. id = "c<NN>-<nn>" where NN is the original
# stable category number and nn is the 1-based query index within that category.
_CATEGORIES: dict[str, list[str]] = {
    "dev": [
        "python 3.13 free-threaded GIL removal status",
        "rust borrow checker explained for beginners",
        "how to debug a segfault with gdb backtrace",
        "difference between git merge and git rebase",
        "what is the actor model in concurrency",
        "go generics type constraints syntax example",
        "fastest way to parse large JSON in python",
        "ripgrep vs grep performance benchmark",
        "explain memory ordering acquire release semantics",
        "uv vs pip vs poetry package manager comparison",
        "how does a bloom filter work",
        "zig comptime metaprogramming tutorial",
        "what causes python circular import error",
        "sqlite WAL mode concurrent writes",
        "linker error undefined reference to symbol fix",
        "what is tail call optimization",
        "best data structure for LRU cache implementation",
        "how to profile python code with py-spy",
        "C++ move semantics rvalue reference explained",
        "POSIX shell parameter expansion cheatsheet",
    ],
    "firmware": [
        "ESP32 deep sleep current consumption microamps",
        "ESP-IDF v5.3 i2c master driver new API migration",
        "how to flash ESP32 over OTA with esp_ota_ops",
        "FreeRTOS task priority inversion mutex",
        "ESP32 MCPWM servo control duty cycle example",
        "STM32 DMA circular buffer ADC sampling",
        "Modbus RTU CRC16 calculation algorithm",
        "ESP32-S3 PSRAM octal vs quad SPI configuration",
        "Arduino millis overflow handling after 49 days",
        "ESP-IDF NVS namespace storage write endurance",
        "what is JTAG boundary scan debugging",
        "ESP32 WiFi reconnect after disconnect event handler",
        "LoRaWAN spreading factor range tradeoff",
        "how to reduce ESP32 brownout reset boot loop",
        "CAN bus bit timing baud rate calculator",
        "ESP-IDF MQTT TLS client certificate setup",
        "RTOS stack overflow detection configCHECK",
        "I2C clock stretching slave hold explained",
        "ESP32 ULP coprocessor assembly wake threshold",
        "bootloader vs application partition table esp32",
    ],
    "web": [
        "React 19 use hook server components",
        "Flutter riverpod vs provider state management",
        "CSS container queries browser support 2026",
        "Tailwind CSS v4 oxide engine config changes",
        "Next.js app router server actions form",
        "how to fix flutter widget rebuild performance",
        "CSS grid subgrid practical layout example",
        "React useEffect cleanup function memory leak",
        "Vite vs webpack build speed comparison",
        "Flutter custom painter draw chart canvas",
        "what is hydration mismatch in SSR",
        "CSS :has() selector parent styling examples",
        "TypeScript satisfies operator vs as assertion",
        "Flutter platform channels native android kotlin",
        "debounce vs throttle event handler javascript",
        "React server component vs client component boundary",
        "CSS aspect-ratio responsive image without layout shift",
        "Flutter isolate compute heavy parsing UI freeze",
        "web accessibility ARIA live region announce",
        "shadcn ui dialog component composition pattern",
    ],
    "ai_ml": [
        "transformer attention mechanism scaled dot product explained",
        "RAG retrieval augmented generation chunking strategy",
        "LoRA fine-tuning low rank adaptation explained",
        "what is mixture of experts MoE routing",
        "vector database HNSW index recall tradeoff",
        "LLM quantization GGUF Q4 K M vs Q8",
        "prompt injection mitigation defense techniques",
        "diffusion model denoising training objective",
        "speculative decoding LLM inference speedup",
        "embedding cosine similarity vs dot product",
        "KV cache memory transformer inference optimization",
        "reinforcement learning from human feedback RLHF steps",
        "gradient accumulation effective batch size",
        "what is catastrophic forgetting continual learning",
        "rotary positional embedding RoPE explained",
        "flash attention 2 memory IO awareness",
        "BM25 vs dense retrieval hybrid search",
        "model distillation teacher student knowledge transfer",
        "tokenizer byte pair encoding BPE algorithm",
        "agentic LLM tool calling function schema design",
    ],
    "news": [
        "latest AI model releases 2026 frontier labs",
        "EU digital identity wallet rollout 2026 update",
        "EU AI Act enforcement 2026 compliance deadline",
        "semiconductor export controls 2026 China update",
        "AI data center energy demand 2026 news",
        "OpenAI Anthropic Google latest announcement 2026",
        "Nvidia GPU shortage datacenter 2026",
        "open source security funding 2026 trends",
        "climate policy COP 2026 agreement outcome",
        "global public health funding 2026 forecast",
        "major data breach cybersecurity 2026",
        "electric vehicle market share 2026 trends",
        "quantum computing milestone 2026 breakthrough",
        "global inflation rates 2026 central bank response",
        "tech layoffs 2026 industry news",
        "Indonesia economic growth 2026 outlook",
        "new programming language adoption survey 2026",
        "robotics humanoid announcement 2026",
        "renewable energy capacity additions 2026",
        "space launch milestones 2026 missions",
    ],
    "docs": [
        "python asyncio.gather return_exceptions parameter docs",
        "httpx AsyncClient timeout configuration documentation",
        "pydantic v2 model_validate vs parse_obj",
        "FastAPI dependency injection Depends documentation",
        "numpy einsum notation reference examples",
        "pandas merge how parameter inner outer left",
        "postgresql jsonb operators query documentation",
        "docker compose healthcheck condition depends_on",
        "nginx proxy_pass trailing slash behavior",
        "playwright page.wait_for_selector state option",
        "redis SET EX NX options documentation",
        "git rebase --onto explained with example",
        "kubernetes liveness vs readiness probe difference",
        "systemd service restart policy on-failure",
        "openssl generate self-signed certificate command",
        "sqlalchemy 2.0 select statement async session",
        "ffmpeg crop filter syntax documentation",
        "jq select filter array of objects",
        "rust tokio spawn vs block_on documentation",
        "matplotlib subplots sharex sharey reference",
    ],
    "science": [
        "CRISPR Cas9 mechanism of action explained",
        "general relativity gravitational time dilation",
        "P versus NP problem explained simply",
        "mRNA vaccine mechanism immune response",
        "what is quantum entanglement Bell inequality",
        "second law of thermodynamics entropy explained",
        "protein folding alphafold prediction accuracy",
        "Riemann hypothesis significance number theory",
        "photosynthesis light-dependent reactions steps",
        "black hole event horizon hawking radiation",
        "Navier-Stokes equations turbulence open problem",
        "neuron action potential sodium potassium pump",
        "standard model particle physics force carriers",
        "Bayesian inference prior posterior likelihood",
        "central limit theorem intuition explained",
        "dark matter evidence galaxy rotation curves",
        "telomere shortening cellular aging mechanism",
        "Fourier transform intuition frequency domain",
        "antibiotic resistance horizontal gene transfer",
        "Markov chain stationary distribution explained",
    ],
    "business": [
        "SaaS pricing tiers freemium vs usage based",
        "IoT gateway market size forecast 2026",
        "how to calculate customer acquisition cost CAC",
        "B2B sales funnel conversion benchmarks",
        "net revenue retention NRR good benchmark",
        "industrial automation market Indonesia opportunity",
        "product-market fit how to measure",
        "MQL vs SQL marketing qualified lead difference",
        "edge computing market growth drivers",
        "pricing strategy value-based vs cost-plus",
        "go-to-market strategy hardware startup",
        "what is LTV to CAC ratio healthy",
        "Modbus gateway competitor landscape vendors",
        "annual recurring revenue ARR vs MRR",
        "channel partner reseller margin structure",
        "smart energy monitoring market trends 2026",
        "how to write a software quotation proposal",
        "freemium conversion rate industry average",
        "enterprise sales cycle length B2B SaaS",
        "total addressable market TAM SAM SOM estimation",
    ],
}

CATEGORIES: tuple[str, ...] = tuple(_CATEGORIES.keys())
_CATEGORY_NUMBERS: dict[str, int] = {
    "dev": 1,
    "firmware": 2,
    "web": 5,
    "ai_ml": 6,
    "news": 7,
    "docs": 8,
    "science": 9,
    "business": 10,
}


def _build_scenarios() -> list[dict]:
    scenarios: list[dict] = []
    for category, queries in _CATEGORIES.items():
        cat_idx = _CATEGORY_NUMBERS[category]
        for q_idx, query in enumerate(queries, start=1):
            scenarios.append(
                {
                    "id": f"c{cat_idx:02d}-{q_idx:02d}",
                    "category": category,
                    "query": query,
                }
            )
    return scenarios


SCENARIOS: list[dict] = _build_scenarios()


# Stratified sample for the live 3-way comparison: exactly 5 per category, 40 total
# (8 categories x 5). Picks are spread across each category's 1..20 index range
# (early / lower-middle / middle / upper-middle / late) for diversity. The first 2-3
# of each row are the original 25-id sample; the rest extend it to 5/category.
_COMPARE_PICKS: dict[str, list[int]] = {
    "dev": [1, 10, 18, 4, 14],
    "firmware": [2, 14, 5, 11, 18],
    "web": [1, 11, 20, 5, 15],
    "ai_ml": [1, 16, 5, 9, 13],
    "news": [2, 5, 13, 8, 17],
    "docs": [3, 18, 6, 11, 15],
    "science": [3, 9, 16, 6, 13],
    "business": [1, 13, 5, 9, 17],
}


def _build_compare_ids() -> list[str]:
    ids: list[str] = []
    for category, picks in _COMPARE_PICKS.items():
        ci = _CATEGORY_NUMBERS[category]
        for p in picks:
            ids.append(f"c{ci:02d}-{p:02d}")
    return ids


COMPARE_IDS: list[str] = _build_compare_ids()


def by_id(scenario_id: str) -> dict:
    """Return the scenario dict for an id (raises KeyError if unknown)."""
    for s in SCENARIOS:
        if s["id"] == scenario_id:
            return s
    raise KeyError(scenario_id)


def compare_scenarios() -> list[dict]:
    """The 40 scenarios named by COMPARE_IDS, in COMPARE_IDS order."""
    return [by_id(i) for i in COMPARE_IDS]


def _self_check() -> None:
    assert len(SCENARIOS) == 160, f"expected 160 scenarios, got {len(SCENARIOS)}"
    ids = [s["id"] for s in SCENARIOS]
    assert len(set(ids)) == len(ids), "scenario ids not unique"
    queries = [s["query"] for s in SCENARIOS]
    assert len(set(queries)) == len(queries), "duplicate queries present"
    assert len(COMPARE_IDS) == 40, f"expected 40 compare ids, got {len(COMPARE_IDS)}"
    assert len(set(COMPARE_IDS)) == 40, "compare ids not unique"
    id_set = set(ids)
    missing = [i for i in COMPARE_IDS if i not in id_set]
    assert not missing, f"COMPARE_IDS not in SCENARIOS: {missing}"
    all_cats = set(_CATEGORIES)
    scen_cats = {s["category"] for s in SCENARIOS}
    assert scen_cats == all_cats, f"category mismatch in SCENARIOS: {scen_cats ^ all_cats}"
    cmp_cats = {by_id(i)["category"] for i in COMPARE_IDS}
    assert cmp_cats == all_cats, f"not every category in COMPARE_IDS: {all_cats - cmp_cats}"
    per_cat = {c: sum(1 for s in SCENARIOS if s["category"] == c) for c in all_cats}
    assert all(v == 20 for v in per_cat.values()), f"uneven categories: {per_cat}"
    cmp_per_cat = {
        c: sum(1 for i in COMPARE_IDS if by_id(i)["category"] == c) for c in all_cats
    }
    assert all(v == 5 for v in cmp_per_cat.values()), (
        f"COMPARE_IDS not exactly 5/category: {cmp_per_cat}"
    )
    print(f"OK: {len(SCENARIOS)} scenarios, {len(CATEGORIES)} categories x 20")
    print(f"OK: {len(COMPARE_IDS)} stratified compare ids, exactly 5 per category")
    print("COMPARE_IDS:", ", ".join(COMPARE_IDS))


if __name__ == "__main__":
    _self_check()
