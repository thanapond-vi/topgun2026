"""Market price and our sales by round in arena20 (does a world look like the real market?).

    python mock/price_trend.py --world decline --games 100
Prints, per round, the median sold markup (price / true TV) per buyer type over all teams, the average sold price of
the Specialty buyer in markup terms, and our bags sold / offered and average sold markup. Mock only.
"""
import argparse
import importlib
import statistics as st
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]
import arena20  # noqa: E402
from mock_market import BAG, tv_of  # noqa: E402

mk_all = defaultdict(list)
ours = defaultdict(lambda: [0, 0.0, 0])
_orig = arena20.clear


def clear(m, practice=False):
    _orig(m, practice)
    if practice:
        return
    r = m.round_no
    rtype = {q["rfq_id"]: q["buyer_type"] for q in m.rfqs}
    for rv in m.rivals:
        for o in rv.sent:
            if o.get("status") in ("ACCEPTED", "PARTIAL"):
                mk_all[(r, o["btype"])].append(o["price_per_kg"] / tv_of(o["lot"]["true"]))
    for o in m.my_offers:
        lot = m._lot(o["lot_id"])
        f = o.get("filled_kg") or 0
        ours[r][2] += o["kg"] // BAG
        if f:
            ours[r][0] += f // BAG
            ours[r][1] += f // BAG * o["price_per_kg"] / tv_of(lot["true"])
            mk_all[(r, rtype[o["rfq_id"]])].append(o["price_per_kg"] / tv_of(lot["true"]))


arena20.clear = clear


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--world", default="")
    p.add_argument("--ceil", default="real18")
    p.add_argument("--games", type=int, default=100)
    p.add_argument("--seed", type=int, default=40000)
    p.add_argument("--rounds", type=int, default=15)
    a = p.parse_args()
    args = argparse.Namespace(teams=56, lots=5, rounds=a.rounds, capacity=5, practice=2, mix=arena20.DEFAULT_MIX,
                              defect_noise=0.5, moisture_noise=0.15, demand=0.95, ceil=a.ceil, world=a.world,
                              decline_spec=None, decline_inst=None)
    if a.world == "decline":
        arena20.DECLINE.update({"SPECIALTY": 0.0, "INSTANT": 0.008})
        arena20.SPEC_DRIFT = 0.006
        args.mix = arena20.MIX_DECLINE
    elif a.world == "short":
        args.mix, args.demand = arena20.MIX_SHORT, 0.70
    elif a.ceil == "real18":
        args.mix = arena20.MIX_REAL18
    if a.ceil == "real18":
        arena20.CEIL20 = dict(arena20.CEIL_REAL18)
    strat, appr = importlib.import_module("strategy"), importlib.import_module("appraisal")
    with tempfile.TemporaryDirectory() as t:
        for i in range(a.games):
            arena20.play_one(strat, appr, a.seed + i, args, Path(t))
    print(f"world {a.world or a.ceil} · {a.games} games")
    print("round | market sold markup median SPEC / COMM / INST · mean SPEC | ours sold/offered bags per game @ markup")
    for r in range(1, a.rounds + 1):
        cells = [f"{st.median(mk_all[(r, t)]):.3f}" if mk_all[(r, t)] else "  -  " for t in ("SPECIALTY", "COMMERCIAL", "INSTANT")]
        sp = f"{st.mean(mk_all[(r, 'SPECIALTY')]):.3f}" if mk_all[(r, "SPECIALTY")] else "-"
        s, mk, off = ours[r]
        print(f"R{r:<2} | {' / '.join(cells)} · {sp} | {s / a.games:.1f}/{off / a.games:.1f} @{mk / s if s else 0:.3f}")


if __name__ == "__main__":
    main()
