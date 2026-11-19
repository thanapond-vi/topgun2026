"""Check the agent after a code or strategy.env change, BEFORE restarting it in a live game (~15 s).

    python tools/quick_check.py

1. every .py file compiles and main / strategy / console import
2. the newest real round in logs/games/<newest>/history.jsonl goes through strategy.decide() with the new code
   (LLM off, a copy of the memory file): no crash, offers within capacity, no price under salvage
3. a short arena run in the day-18 market (20 games, 15 rounds): no crash, PR printed
Prints OK or what failed. Never calls the market or the LLM.
"""
import json
import py_compile
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
BAG = 60
fails = []


def step(name, fn):
    try:
        msg = fn()
        print(f"OK   {name}" + (f": {msg}" if msg else ""))
    except Exception as e:  # noqa: BLE001
        fails.append(name)
        print(f"FAIL {name}: {e!r}")


def compile_all():
    files = [p for p in HERE.glob("*.py")] + list((HERE / "tools").glob("*.py"))
    for p in files:
        py_compile.compile(str(p), doraise=True)
    import console, main, strategy  # noqa: F401,E401
    return f"{len(files)} files"


def replay_last_round():
    import strategy
    from memory import Memory
    games = [p for p in (HERE / "logs" / "games").iterdir() if p.is_dir() and (p / "history.jsonl").exists()]
    if not games:
        return "no game logs yet (skipped)"
    g = max(games, key=lambda p: (p / "history.jsonl").stat().st_mtime)
    rows = [json.loads(l) for l in (g / "history.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    opens = [r for r in rows if r["state"].get("phase") == "ROUND_OPEN" and r["state"].get("rfqs")]
    if not opens:
        return f"{g.name}: no open round logged (skipped)"
    last = opens[-1]
    with tempfile.TemporaryDirectory() as tmp:
        mem_files = sorted(g.glob("memory*.json"))
        mpath = Path(tmp) / "memory.json"
        if mem_files:
            shutil.copy(mem_files[-1], mpath)
        memory = Memory(str(mpath))
        st = last["state"]
        rules = {"salvage_ratio": st.get("salvage_ratio", 0.8), "timeline": {"rounds": (st.get("round") or {}).get("of") or 15}}
        ctx = strategy.Context(rules, (last.get("calendar") or {}).get("calendar") or [], last["tv"], random.Random(1), memory, None, 300)
        offers, source = strategy.decide(st, ctx)
    cap = st.get("capacity_left_kg")
    kg = sum(o["kg"] for o in offers)
    if cap is not None and kg > cap:
        raise AssertionError(f"{kg} kg offered > capacity {cap}")
    low = [o for o in offers if o["price_per_kg"] < 0.8 * last["tv"].get(o["lot_id"], 0)]
    if low:
        raise AssertionError(f"{len(low)} offers under salvage")
    return f"{g.name} {st['round']['round_id']}: {len(offers)} offers, {kg // BAG} bags ({source})"


def short_arena():
    p = subprocess.run([sys.executable, str(HERE / "mock" / "arena20.py"), "--games", "20", "--ceil", "real18", "--seed", "777"],
                       cwd=HERE, capture_output=True, text=True, timeout=300, env={**__import__("os").environ, "USE_LLM": "0"})
    line = next((l for l in p.stdout.splitlines() if l.startswith("JSON ")), None)
    if not line:
        raise RuntimeError((p.stderr or p.stdout)[-300:])
    r = json.loads(line[5:])
    return f"20 games day-18 market: PR {r['pr']:.4f}, rank {r['rank']:.1f} of 56"


step("compile + import", compile_all)
step("replay newest real round", replay_last_round)
step("short arena", short_arena)
print("\nALL OK, safe to restart" if not fails else f"\n!! {len(fails)} FAILED: do not restart with this code")
sys.exit(1 if fails else 0)
