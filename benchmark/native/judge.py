"""Blind pairwise LLM judge over the 4-way answers (native vs Argus arm, same scenario).
Judge model differs from the answering model. Order of A/B is randomised per scenario and
recorded, so position bias can be checked. usage: judge.py <4way_answers.json> <out.json>"""
import json
import os
import random
import re
import shutil
import subprocess
import sys

CLAUDE = shutil.which("claude")
ENV = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
RUBRIC = """You are grading two research answers to the same question. Score each answer on:
- accuracy: factual correctness and absence of invented claims (1-5)
- sources: the listed sources are real, relevant and actually support the claims (1-5)
- specificity: concrete, current, named facts rather than generic statements (1-5)
Then pick the better answer overall ("A", "B" or "tie").
Reply with ONLY a JSON object: {"A": {"accuracy": n, "sources": n, "specificity": n},
"B": {"accuracy": n, "sources": n, "specificity": n}, "winner": "A"|"B"|"tie", "why": "<=25 words"}"""


def ask(prompt: str) -> dict:
    cmd = [CLAUDE, "-p", prompt, "--setting-sources", "", "--disable-slash-commands", "--tools", "",
           "--model", "claude-sonnet-5-5", "--output-format", "json", "--max-turns", "1"]
    p = subprocess.run(cmd, env=ENV, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, timeout=180)
    try:
        text = json.loads(p.stdout).get("result", "")
    except Exception:
        text = p.stdout
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {"error": text[:200]}
    except Exception:
        return {"error": text[:200]}


recs = json.load(open(sys.argv[1], encoding="utf-8"))
by = {}
for r in recs:
    by.setdefault(r["id"], {})[r["condition"]] = r
rng = random.Random(42)
out = []
if os.path.exists(sys.argv[2]):
    out = json.load(open(sys.argv[2], encoding="utf-8"))
done = {r["id"] for r in out if "winner" in r}
for sid, arms in sorted(by.items()):
    if "claude-native" not in arms or "claude-argus" not in arms:
        continue
    a_is_native_draw = rng.random() < 0.5  # keep the random sequence stable across resumes
    if sid in done:
        continue
    q = arms["claude-native"]["query"]
    a_is_native = (not a_is_native_draw) if len(sys.argv) > 3 and sys.argv[3] == "swap" else a_is_native_draw
    A = arms["claude-native"] if a_is_native else arms["claude-argus"]
    B = arms["claude-argus"] if a_is_native else arms["claude-native"]
    prompt = (f"{RUBRIC}\n\nQUESTION: {q}\n\n=== ANSWER A ===\n{A['answer'][:6000]}\n\n"
              f"=== ANSWER B ===\n{B['answer'][:6000]}\n")
    verdict = ask(prompt)
    rec = {"id": sid, "query": q, "A_is_native": a_is_native, "verdict": verdict}
    if "winner" in verdict:
        w = verdict["winner"]
        rec["winner"] = "tie" if w == "tie" else ("native" if (w == "A") == a_is_native else "argus")
        rec["native"] = verdict["A"] if a_is_native else verdict["B"]
        rec["argus"] = verdict["B"] if a_is_native else verdict["A"]
    out.append(rec)
    print(f"{sid} -> {rec.get('winner', 'ERR')}  {str(verdict.get('why', verdict.get('error', '')))[:80]}", flush=True)
    json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), indent=1)
wins = [r.get("winner") for r in out]
print("native wins", wins.count("native"), "argus wins", wins.count("argus"), "ties", wins.count("tie"), "errors", wins.count(None))
for k in ("accuracy", "sources", "specificity"):
    n = [r["native"][k] for r in out if "native" in r]
    a = [r["argus"][k] for r in out if "argus" in r]
    if n:
        print(f"{k}: native {sum(n)/len(n):.2f}  argus {sum(a)/len(a):.2f}")
