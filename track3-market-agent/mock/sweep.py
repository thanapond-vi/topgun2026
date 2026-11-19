"""Overnight parameter sweep on many cores: every variant x every scenario of mock/arena20.py, one process each.

    python mock/sweep.py --out sweep-out --games 300 --workers 70

Phase 1 changes one setting at a time from the current defaults. Phase 2 combines every change that beat the
defaults on mean rank and re-tests the combination against the defaults on fresh seeds. Results: <out>/results.jsonl
(one line per run, written as they finish) and <out>/summary.md (written at the end of each phase).
Only uses the code here and the mock market: no network, no keys.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIOS = {
    "d20":        ["--rounds", "15", "--capacity", "5"],
    "d20_cap4":   ["--rounds", "15", "--capacity", "4"],
    "d20_strong": ["--rounds", "15", "--capacity", "5", "--mix", "seeit=14,nuxeus=14,spread1=14,tuned=8,dumper=5"],
    "d20_naive":  ["--rounds", "15", "--capacity", "5", "--mix", "starter=30,tuned=15,dumper=10"],
    "d18":        ["--rounds", "8", "--capacity", "5", "--practice", "1"],
}
# --set short (19 Nov): buyers want less than the teams bring (demand 0.70, mostly thin-margin rivals) next to the
# real-day-18 calibrated world and the old harsh one, so a change must not lose where the market is not short.
SCENARIOS_SHORT = {
    "short20":      ["--world", "short"],
    "short20_cap4": ["--world", "short", "--capacity", "4"],
    "short18":      ["--world", "short", "--rounds", "8", "--practice", "1"],
    "short20_d80":  ["--world", "short", "--demand", "0.80"],
    "real20":       ["--ceil", "real18"],
    "real18":       ["--ceil", "real18", "--rounds", "8", "--practice", "1"],
    "harsh20":      ["--rounds", "15", "--capacity", "5"],
}
GRID_SHORT = {
    "STRATEGY_STEP_UP": ["0.005", "0.01", "0.015", "0.02"],
    "STRATEGY_SHORT_FLOOR": ["0", "0.86", "0.88", "0.90", "0.92", "0.94"],
    "STRATEGY_SHORT_FILL": ["0.85", "0.90", "0.96"],
    "STRATEGY_FLOOR_MID": ["0.90", "0.94", "0.97"],
    "STRATEGY_PRESSURE_DROP": ["0.04", "0.12", "0.16"],
    "STRATEGY_ANCHOR_UNDER": ["0.005", "0.02"],
    "STRATEGY_INSTANT_FLOOR_GAP": ["0.03", "0.08"],
    "STRATEGY_PACE": ["1.1", "1.6"],
    "STRATEGY_LAST_CAP_INST": ["0", "0.90"],
    "STRATEGY_MARKUP_SPEC": ["1.00", "1.05"],
    "STRATEGY_KEEP_COM_LAST": ["3"],
}
EXTRA_SHORT = [("old_default", {"STRATEGY_STEP_UP": "0.02", "STRATEGY_SHORT_FLOOR": "0"})]
EXTRA = []
ALL_SCENARIOS = {**SCENARIOS, **SCENARIOS_SHORT}   # worker processes import this module fresh: look scenarios up here
GRID = {
    "STRATEGY_PRESSURE_DROP": ["0.04", "0.12", "0.16", "0.20"],
    "STRATEGY_PRACTICE_PROBE": ["0"],
    "STRATEGY_PROBE_LADDER": ["-0.05,0,0.05", "0", "-0.02,0.02"],
    "STRATEGY_CROWD_MAX": ["2.0", "3.0", "4.0"],
    "STRATEGY_PRESSURE": ["0"],
    "STRATEGY_ANCHOR_UNDER": ["0.0", "0.02"],
    "STRATEGY_ANCHOR": ["0"],
    "STRATEGY_ANCHOR_AGE": ["1", "3"],
    "STRATEGY_HOLD_IF_BETTER": ["0"],
    "STRATEGY_CLOSE_MAX_TV": ["1"],
    "STRATEGY_MARKUP_SPEC": ["1.00", "1.05", "1.10"],
    "STRATEGY_MARKUP_COM": ["0.96", "1.00"],
    "STRATEGY_MARKUP_INST": ["0.90", "0.92"],
    "STRATEGY_INSTANT_FLOOR_GAP": ["0.03", "0.08"],
    "STRATEGY_FLOOR_MID": ["0.94", "1.00"],
    "STRATEGY_STEP_UP": ["0.01", "0.03"],
    "STRATEGY_UNTESTED_BAGS": ["0", "1", "3"],
    "STRATEGY_INSTANT_EARLY_FRAC": ["0", "0.3", "0.5"],
    "STRATEGY_RUSH_ROUND_DOWN": ["0"],
    "STRATEGY_PACE": ["1.1", "1.5"],
    "STRATEGY_MAX_DROP": ["0.05"],
    "STRATEGY_LAST_CAP_INST": ["0", "0.90"],
    "STRATEGY_LAST_NO_STEP": ["1"],
    "STRATEGY_COMMERCIAL_MAX": ["1.01", "1.05"],
}


def run(task):
    name, env_add, scen, games, seed = task
    env = {**os.environ, "USE_LLM": "0", **env_add}
    t0 = time.time()
    p = subprocess.run([sys.executable, str(HERE / "arena20.py"), "--games", str(games), "--seed", str(seed),
                        *ALL_SCENARIOS[scen]], cwd=HERE.parent, env=env, capture_output=True, text=True)
    line = next((l for l in p.stdout.splitlines() if l.startswith("JSON ")), None)
    res = json.loads(line[5:]) if line else {"error": (p.stderr or p.stdout)[-500:]}
    return {"variant": name, "env": env_add, "scenario": scen, "games": games, "seed": seed,
            "seconds": round(time.time() - t0, 1), **res}


def table(rows, out: Path, title: str, mode: str):
    by = {}
    for r in rows:
        if "rank" in r:
            by.setdefault(r["variant"], {})[r["scenario"]] = r
    base = by.get("default", {})
    lines = [f"## {title}", "", "rank = mean rank of 56 (lower is better) · Δ vs default · PR = mean PR", "",
             "| variant | mean rank | Δ rank | mean PR | Δ PR | " + " | ".join(SCENARIOS) + " |",
             "|---|---|---|---|---|" + "---|" * len(SCENARIOS)]
    scored = []
    for v, sc in by.items():
        if len(sc) < len(SCENARIOS):
            continue
        rank = statistics.mean(sc[s]["rank"] for s in SCENARIOS)
        pr = statistics.mean(sc[s]["pr"] for s in SCENARIOS)
        scored.append((rank, pr, v, sc))
    b_rank = statistics.mean(base[s]["rank"] for s in SCENARIOS) if len(base) == len(SCENARIOS) else None
    b_pr = statistics.mean(base[s]["pr"] for s in SCENARIOS) if len(base) == len(SCENARIOS) else None
    for rank, pr, v, sc in sorted(scored):
        d_rank = f"{rank - b_rank:+.2f}" if b_rank is not None else ""
        d_pr = f"{pr - b_pr:+.4f}" if b_pr is not None else ""
        cells = " | ".join(f"{sc[s]['rank']:.1f} / {sc[s]['pr']:.3f}" for s in SCENARIOS)
        lines.append(f"| {v} | {rank:.2f} | {d_rank} | {pr:.4f} | {d_pr} | {cells} |")
    with open(out / "summary.md", mode, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")
    return scored, b_rank, b_pr


def phase(tasks, workers, out: Path):
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as ex, open(out / "results.jsonl", "a", encoding="utf-8") as f:
        futs = [ex.submit(run, t) for t in tasks]
        for i, fu in enumerate(as_completed(futs), 1):
            r = fu.result()
            rows.append(r)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{time.strftime('%H:%M:%S')}] {i}/{len(tasks)} {r['variant']} {r['scenario']} "
                  f"rank {r.get('rank', 'ERR')} pr {r.get('pr', '')}", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sweep-out")
    ap.add_argument("--games", type=int, default=300)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 8))
    ap.add_argument("--seed", type=int, default=10000)
    ap.add_argument("--set", default="old", choices=["old", "short"])
    ap.add_argument("--base", default="{}", help="JSON env every variant starts from (the previous night round's best)")
    a = ap.parse_args()
    global SCENARIOS, GRID, EXTRA
    if a.set == "short":
        SCENARIOS, GRID, EXTRA = SCENARIOS_SHORT, GRID_SHORT, EXTRA_SHORT
    base = json.loads(a.base)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(f"# arena20 sweep · started {time.strftime('%Y-%m-%d %H:%M')} · {a.games} games per cell\n\n",
                                    encoding="utf-8")
    (out / "base.json").write_text(json.dumps(base, indent=1), encoding="utf-8")
    variants = [("default", {})] + [(f"{k.replace('STRATEGY_', '')}={v}", {k: v}) for k, vs in GRID.items() for v in vs
                                    if base.get(k) != v] + EXTRA
    tasks = [(n, {**base, **e}, s, a.games, a.seed) for n, e in variants for s in SCENARIOS]
    print(f"phase 1: {len(tasks)} runs on {a.workers} workers", flush=True)
    rows = phase(tasks, a.workers, out)
    scored, b_rank, b_pr = table(rows, out, "Phase 1: one change at a time", "a")
    # phase 2: combine the best value of every setting that beat the default on mean rank without losing PR
    best = {}
    for rank, pr, v, sc in sorted(scored):
        if v == "default" or rank >= b_rank or pr < b_pr - 0.001:
            continue
        env = next(e for n, e in variants if n == v)
        if len(env) != 1:
            continue                                   # fixed comparison variants (old_default) are not combined
        k = next(iter(env))
        best.setdefault(k, (rank, env[k]))
    combo = {k: v for k, (_, v) in best.items()}
    top3 = dict(sorted(best.items(), key=lambda x: x[1][0])[:3])
    combos = [("default", dict(base)), ("combo_all", {**base, **combo}),
              ("combo_top3", {**base, **{k: v for k, (_, v) in top3.items()}})]
    tasks2 = [(n, e, s, a.games * 2, a.seed + 50000) for n, e in combos for s in SCENARIOS]
    print(f"phase 2: {combos}", flush=True)
    rows2 = phase(tasks2, a.workers, out)
    with open(out / "summary.md", "a", encoding="utf-8") as f:
        f.write("## Phase 2 combos\n\n" + "\n".join(f"- {n}: `{json.dumps(e)}`" for n, e in combos) + "\n\n")
    scored2, b2_rank, b2_pr = table(rows2, out, f"Phase 2: combos on fresh seeds ({a.games * 2} games per cell)", "a")
    # the next night round starts from the winner: a combo only if it beats the base on rank without losing PR
    win = min(((r, p, v) for r, p, v, _ in scored2 if v == "default" or (r < b2_rank and p >= b2_pr - 0.001)),
              default=(b2_rank, b2_pr, "default"))
    best = dict(combos)[win[2]]
    (out / "best.json").write_text(json.dumps(best, indent=1), encoding="utf-8")
    with open(out / "summary.md", "a", encoding="utf-8") as f:
        f.write(f"## Winner of this round: {win[2]} · mean rank {win[0]:.2f} · PR {win[1]:.4f}\n\n`{json.dumps(best)}`\n")
    print("done", flush=True)


if __name__ == "__main__":
    main()
