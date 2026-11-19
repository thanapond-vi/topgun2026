"""Why do we lose rank 1 in arena20? Compare our sales with the winner of each game.

    python mock/diag_winner.py --games 200          # day-20 shape, default mix
    python mock/diag_winner.py --games 200 --capacity 4

For every team it records, per buyer type: kg sold, revenue, and the true TV of what was sold
(markup = revenue / true TV), plus the TV left unsold (salvage 0.8). Display only; nothing else changes.
"""
import argparse
import os
import importlib
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import arena20  # noqa: E402
from mock_market import BAG, RULES, tv_of  # noqa: E402

TYPES = ("SPECIALTY", "COMMERCIAL", "INSTANT")
_stats = {}          # team -> {"kg": {t}, "rev": {t}, "tv": {t}, "by_round": {r: kg}}
_orig_clear = arena20.clear


def _team(tid):
    return _stats.setdefault(tid, {"kg": defaultdict(float), "rev": defaultdict(float), "tv": defaultdict(float),
                                   "offered": defaultdict(float), "by_round": defaultdict(float)})


def _clear(m, practice=False):
    _orig_clear(m, practice)
    if practice:
        return
    rtype = {r["rfq_id"]: r["buyer_type"] for r in m.rfqs}
    for o in m.my_offers:
        lot = m._lot(o["lot_id"])
        t = rtype[o["rfq_id"]]
        s = _team("T01")
        s["offered"][t] += o["kg"]
        f = o.get("filled_kg") or 0
        if f:
            s["kg"][t] += f
            s["rev"][t] += f * o["price_per_kg"]
            s["tv"][t] += f * tv_of(lot["true"])
            s["by_round"][m.round_no] += f
    for rv in m.rivals:
        for o in rv.sent:
            s = _team(rv.tid)
            t = o["btype"]
            s["offered"][t] += o["kg"]
            f = o.get("filled") or 0
            if f and o.get("status") in ("ACCEPTED", "PARTIAL"):
                s["kg"][t] += f
                s["rev"][t] += f * o["price_per_kg"]
                s["tv"][t] += f * tv_of(o["lot"]["true"])
                s["by_round"][m.round_no] += f


arena20.clear = _clear


def row(s, total_tv, total_kg):
    out = {}
    for t in TYPES:
        out[t] = (s["kg"][t] / BAG, s["rev"][t] / s["tv"][t] if s["tv"][t] else None)
    sold_tv = sum(s["tv"].values())
    out["sold_frac"] = sum(s["kg"].values()) / total_kg
    out["unsold_tv_frac"] = (total_tv - sold_tv) / total_tv
    out["mk_sold"] = sum(s["rev"].values()) / sold_tv if sold_tv else 0
    out["early"] = sum(v for r, v in s["by_round"].items() if r <= 5) / BAG
    out["fest"] = sum(v for r, v in s["by_round"].items() if 6 <= r <= 8) / BAG
    out["late"] = sum(v for r, v in s["by_round"].items() if r >= 13) / BAG
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--games", type=int, default=100)
    p.add_argument("--seed", type=int, default=7000)
    p.add_argument("--rounds", type=int, default=15)
    p.add_argument("--capacity", type=int, default=5)
    p.add_argument("--practice", type=int, default=2)
    p.add_argument("--mix", default=arena20.DEFAULT_MIX)
    p.add_argument("--teams", type=int, default=56)
    p.add_argument("--lots", type=int, default=5)
    p.add_argument("--demand", type=float, default=0.95)
    p.add_argument("--ceil", default="", help="SPEC_lo,SPEC_hi,COM_lo,COM_hi,INST_lo,INST_hi")
    a = p.parse_args()
    if a.ceil:
        v = [float(x) for x in a.ceil.split(",")]
        arena20.CEIL20 = {"SPECIALTY": (v[0], v[1]), "COMMERCIAL": (v[2], v[3]), "INSTANT": (v[4], v[5])}
    a.defect_noise, a.moisture_noise = 0.5, 0.15
    strat, appr = importlib.import_module("strategy"), importlib.import_module("appraisal")
    ours, winners, gaps, win_kind, kinds = [], [], [], defaultdict(int), defaultdict(list)
    prs, bests, top11, ranks = [], [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(a.games):
            _stats.clear()
            res = arena20.play_one(strat, appr, a.seed + i, a, Path(tmp))
            # lots are identical for every team: totals from any rival's lots
            sample_team = next(iter(res["rivals"]))
            m_lots = None
            # recompute totals from our own PR formula inputs: use stats + pr
            best_tid, (best_kind, best_pr) = max(res["rivals"].items(), key=lambda kv: kv[1][1])
            win_kind[best_kind if best_pr > res["pr"] else "ours"] += 1
            gaps.append(best_pr - res["pr"])
            prs.append(res["pr"]); bests.append(best_pr); ranks.append(res["rank"])
            allpr = sorted([pr for _, pr in res["rivals"].values()] + [res["pr"]], reverse=True)
            top11.append(allpr[10])
            tot = _totals.get("tv"), _totals.get("kg")
            ours.append(row(_team("T01"), *tot))
            winners.append((best_kind, row(_team(best_tid), *tot)))
            for tid, (kind, pr) in res["rivals"].items():
                kinds[kind].append(row(_team(tid), *tot))
    n = a.games
    print(f"{n} games · cap {a.capacity} · mix {a.mix}")
    print("winner of the game: " + ", ".join(f"{k} {v}" for k, v in sorted(win_kind.items(), key=lambda x: -x[1])))
    print(f"PR ours {statistics.mean(prs):.4f} · rank mean {statistics.mean(ranks):.1f} top1 {sum(r == 1 for r in ranks) / n:.0%}"
          f" · winner {statistics.mean(bests):.4f} · 11th place {statistics.mean(top11):.4f}")
    print(f"gap to best rival PR: mean {statistics.mean(gaps):+.4f} median {statistics.median(gaps):+.4f}")

    def show(name, rows):
        def avg(f):
            vals = [f(r) for r in rows if f(r) is not None]
            return statistics.mean(vals) if vals else float("nan")
        cells = []
        for t in TYPES:
            cells.append(f"{t[:4]} {avg(lambda r: r[t][0]):4.1f}b @{avg(lambda r: r[t][1]):.3f}")
        print(f"  {name:16} sold {avg(lambda r: r['sold_frac']):5.1%} · mk {avg(lambda r: r['mk_sold']):.3f} · "
              + " · ".join(cells)
              + f" · R1-5 {avg(lambda r: r['early']):4.1f}b fest {avg(lambda r: r['fest']):4.1f}b R13+ {avg(lambda r: r['late']):4.1f}b")
    show("ours", ours)
    show("game winner", [w for _, w in winners])
    for k, rows in sorted(kinds.items()):
        show(k, rows)


_totals = {}
_orig_play = arena20.play_one


def _play(strat, appr, seed, a, tmp):
    orig_setup = arena20.setup

    def setup(m, aa):
        orig_setup(m, aa)
        _totals["tv"] = sum(tv_of(l["true"]) * l["kg_total"] for l in m.lots)
        _totals["kg"] = sum(l["kg_total"] for l in m.lots)
    arena20.setup = setup
    try:
        return _orig_play(strat, appr, seed, a, tmp)
    finally:
        arena20.setup = orig_setup


arena20.play_one = _play

if __name__ == "__main__":
    main()
