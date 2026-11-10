"""Play many mock games fast (no HTTP, no clock) to compare strategies by PR.

    python mock/run_games.py                                  # 30 games with strategy.py + appraisal.py
    python mock/run_games.py --games 100 --strategy strategy --appraisal appraisal
    python mock/run_games.py --defect-noise 0.0               # day-17 style: samples equal the true quality

PR = (revenue + 0.80 x TV of unsold) / TV of all, the organizer's sales score (slides day 17).
Rank is among all sellers in the game (1 = best). Rival behaviour is a guess, so compare strategies
against each other here and confirm on the organizer's sandbox before trusting absolute numbers.
"""
import argparse
import importlib
import random
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from mock_market import RULES, ApiError, Market  # noqa: E402
from memory import Memory  # noqa: E402


def play_one(strat, appr, seed: int, a, tmp: Path) -> dict:
    ns = argparse.Namespace(seed=seed, teams=a.teams, lots=a.lots, rounds=a.rounds, setup_seconds=0,
                            appraisal_seconds=0, round_seconds=0, defect_noise=a.defect_noise,
                            moisture_noise=a.moisture_noise, ceiling_shift=a.ceiling_shift, rival_kinds=a.rival_kinds)
    m = Market(ns)
    m.phase = "APPRAISAL"
    st = m.state()
    tv = appr.estimate_lots(RULES, st["lots"])
    m.appraise({"items": appr.appraisal_items(RULES, st["lots"], tv)})
    memory = Memory(str(tmp / f"memory-{seed}.json"))
    rng = random.Random(seed)
    errors = 0
    for r in range(1, a.rounds + 1):
        m.round_no, m.rfqs, m.my_offers = r, m._new_rfqs(), []
        m.phase, m.phase_ends = "ROUND_OPEN", time.time() + 3600
        st = m.state()
        memory.record_results(st["last_results"])
        ctx = strat.Context(RULES, m.calendar, tv, rng, memory, None, 60)
        offers, _ = strat.decide(st, ctx)
        if offers:
            try:
                m.offer_batch({"offers": offers, "replace_existing": True})
            except ApiError:
                errors += 1
                for o in offers:
                    try:
                        m.offer(o)
                    except ApiError:
                        errors += 1
        memory.record_sent(f"R{r}", offers, st, tv)
        m._clear()
    m._close()
    memory.record_results(m.state()["last_results"])
    rev = m.reveal()
    rivals = m.rival_prs()
    return {"pr": rev["pr"], "appraisal": rev["appraisal_points"], "errors": errors,
            "rank": 1 + sum(pr > rev["pr"] for _, pr in rivals.values()), "sellers": len(rivals) + 1,
            "rivals": rivals}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--games", type=int, default=30)
    p.add_argument("--seed", type=int, default=1000, help="first seed; game i uses seed + i")
    p.add_argument("--strategy", default="strategy", help="module name in track3/ with Context and decide()")
    p.add_argument("--appraisal", default="appraisal", help="module name with estimate_lots() and appraisal_items()")
    p.add_argument("--teams", type=int, default=40)
    p.add_argument("--lots", type=int, default=6)
    p.add_argument("--rounds", type=int, default=8)
    p.add_argument("--defect-noise", type=float, default=0.5)
    p.add_argument("--moisture-noise", type=float, default=0.15)
    p.add_argument("--ceiling-shift", type=float, default=0.0, help="add to every buyer's hidden ceiling (x TV)")
    p.add_argument("--rival-kinds", default="fixed,undercut,adaptive,random", help="comma list cycled over rivals")
    a = p.parse_args()
    strat, appr = importlib.import_module(a.strategy), importlib.import_module(a.appraisal)
    by_kind = defaultdict(list)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(a.games):
            res = play_one(strat, appr, a.seed + i, a, Path(tmp))
            rows.append(res)
            for kind, pr in res["rivals"].values():
                by_kind[kind].append(pr)
    prs = [r["pr"] for r in rows]
    print(f"{a.strategy} + {a.appraisal}: {a.games} games, {a.teams} sellers, {a.rounds} rounds, "
          f"defect noise {a.defect_noise}")
    print(f"  PR        mean {statistics.mean(prs):.4f}  sd {statistics.pstdev(prs):.4f}  "
          f"min {min(prs):.4f}  max {max(prs):.4f}")
    print(f"  rank      mean {statistics.mean(r['rank'] for r in rows):.1f} of {rows[0]['sellers']}")
    print(f"  appraisal mean {statistics.mean(r['appraisal'] for r in rows):.2f} / 20 points")
    print(f"  rejected offers {sum(r['errors'] for r in rows)}")
    print("  rival PR by kind: " + ", ".join(f"{k} {statistics.mean(v):.4f}" for k, v in sorted(by_kind.items())))


if __name__ == "__main__":
    main()
