"""Watch the running game's log and exit only when something needs a decision (saves the AI assistant's tokens).

    python tools/watch.py                 # newest game in logs/games/
    python tools/watch.py --skip M1       # signal already handled: do not wake for it again

Every round it appends one line to logs/watch.log (for people). It exits (prints one short line) when:
  SIGNAL M1   we sold < 60% in the two latest rounds (playbook M1; the rules accept it on the same evidence)
  SIGNAL M2   price-war mode is on but we sold >= 90% two rounds in a row and Instant filled < 90% (check M2)
  ALERT ...   the agent logged a crash/error, or a round has no offers
  CLOSED      the market closed
Reads logs only. Never calls the market or the LLM, never changes files except logs/watch.log.
"""
import argparse
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LOGS = HERE / "logs"


def load(p: Path) -> list:
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def game_dir() -> Path:
    games = [p for p in (LOGS / "games").iterdir() if p.is_dir() and (p / "decisions.jsonl").exists()]
    return max(games, key=lambda p: (p / "decisions.jsonl").stat().st_mtime)


def rounds_info(g: Path) -> tuple[dict, list, list]:
    dec = load(g / "decisions.jsonl")
    starts = [d["time"] for d in dec if d.get("event") == "new_game"]
    since = starts[-1] if starts else ""
    dec = [d for d in dec if d.get("time", "") >= since]
    res = {}
    stock = None
    for h in load(g / "history.jsonl"):
        if h.get("time", "") < since:
            continue
        lots = (h.get("state") or {}).get("lots")
        if lots:
            stock = sum(l.get("kg_remaining") or 0 for l in lots)
        lr = (h.get("state") or {}).get("last_results") or {}
        if str(lr.get("round_id") or "").startswith("R"):
            res[lr["round_id"]] = lr
    stats = {}
    for rid, lr in res.items():
        mine = [o for o in lr.get("my_offers") or [] if o.get("status") not in ("SPEC_MISMATCH", "CANCELLED")]
        kg = sum(o["kg"] for o in mine)
        rows = [r for r in lr.get("market_summary") or [] if r.get("buyer_type") == "INSTANT" and r.get("wanted_kg")]
        w = sum(r["wanted_kg"] for r in rows)
        stats[rid] = {"fill": sum(o.get("filled_kg") or 0 for o in mine) / kg if kg else None,
                      "inst": sum(r.get("bought_kg") or 0 for r in rows) / w if w else None}
    order = sorted(stats, key=lambda r: int(r[1:]))
    stats["_stock_kg"] = stock
    return stats, order, dec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="comma list of signals already handled, e.g. M1,M2")
    ap.add_argument("--poll", type=float, default=3.0)
    a = ap.parse_args()
    skip = {x.strip() for x in a.skip.split(",") if x.strip()}
    seen = None
    while True:
        g = game_dir()
        stats, order, dec = rounds_info(g)
        stock = stats.pop("_stock_kg", None)
        if any(d.get("event") == "market_closed" for d in dec):
            print(f"CLOSED {g.name}")
            return
        bad = [d for d in dec if d.get("event") in ("strategy_crashed", "offers_error", "appraisal_error")]
        if bad:
            print(f"ALERT {bad[-1]['event']} at {bad[-1]['time']} ({g.name})")
            return
        sent = [d for d in dec if d.get("event") == "offers_sent" and str(d.get("round", "")).startswith("R")]
        if sent and sent[-1].get("n", 0) == 0 and sent[-1]["round"] != "R15" and stock != 0:   # sold out = fine
            print(f"ALERT no offers in {sent[-1]['round']} ({g.name})")
            return
        if order and order[-1] != seen:
            seen = order[-1]
            last2 = [stats[r] for r in order[-2:]]
            war = bool(sent) and any("price war" in str(n) for n in sent[-1].get("notes") or [])
            fmt = lambda x: "-" if x is None else f"{x:.0%}"
            line = (f"{time.strftime('%H:%M:%S')} {g.name} {seen}: we sold {fmt(stats[seen]['fill'])} · "
                    f"Instant filled {fmt(stats[seen]['inst'])} · war mode {'on' if war else 'off'}")
            with open(LOGS / "watch.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if len(last2) == 2 and all(s["fill"] is not None and s["inst"] is not None for s in last2):
                # 20 Nov HPC: against TGR-23/TGR-13-heavy rivals M1 helped even when Instant was not oversupplied, so
                # wake on "we sold < 60% two rounds" alone (the rules accept M1 on that too)
                if "M1" not in skip and all(s["fill"] < 0.60 for s in last2):
                    print(f"SIGNAL M1 after {seen}: sold {fmt(last2[0]['fill'])}/{fmt(last2[1]['fill'])}, "
                          f"Instant {fmt(last2[0]['inst'])}/{fmt(last2[1]['inst'])} ({g.name})")
                    return
                if "M2" not in skip and war and all(s["fill"] >= 0.90 for s in last2) and last2[1]["inst"] < 0.90:
                    print(f"SIGNAL M2 after {seen}: war mode on but sold {fmt(last2[0]['fill'])}/{fmt(last2[1]['fill'])}, "
                          f"Instant {fmt(last2[1]['inst'])} ({g.name})")
                    return
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
