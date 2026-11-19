"""Round-by-round report from the agent's own logs, for whoever watches the game (person or AI assistant).

    python tools/round_report.py                    # newest game in logs/games/
    python tools/round_report.py Competition-Day18  # a given game folder
    python tools/round_report.py --last 3           # only the last 3 rounds in detail

Reads logs/games/<game>/history.jsonl and decisions.jsonl only. Never calls the market or the LLM.
Ends with the signals used by Net-market/17-race-day-playbook.md.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
BAG = 60
sys.path.insert(0, str(HERE))
import strategy  # noqa: E402  (spec_ok, active settings)


def load(path: Path) -> list:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def newest_game() -> Path:
    games = [p for p in (HERE / "logs" / "games").iterdir() if p.is_dir()]
    return max(games, key=lambda p: max((f.stat().st_mtime for f in p.iterdir()), default=0))


def order(rid: str) -> tuple:
    return (0 if rid.startswith("P") else 1, int("".join(c for c in rid if c.isdigit()) or 0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("game", nargs="?")
    ap.add_argument("--last", type=int, default=0)
    ap.add_argument("--brief", action="store_true", help="one line per round + signals, no offer lines (fewer tokens)")
    a = ap.parse_args()
    gdir = HERE / "logs" / "games" / a.game if a.game else newest_game()
    hist, dec = load(gdir / "history.jsonl"), load(gdir / "decisions.jsonl")
    # the practice market can hand out the same lots again, so one folder may hold several games: keep the newest
    starts = [d["time"] for d in dec if d.get("event") == "new_game"]
    if starts:
        dec = [d for d in dec if d.get("time", "") >= starts[-1]]
        hist = [h for h in hist if h.get("time", "") >= starts[-1]] or hist[-1:]
    if not hist:
        sys.exit(f"no history in {gdir}")
    tv = hist[-1]["tv"]
    lots0 = {}
    rfqs, results, standings = {}, {}, {}
    for h in hist:
        st = h["state"]
        for l in st.get("lots") or []:
            lots0.setdefault(l["lot_id"], l)
        rnd = st.get("round") or {}
        if st.get("phase") == "ROUND_OPEN" and rnd.get("round_id"):
            rfqs[rnd["round_id"]] = {r["rfq_id"]: r for r in st.get("rfqs") or []}
        lr = st.get("last_results") or {}
        if lr.get("round_id"):
            results[lr["round_id"]] = lr
            standings[lr["round_id"]] = (st.get("standings") or {}, st.get("lots") or [])
    sent = {d["round"]: d for d in dec if d.get("event") == "offers_sent"}
    alerts = [d for d in dec if d.get("event") in ("strategy_crashed", "state_error", "offers_error", "offer_rejected",
                                                    "round_already_sent", "appraisal_error", "backfill_error")]
    total_tv = sum(tv.get(l, 0) * x["kg_total"] for l, x in lots0.items())
    print(f"game {gdir.name} · {len(lots0)} lots · {sum(x['kg_total'] for x in lots0.values()) // BAG} bags · TV {total_tv:,.0f}")
    act = {k: getattr(strategy, k) for k in ("WAR_MODE", "SHORT_FILL", "WAR_STEP_UP", "FLOOR_END_MARGIN", "LAST_CAP_INST",
                                              "MAX_PER_RFQ", "MAX_PER_RFQ_LATE", "ANCHOR_UNDER", "PACE", "STEP_UP")}
    print("settings now: " + " · ".join(f"{k}={v}" for k, v in act.items()))
    rids = sorted(set(results) | set(sent), key=order)
    shown = rids[-a.last:] if a.last else rids
    inst_fill, our_fill, prs = {}, {}, {}
    for rid in rids:
        res = results.get(rid) or {}
        summ = {s["rfq_id"]: s for s in res.get("market_summary") or [] if s.get("rfq_id")}
        mine = res.get("my_offers") or []
        w = sum(s.get("wanted_kg") or 0 for s in summ.values() if s.get("buyer_type") == "INSTANT")
        b = sum(s.get("bought_kg") or 0 for s in summ.values() if s.get("buyer_type") == "INSTANT")
        if w:
            inst_fill[rid] = b / w
        off = sum(o["kg"] for o in mine if o["status"] not in ("SPEC_MISMATCH", "CANCELLED"))
        if off:
            our_fill[rid] = sum(o.get("filled_kg") or 0 for o in mine) / off
        stg, lots = standings.get(rid, ({}, []))
        if lots and total_tv:
            left = sum(tv.get(l["lot_id"], 0) * l["kg_remaining"] for l in lots)
            prs[rid] = (stg.get("revenue_thb", 0) + 0.8 * left) / total_tv
        if rid not in shown:
            continue
        d = sent.get(rid, {})
        print(f"\n── {rid} · sent {d.get('n', 0)} offers {d.get('kg', 0) // BAG} bags ({d.get('source', '-')})"
              + (f" · our fill {our_fill[rid]:.0%}" if rid in our_fill else "")
              + (f" · Instant market fill {inst_fill[rid]:.0%}" if rid in inst_fill else "")
              + (f" · PR now {prs[rid]:.3f}" if rid in prs else ""))
        if a.brief:
            lost = [o for o in mine if o["status"] in ("OUTBID", "ABOVE_LIMIT")]
            if lost:
                worst = []
                for o in lost:
                    s2 = summ.get(o["rfq_id"], {})
                    t2 = tv.get(o["lot_id"]) or 0
                    worst.append(f"{str(s2.get('buyer_type', '?'))[:4]} {o['status'][:5]} @{o['price_per_kg'] / t2:.3f}" if t2 else o["status"])
                print("   lost: " + " · ".join(worst))
            continue
        for n in d.get("notes") or []:
            if str(n).startswith("guard: price war") or "never sold" in str(n):
                print(f"   note: {str(n)[:110]}")
        for o in mine:
            s = summ.get(o["rfq_id"], {})
            t = tv.get(o["lot_id"]) or 0
            mk = o["price_per_kg"] / t if t else 0
            rq = (rfqs.get(rid) or {}).get(o["rfq_id"]) or {}
            fit = [l for l, x in lots0.items() if rq.get("spec") and strategy.spec_ok(x.get("sample") or {}, rq["spec"])]
            mkt = ""
            if s.get("price_max") is not None:
                mkt = f"market sold {s['price_min']}–{s['price_max']}"
                if len(fit) == 1 and tv.get(fit[0]):
                    mkt += f" (max {s['price_max'] / tv[fit[0]]:.3f}×TV)"
            else:
                mkt = "nobody sold"
            mark = "✓" if o["status"] in ("ACCEPTED", "PARTIAL") else "✗"
            print(f" {mark} {o['rfq_id']:<7} {str(s.get('buyer', '?'))[:14]:<14} {str(s.get('buyer_type', '?'))[:4]:<4}"
                  f" {(o.get('filled_kg') or 0) // BAG}/{o['kg'] // BAG}b @{mk:.3f} {o['status']:<12} {mkt}"
                  f" · {s.get('offers', '?')} offers for {(s.get('wanted_kg') or 0) // BAG}b")
    # signals for the playbook
    real = [r for r in rids if r.startswith("R")]
    last2 = [r for r in real if r in results][-2:]          # rounds whose results are in
    print("\n== signals (playbook)")
    if last2:
        f = [inst_fill[r] for r in last2 if r in inst_fill]
        o = [our_fill[r] for r in last2 if r in our_fill]
        print(f"Instant market fill last 2 rounds: {', '.join(f'{x:.0%}' for x in f) or '-'}"
              f"  (≥{strategy.SHORT_FILL:.0%} = oversupplied → price-war mode switches on by itself)")
        print(f"our fill last 2 rounds: {', '.join(f'{x:.0%}' for x in o) or '-'}")
    lots_now = (hist[-1]["state"].get("lots") or []) or list(lots0.values())   # newest state the agent read
    left_bags = sum(l["kg_remaining"] for l in lots_now) // BAG
    n_total = (hist[-1]["state"].get("round") or {}).get("of") or 15
    n_now = order(real[-1])[1] if real else 0
    print(f"stock left {left_bags} bags · rounds left {max(0, n_total - n_now)} · need ~{left_bags / max(1, n_total - n_now):.1f} bags/round")
    if prs:
        print(f"PR now {list(prs.values())[-1]:.3f} (our TV estimate)")
    zero = [r for r in real if our_fill.get(r) == 0]
    if zero:
        print(f"!! rounds where nothing sold: {', '.join(zero)}")
    if alerts:
        print(f"!! {len(alerts)} alert events: " + ", ".join(sorted({x['event'] for x in alerts})))
    missing = [f"R{i}" for i in range(1, n_now + 1) if f"R{i}" not in sent]
    if missing:
        print(f"!! rounds with no offers logged: {', '.join(missing)}")


if __name__ == "__main__":
    main()
