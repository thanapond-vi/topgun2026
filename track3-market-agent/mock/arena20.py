"""Arena for stage 2/3: same lots for every team, rivals that copy the rules other teams published on GitHub.

    python mock/arena20.py                      # 30 games, 56 teams, 15 rounds (day-20 calendar)
    python mock/arena20.py --rounds 8           # day-18 shape (all rounds "ปกติ", 25 bags)
    python mock/arena20.py --mix starter=56     # everyone runs the organizer's starter

Differences from run_games.py / mock_market.py (those files are not changed):
  - every team gets the SAME lots and the SAME samples (slides day 18/20: lots differ only in lot_id and order)
  - market_summary price_min/max/avg count only offers that SOLD (seen in the real log 18 Nov: our losing
    offer at 145.16 was above price_max 128.28)
  - buyers like the 18 Nov competition market: 6 buyers, ~11 RFQs per round, one SPECIALTY buyer
    nobody can supply (screen >= 17)
  - 15 rounds: calendar R1-5 normal, R6-8 festival (SPECIALTY demand x2), R9-12 normal, R13-14 near close, R15 last
  - rivals (spotter/ notes, 18 Nov 22:20; simplified, not their code):
      starter   organizer starter, markup 1.00 for everyone, never learns (SunnyLeenawat-like)
      tuned     start Spec 1.10 / Com 1.00 / Inst 0.95, +0.03 sold / -0.03 lost / -0.05 above limit
      spread1   HOT COFFEE TGR-12: start 1.00/1.00/0.97, +0.02 / -0.03 (x1.5 if most lost) / -0.06, x max(0.5, 1.5-progress)
      seeit     See-It TGR-23: start 1.06/0.99/0.93, +0.01 (+0.02 unfilled) / -0.01 (max -0.03), pull 30% to
                price_max/TV + 0.01, 1 bag at a time to the best RFQs, last round ladder 0/-0.03/-0.06
      nuxeus    NU XEUS TGR-16: start 1.085/1.03/0.935, calendar phases with floors, lost -> clearing - 0.012,
                crowded SPECIALTY (offers per bag >= 1.8): 1 probe bag there, rest to COMMERCIAL
      dumper    sells INSTANT at 0.72-0.80 and the rest at 0.92 (the 76-84 THB minimums seen 18 Nov)
"""
import argparse
import importlib
import json
import math
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

import mock_market  # noqa: E402
from mock_market import BAG, RULES, ApiError, Market, passes, tv_of  # noqa: E402
from memory import Memory  # noqa: E402

BUYERS20 = [  # name, type, RFQs per round, share of demand, spec
    ("1 ดอยช้างโรสเตอร์", "SPECIALTY", 2, 0.07, {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 16}),
    ("2 บางกอกบลูม", "SPECIALTY", 1, 0.07, {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 17}),
    ("3 สยามคอมเมิร์ซ", "COMMERCIAL", 2, 0.22, {"max_defect_pct": 7.0, "max_moisture_pct": 12.5, "min_screen": None}),
    ("4 ชุมพรคั่วบด", "COMMERCIAL", 2, 0.19, {"max_defect_pct": 7.0, "max_moisture_pct": 12.5, "min_screen": None}),
    ("5 อินสแตนท์ไทย", "INSTANT", 2, 0.27, {"max_defect_pct": 15.0, "max_moisture_pct": 14.0, "min_screen": None}),
    ("6 ทรีอินวันฟู้ดส์", "INSTANT", 2, 0.18, {"max_defect_pct": 15.0, "max_moisture_pct": 14.0, "min_screen": None}),
]
# hidden ceiling x true TV. SPECIALTY: our 1.15 came back OUTBID (not ABOVE_LIMIT) on 18 Nov, so >= 1.15.
# COMMERCIAL: 1.06 was ABOVE_LIMIT in practice. INSTANT: sold <= ~0.97.
CEIL20 = {"SPECIALTY": (1.15, 1.30), "COMMERCIAL": (0.99, 1.06), "INSTANT": (0.91, 0.98)}
START = {"starter": (1.00, 1.00, 1.00), "tuned": (1.10, 1.00, 0.95), "spread1": (1.00, 1.00, 0.97),
         "seeit": (1.06, 0.99, 0.93), "nuxeus": (1.085, 1.03, 0.935), "dumper": (0.92, 0.92, 0.76),
         "volume": (1.00, 0.96, 0.90), "latedump": (1.10, 1.00, 0.95), "t45": (1.13, 1.02, 0.92),
         "nux16": (1.10, 1.03, 0.935), "tgr20": (1.05, 1.00, 0.90), "tgr13": (1.05, 1.00, 0.93),
         "tgr41": (1.06, 1.00, 0.93)}
#   tgr41  สาดบัค: price under the ceiling it learned per buyer, +0.01 sold / -0.02 lost, and from 60% of the game
#          lowers 0.02 per round more so the stock sells out (practice PR 0.921) · simplified from the spotter summary
MIX_THREAT = "seeit=12,tgr13=12,nux16=8,tgr20=8,tgr41=6,spread1=5,tuned=2,dumper=2"
# spotter 20 Nov 04:30 (simplified, not their code):
#   nux16  NU XEUS Gen 16: slow R1-R4 (pace 0.75), fast after R5 (1.65); Instant-only lots fast early; holds Specialty-grade
#          for SPECIALTY >= 1.075 until R12; Specialty 1.08-1.13, Commercial >= 0.995; last 2 rounds caps S 0.98 C 0.95 I 0.88
#   tgr20  REAI CMU: start S 1.05 / C 1.00 / I 0.90, +0.01 when nothing lost, -0.02 when lost, one probe bag at +0.02
#   tgr13  ModEngi market3: price at min(last cut-off, average sold) per buyer minus 0.005, 1 bag at a time, floor 0.85
MIX_SCOUT = "starter=9,tuned=8,nux16=8,seeit=6,tgr13=6,tgr20=6,spread1=5,t45=5,dumper=2"
# "t45" (19 Nov): modelled on a real day-18 agent's memory file (team T45, 21/25 bags sold): 1 bag per offer;
# Specialty starts 1.13 and drops 0.02 every round it does not sell; Commercial +0.02 after a sale, -0.01 after a
# loss; Instant +0.02 after a sale, -0.02 after a loss; one extra Specialty "probe" bag at +0.03 each round.
# --world decline (19 Nov): prices fall during the game like 18 Nov. Specialty buyers' ceilings fall DECLINE["SPECIALTY"]
# per round from R1 (real: cut-off 1.03 -> 0.97 over R1-R3), Instant ceilings fall DECLINE["INSTANT"] per round after
# INST_DECLINE_FROM (real: best R2-R5, then down), and "latedump" rivals (like tuned) dump everything left from 60% of
# the game at 0.80-0.86 (real: Specialty average 127 -> 115 because some teams sold cheap late).
DECLINE = {"SPECIALTY": 0.0, "INSTANT": 0.0}
SPEC_DRIFT = 0.0     # learning rivals ask this much less for SPECIALTY every round (they undercut each other)
INST_DECLINE_FROM = 5
MIX_DECLINE = "starter=12,tuned=12,latedump=8,spread1=7,seeit=7,nuxeus=7,dumper=2"


def ceil_factor(rnd: int, btype: str, practice: bool) -> float:
    if practice:
        return 1.0
    if btype == "SPECIALTY":
        return 1.0 - DECLINE["SPECIALTY"] * (rnd - 1)
    if btype == "INSTANT":
        return 1.0 - DECLINE["INSTANT"] * max(0, rnd - INST_DECLINE_FROM)
    return 1.0
# --world short (19 Nov, user's view): buyers want less than the teams bring, so most teams accept a thin margin to
# sell at all. "volume" rivals: lost -> 0.01 under the last clearing price, sold -> +0.005, floor 0.85, sell fast.
DEFAULT_MIX = "starter=12,tuned=18,spread1=5,seeit=6,nuxeus=6,dumper=8"
# --ceil real18 (19 Nov): buyers pay more and fewer teams dump, so the day-18 shape (8 rounds, 1 practice) gives
# winner ~0.976 and 11th ~0.958 like the real board (0.976 / 0.955). The default above is harsher (winner ~0.94).
CEIL_REAL18 = {"SPECIALTY": (1.15, 1.30), "COMMERCIAL": (1.02, 1.10), "INSTANT": (0.97, 1.04)}
MIX_REAL18 = "starter=12,tuned=20,spread1=7,seeit=7,nuxeus=7,dumper=2"
MIX_SHORT = "volume=30,tuned=8,spread1=5,seeit=4,nuxeus=4,starter=2,dumper=2"


def calendar20(rounds):
    cal = []
    for i in range(1, rounds + 1):
        if rounds < 15:
            label, w, spec = "ปกติ", 1.0, 1.0
        elif 6 <= i <= 8:
            label, w, spec = "เทศกาลกาแฟ", 1.2, 2.0
        elif i in (13, 14):
            label, w, spec = "ใกล้ปิดตลาด", 0.75, 1.0
        elif i == 15:
            label, w, spec = "รอบสุดท้าย", 0.55, 1.0
        else:
            label, w, spec = "ปกติ", 1.0, 1.0
        cal.append({"round": f"R{i}", "rounds": str(i), "label": label, "demand": w, "demand_weight": w,
                    "spec_boost": spec})
    return cal


class Bot:
    def __init__(self, tid, kind, lots, rng):
        self.tid, self.kind, self.lots, self.rng = tid, kind, lots, rng
        s, c, i = START[kind]
        self.k = {"SPECIALTY": s, "COMMERCIAL": c, "INSTANT": i}   # per buyer name after the first result
        self.ceil = {}
        self.sent = []

    def mk(self, buyer, btype):
        return self.k.get(buyer, self.k[btype])

    def offers(self, rfqs, left, cap, rnd, rounds, cal, crowd):
        progress = (rnd - 1) / max(1, rounds)
        last = left <= 1
        pace = 2.0 if self.kind in ("spread1", "seeit", "volume") else 1.3
        if self.kind == "nux16":
            pace = 0.75 if rnd <= 4 else 1.05 if rnd == 5 else 1.65
        out, room = [], cap
        budgets = {id(l): min(l["kg_remaining"], max(BAG, math.ceil(l["kg_remaining"] / max(left, 1) * pace / BAG) * BAG))
                   for l in self.lots}
        if last or (self.kind == "latedump" and progress >= 0.6):
            budgets = {id(l): l["kg_remaining"] for l in self.lots}
        pairs = []
        for lot in self.lots:
            for r in rfqs:
                if passes(lot["sample"], r["spec"]):
                    m = self.price_mk(r, lot, progress, last, cal, crowd)
                    if m is None:
                        continue
                    pairs.append((m * tv_of(lot["sample"]) if self.kind != "seeit" else m, lot, r, m))
        pairs.sort(key=lambda x: -x[0] * self.rng.uniform(0.99, 1.01))
        rfq_left = {r["rfq_id"]: r["kg"] for r in rfqs}
        one_bag = self.kind in ("seeit", "t45", "tgr13")
        for _ in range(3 if one_bag else 1):
            for _, lot, r, m in pairs:
                b = budgets[id(lot)]
                if room < BAG or b < BAG or rfq_left[r["rfq_id"]] < BAG:
                    continue
                if self.kind == "nuxeus" and r["buyer_type"] == "SPECIALTY" and crowd.get(r["buyer"], 0) >= 1.8:
                    kg = BAG
                else:
                    kg = BAG if one_bag else min(b, rfq_left[r["rfq_id"]], room) // BAG * BAG
                existing = next((o for o in out if o["lot"] is lot and o["rfq_id"] == r["rfq_id"]), None)
                if existing:
                    if not one_bag:
                        continue
                    existing["kg"] += BAG
                else:
                    out.append({"team": self.tid, "lot": lot, "rfq_id": r["rfq_id"], "kg": kg, "mk": m,
                                "buyer": r["buyer"], "btype": r["buyer_type"],
                                "price_per_kg": round(m * tv_of(lot["sample"]), 2)})
                budgets[id(lot)] -= kg
                rfq_left[r["rfq_id"]] -= kg
                room -= kg
        if self.kind == "seeit" and last:                 # last round ladder
            for j, o in enumerate(sorted(out, key=lambda o: o["mk"])):
                o["mk"] = max(0.85, o["mk"] - (0, 0.03, 0.06)[min(j, 2)])
                o["price_per_kg"] = round(o["mk"] * tv_of(o["lot"]["sample"]), 2)
        self.sent = out
        return out

    def price_mk(self, r, lot, progress, last, cal, crowd):
        t, b = r["buyer_type"], r["buyer"]
        m = self.mk(b, t)
        if self.kind == "dumper" or (self.kind == "latedump" and progress >= 0.6):
            if self.kind == "latedump":
                return self.rng.uniform(0.80, 0.86) if t == "INSTANT" else self.rng.uniform(0.86, 0.92)
            return self.rng.uniform(0.72, 0.80) if t == "INSTANT" else 0.92
        if self.kind == "starter":
            return 1.0
        if self.kind == "nuxeus":
            fest = "เทศกาล" in cal["label"]
            if last or cal["demand_weight"] < 0.75:
                floor = {"SPECIALTY": 0.88, "COMMERCIAL": 0.88, "INSTANT": 0.83}[t]
                m -= 0.042 if last else 0.022
            elif fest:
                floor = {"SPECIALTY": 1.02, "COMMERCIAL": 0.985, "INSTANT": 0.89}[t]
            elif progress <= 0.36:
                floor = {"SPECIALTY": 1.01, "COMMERCIAL": 0.985, "INSTANT": 0.89}[t]
            elif progress <= 0.72:
                floor = {"SPECIALTY": 0.96, "COMMERCIAL": 0.96, "INSTANT": 0.88}[t]
            else:
                floor = {"SPECIALTY": 0.92, "COMMERCIAL": 0.92, "INSTANT": 0.86}[t]
            m = max(m, floor)
        if self.kind == "nux16":
            if last or progress >= 13 / 15:          # last 2 of 15 rounds (price_mk has no round count)
                m = min(m, {"SPECIALTY": 0.98, "COMMERCIAL": 0.95, "INSTANT": 0.88}[t])
            elif t == "SPECIALTY":
                m = min(max(m, 1.08), 1.13)
            elif t == "COMMERCIAL":
                m = max(m, 0.995)
            elif passes(lot["sample"], {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 16}) and progress < 0.8:
                return None                                  # keep Specialty-grade lots away from INSTANT until R12
        if SPEC_DRIFT and t == "SPECIALTY" and self.kind not in ("starter", "dumper"):
            m -= SPEC_DRIFT * round(progress * 15)
        if self.kind == "seeit" and "เทศกาล" in cal["label"]:
            m += 0.01
        if b in self.ceil:
            m = min(m, self.ceil[b] - 0.02)
        return max(0.83, min(1.40, m))

    def learn(self, summary, rnd, rounds):
        by_buyer = defaultdict(list)
        for o in self.sent:
            by_buyer[(o["buyer"], o["btype"])].append(o)
        progress = rnd / max(1, rounds)
        for (b, t), os_ in by_buyer.items():
            m = sum(o["mk"] for o in os_) / len(os_)
            sold = [o for o in os_ if o["status"] in ("ACCEPTED", "PARTIAL")]
            lost = [o for o in os_ if o["status"] == "OUTBID"]
            above = [o for o in os_ if o["status"] == "ABOVE_LIMIT"]
            if above:
                self.ceil[b] = min(self.ceil.get(b, 9), min(o["mk"] for o in above))
            if self.kind in ("starter", "dumper"):
                continue
            if self.kind in ("tuned", "latedump"):
                new = m - 0.05 if above else m - 0.03 if lost and not sold else m + 0.03 if not lost else m
            elif self.kind == "spread1":
                kk = max(0.5, 1.5 - progress)
                kg = sum(o["kg"] for o in os_)
                d = (0.02 * sum(o["filled"] for o in sold) - 0.03 * sum(o["kg"] for o in lost)
                     - 0.06 * sum(o["kg"] for o in above)) / kg
                new = m + d * kk
            elif self.kind == "seeit":
                if above:
                    new = min(m, self.ceil[b] - 0.02)
                elif lost and not sold:
                    new = m - 0.01
                elif sold and not lost:
                    unfilled = any(s["bought_kg"] < s["wanted_kg"] for s in summary.values() if s["buyer"] == b)
                    new = m + (0.02 if unfilled else 0.01)
                else:
                    new = m
                new = max(new, m - 0.03)
                anchors = [s["price_max"] / tv_of(o["lot"]["sample"]) for o in os_
                           for s in [summary.get(o["rfq_id"])] if s and s.get("price_max")]
                if anchors:
                    new += 0.3 * (min(anchors) + 0.01 - new)
            elif self.kind == "tgr41":
                new = (m + 0.01 if sold and not lost else m - 0.02 if lost else m) - (0.02 if progress >= 0.6 else 0.0)
                if above:
                    new = min(new, self.ceil[b] - 0.02)
            elif self.kind == "tgr20":
                new = m + 0.01 if not lost and not above else m - 0.02
            elif self.kind == "tgr13":
                cuts = [(s["price_max"] / tv_of(o["lot"]["sample"]), s["price_avg"] / tv_of(o["lot"]["sample"]))
                        for o in os_ for s in [summary.get(o["rfq_id"])] if s and s.get("price_max")]
                new = max(0.85, min(min(c, a) for c, a in cuts) - 0.005) if cuts else (m - 0.02 if lost else m)
            elif self.kind == "t45":
                if t == "SPECIALTY":
                    new = m if sold else m - 0.02
                elif t == "COMMERCIAL":
                    new = m + 0.02 if sold and not lost else m - 0.01 if lost and not sold else m
                else:
                    new = m + 0.02 if sold and not lost else m - 0.02 if lost and not sold else m
                if above:
                    new = min(new, self.ceil[b] - 0.02)
            elif self.kind == "volume":
                clear = [s["price_max"] / tv_of(o["lot"]["sample"]) for o in os_
                         for s in [summary.get(o["rfq_id"])] if s and s.get("price_max")]
                if lost and not sold:
                    new = min(m - 0.02, min(clear) - 0.01) if clear else m - 0.02
                elif sold and not lost:
                    new = m + 0.005
                else:
                    new = m - 0.005
                new = max(new, 0.85)
            else:  # nuxeus
                clear = [s["price_max"] / tv_of(o["lot"]["sample"]) for o in os_
                         for s in [summary.get(o["rfq_id"])] if s and s.get("price_max")]
                if lost and not sold and clear:
                    new = min(m - 0.012, min(clear) - 0.012)
                elif sold and not lost:
                    new = m + 0.008
                else:
                    new = m
            self.k[b] = max(0.83, min(1.40, new))


def _jitter(q, rng):
    return {"defect_pct": round(max(0.3, q["defect_pct"] + rng.uniform(-2.0, 2.0)), 2),
            "moisture_pct": round(max(9.5, q["moisture_pct"] + rng.uniform(-0.6, 0.6)), 2),
            "screen": int(min(18, max(12, q["screen"] + rng.choice((-1, 0, 0, 1)))))}


def diff_lots(base, rng, t):
    """--diff-lots (20 Nov): a rival gets its own lots (quality moved a little, sample noise kept), then bag counts are
    scaled so its total TV equals ours. Like the practice market, unlike stage 2-3 (same lots for everyone)."""
    out = []
    for i, l in enumerate(base, 1):
        true = _jitter(l["true"], rng)
        sample = {k: (true[k] + (l["sample"][k] - l["true"][k]) if k != "screen" else true[k]) for k in true}
        sample["defect_pct"], sample["moisture_pct"] = round(max(0.0, sample["defect_pct"]), 2), round(sample["moisture_pct"], 2)
        out.append({**l, "lot_id": f"LOT-T{t:02d}-{i}", "true": true, "sample": sample})
    target = sum(tv_of(l["true"]) * l["kg_total"] for l in base)
    have = sum(tv_of(l["true"]) * l["kg_total"] for l in out)
    for l in out:
        bags = max(1, round(l["kg_total"] // BAG * target / have))
        l["kg_total"] = l["kg_remaining"] = bags * BAG
    return out


def setup(m: Market, a):
    """Rebuild a freshly reset Market for the arena: identical lots, day-18 buyers, day-20 calendar, new rivals."""
    rng = m.rng
    target = 25 if a.rounds < 15 else 45
    bags = [l["kg_total"] // BAG for l in m.lots]
    scale = target / sum(bags)
    for l in m.lots:
        b = max(1, round(l["kg_total"] // BAG * scale))
        l["kg_total"] = l["kg_remaining"] = b * BAG
    m.calendar = calendar20(a.rounds)
    m.capacity = a.capacity * BAG
    m.ceiling = {name: rng.uniform(*CEIL20[t]) for name, t, *_ in BUYERS20}
    mix = [(k, int(v)) for k, v in (x.split("=") for x in a.mix.split(","))]
    kinds = [k for k, n in mix for _ in range(n)][: a.teams - 1]
    while len(kinds) < a.teams - 1:
        kinds.append("tuned")
    m.rivals = []
    for t, kind in enumerate(kinds, 2):
        if getattr(a, "diff_lots", False):
            lots = diff_lots(m.lots, rng, t)
        else:
            lots = [{**l, "lot_id": f"LOT-T{t:02d}-{i}", "kg_remaining": l["kg_total"]} for i, l in enumerate(m.lots, 1)]
        m.rivals.append(Bot(f"T{t:02d}", kind, lots, random.Random(rng.random())))
    m.rival_revenue = {rv.tid: 0.0 for rv in m.rivals}
    m.sold_tv = defaultdict(float)      # team -> true TV of the kg it sold (price ratio = revenue / this)
    m.pr_hist = defaultdict(list)       # team -> PR at the end of every real round (TA 19 Nov: every round's PR counts)
    m.crowd = {}


def new_rfqs(m: Market, practice: bool = False):
    cal = {"demand": 1.0, "spec_boost": 1.0} if practice else m.calendar[m.round_no - 1]
    supply = sum(l["kg_total"] for l in m.lots) * m.args.teams
    per_round = supply * m.args.demand / m.rounds * cal["demand"]
    out, n = [], 1
    for name, t, count, share, spec in BUYERS20:
        sh = share * (cal["spec_boost"] if t == "SPECIALTY" else 1.0)
        for _ in range(count):
            bags = max(1, round(per_round * sh / count / BAG * m.rng.uniform(0.7, 1.3)))
            out.append({"rfq_id": f"{'P' if practice else 'R'}{m.round_no}-{n:02d}", "buyer": name, "buyer_id": f"B{name[0]}", "buyer_type": t,
                        "kg": bags * BAG, "bags": bags, "spec": spec})
            n += 1
    return out


def clear(m: Market, practice: bool = False):
    rid = f"P{m.round_no}" if practice else f"R{m.round_no}"
    left = m.rounds if practice else m.rounds - m.round_no + 1
    cal = {"label": "practice", "demand_weight": 1.0, "demand": 1.0, "spec_boost": 1.0} if practice \
        else m.calendar[m.round_no - 1]
    if practice:                                     # practice: offers clear, nothing is really sold
        snap = [(l, l["kg_remaining"]) for l in m.lots] + [(l, l["kg_remaining"]) for rv in m.rivals for l in rv.lots]
        money = (m.revenue, m.kg_sold, dict(m.rival_revenue), dict(m.sold_tv))
    all_offers = [dict(o, team="T01", lot=m._lot(o["lot_id"])) for o in m.my_offers if o["status"] == "OPEN"]
    for rv in m.rivals:
        all_offers += rv.offers(m.rfqs, left, m.capacity, m.round_no, m.rounds, cal, m.crowd)
    summary = []
    for rfq in m.rfqs:
        mine = [o for o in all_offers if o["rfq_id"] == rfq["rfq_id"]]
        ok = []
        for o in mine:
            tv = tv_of(o["lot"]["true"])
            o["filled"] = 0
            if not passes(o["lot"]["true"], rfq["spec"]):
                o["status"] = "SPEC_MISMATCH"
            elif o["price_per_kg"] / tv > m.ceiling[rfq["buyer"]] * ceil_factor(m.round_no, rfq["buyer_type"], practice):
                o["status"] = "ABOVE_LIMIT"
            else:
                o["ratio"] = o["price_per_kg"] / tv
                ok.append(o)
        m.rng.shuffle(ok)
        ok.sort(key=lambda o: round(o["ratio"], 4))
        need, bought, prices = rfq["kg"], 0, []
        for o in ok:
            take = min(o["kg"], need, o["lot"]["kg_remaining"]) // BAG * BAG
            o["filled_kg"] = o["filled"] = take
            if take <= 0:
                o["status"] = "OUTBID"
                continue
            need -= take
            bought += take
            o["lot"]["kg_remaining"] -= take
            o["status"] = "ACCEPTED" if take == o["kg"] else "PARTIAL"
            prices.append(o["price_per_kg"])
            m.sold_tv[o["team"]] += take * tv_of(o["lot"]["true"])
            if o["team"] == "T01":
                m.revenue += take * o["price_per_kg"]
                m.kg_sold += take
            else:
                m.rival_revenue[o["team"]] += take * o["price_per_kg"]
        summary.append({"rfq_id": rfq["rfq_id"], "buyer": rfq["buyer"], "buyer_type": rfq["buyer_type"],
                        "wanted_kg": rfq["kg"], "bought_kg": bought, "offers": len(mine),
                        "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
                        "price_avg": round(sum(prices) / len(prices), 2) if prices else None})
        m.crowd[rfq["buyer"]] = len(mine) / max(1, rfq["bags"])
    by_id = {s["rfq_id"]: s for s in summary}
    for rv in m.rivals:
        rv.learn(by_id, m.round_no, m.rounds)
    cleared = {o["offer_id"]: o for o in all_offers if o["team"] == "T01"}
    for o in m.my_offers:
        c = cleared.get(o.get("offer_id"))
        if c is not None and c["status"] != "OPEN":
            o["status"], o["filled_kg"] = c["status"], c.get("filled_kg", 0)
        o.setdefault("filled_kg", 0)
        if o["status"] == "OPEN":
            o["status"] = "OUTBID"
    if practice:
        for l, kg in snap:
            l["kg_remaining"] = kg
        m.revenue, m.kg_sold, m.rival_revenue = money[0], money[1], money[2]
        m.sold_tv = defaultdict(float, money[3])
    m.results[rid] = {"round_id": rid, "kind": "practice" if practice else "real",
                      "my_offers": [{k: o[k] for k in ("offer_id", "rfq_id", "lot_id", "kg", "price_per_kg", "status", "filled_kg")}
                                    for o in m.my_offers],
                      "market_summary": summary}
    m.last_summary = summary


def play_one(strat, appr, seed, a, tmp):
    ns = argparse.Namespace(seed=seed, teams=a.teams, lots=a.lots, rounds=a.rounds, setup_seconds=0,
                            appraisal_seconds=0, round_seconds=0, defect_noise=a.defect_noise,
                            moisture_noise=a.moisture_noise, ceiling_shift=0.0, rival_kinds="fixed",
                            no_specialty=False, instant_ceilings=None,
                            demand=getattr(a, "demand", 0.95))
    mock_market.RULES["timeline"]["rounds"] = a.rounds
    m = Market(ns)
    setup(m, a)
    m.phase = "APPRAISAL"
    st = m.state()
    tv = appr.estimate_lots(RULES, st["lots"])
    m.appraise({"items": appr.appraisal_items(RULES, st["lots"], tv)})
    memory = Memory(str(tmp / f"memory-{seed}.json"))
    rng = random.Random(seed)
    trace = []
    last = None
    plan = [("P", i) for i in range(1, a.practice + 1)] + [("R", r) for r in range(1, a.rounds + 1)]
    for kind, r in plan:
        practice = kind == "P"
        m.round_no, m.rfqs, m.my_offers = r, new_rfqs(m, practice), []
        m.phase, m.phase_ends = "ROUND_OPEN", time.time() + 3600
        st = m.state()
        if practice:
            st["round"] = {"round_id": f"P{r}", "kind": "practice", "number": r, "of": a.rounds,
                           "counts_for_score": False, "calendar": "practice"}
        st["last_results"] = last or {"round_id": None, "my_offers": [], "market_summary": []}
        memory.record_results(st["last_results"])
        ctx = strat.Context(RULES, m.calendar, tv, rng, memory, None, 300)
        offers, _ = strat.decide(st, ctx)
        if offers:
            try:
                m.offer_batch({"offers": offers, "replace_existing": True})
            except ApiError:
                for o in offers:
                    try:
                        m.offer(o)
                    except ApiError:
                        pass
        memory.record_sent(f"{kind}{r}", offers, st, tv)
        clear(m, practice)
        last = m.results[f"{kind}{r}"]
        if not practice:
            trace.append(m.last_summary)
            for tid, lots, rev in [("T01", m.lots, m.revenue)] + [(rv.tid, rv.lots, m.rival_revenue[rv.tid]) for rv in m.rivals]:
                total = sum(tv_of(l["true"]) * l["kg_total"] for l in lots)
                left = sum(tv_of(l["true"]) * l["kg_remaining"] for l in lots)
                m.pr_hist[tid].append((rev + RULES["salvage_ratio"] * left) / total)
    m.round_no = a.rounds
    m._close()
    rev = m.reveal()
    rivals = m.rival_prs()
    rev_of = {"T01": m.revenue, **m.rival_revenue}
    final = {"T01": rev["pr"], **{t: pr for t, (_, pr) in rivals.items()}}
    avg = {t: sum(h) / len(h) for t, h in m.pr_hist.items()}
    ratio = {t: rev_of[t] / m.sold_tv[t] if m.sold_tv[t] else 0.0 for t in final}
    blend = {t: (final[t] + avg[t] + ratio[t]) / 3 for t in final}   # TA: weights not given -> equal weights, a guess

    def rank_of(d):
        return 1 + sum(v > d["T01"] for t, v in d.items() if t != "T01")
    best_tid = max(rivals, key=lambda t: rivals[t][1])
    return {"seed": seed, "sold_bags": m.kg_sold // BAG, "total_bags": sum(l["kg_total"] for l in m.lots) // BAG,
            "revenue": round(m.revenue, 2), "best_rival_pr": round(rivals[best_tid][1], 4), "best_rival_kind": rivals[best_tid][0],
            "pr": rev["pr"], "pr_avg": avg["T01"], "ratio": ratio["T01"], "rank_avg": rank_of(avg),
            "rank_ratio": rank_of(ratio), "rank_blend": rank_of(blend),
            "rank": 1 + sum(pr > rev["pr"] for _, pr in rivals.values()),
            "sellers": len(rivals) + 1, "rivals": rivals, "trace": trace}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--games", type=int, default=30)
    p.add_argument("--seed", type=int, default=3000)
    p.add_argument("--strategy", default="strategy")
    p.add_argument("--appraisal", default="appraisal")
    p.add_argument("--teams", type=int, default=56)
    p.add_argument("--lots", type=int, default=5)
    p.add_argument("--rounds", type=int, default=15)
    p.add_argument("--capacity", type=int, default=5, help="bags per team per round (18 Nov: 300 kg)")
    p.add_argument("--practice", type=int, default=2, help="practice rounds P1..Pn before R1 (day 20: 2)")
    p.add_argument("--mix", default=DEFAULT_MIX)
    p.add_argument("--defect-noise", type=float, default=0.5)
    p.add_argument("--moisture-noise", type=float, default=0.15)
    p.add_argument("--ceil", default="", help="buyer ceilings x TV: SPEC_lo,SPEC_hi,COM_lo,COM_hi,INST_lo,INST_hi"
                   " · 'real18' = calibrated 19 Nov to the real day-18 board (winner ~0.976, 11th ~0.955)")
    p.add_argument("--decline-spec", type=float, default=None, help="--world decline: Specialty ceiling drop per round")
    p.add_argument("--decline-inst", type=float, default=None, help="--world decline: Instant ceiling drop per round after R5")
    p.add_argument("--diff-lots", action="store_true", help="every rival gets its own lots with the same total TV")
    p.add_argument("--demand", type=float, default=0.95, help="buyers' total kg / all teams' kg over the game")
    p.add_argument("--world", default="", help="'short' = real18 ceilings, demand 0.70, mostly thin-margin rivals · 'decline' = prices fall during the game · 'scout' = rivals from the spotter notes (20 Nov)")
    p.add_argument("--per-game", default="", help="append one CSV row per game to this file (log of every game)")
    p.add_argument("--market", action="store_true", help="print the market's sold markups per buyer type and round")
    a = p.parse_args()
    if a.world == "threat":
        a.ceil = a.ceil or "real18"
        if a.mix == DEFAULT_MIX:
            a.mix = MIX_THREAT
    if a.world == "scout":
        a.ceil = a.ceil or "real18"
        if a.mix == DEFAULT_MIX:
            a.mix = MIX_SCOUT
    if a.world == "decline":
        a.ceil = a.ceil or "real18"
        DECLINE["SPECIALTY"] = a.decline_spec if a.decline_spec is not None else 0.0
        global SPEC_DRIFT
        SPEC_DRIFT = 0.006
        DECLINE["INSTANT"] = a.decline_inst if a.decline_inst is not None else 0.008
        if a.mix == DEFAULT_MIX:
            a.mix = MIX_DECLINE
    if a.world == "short":
        a.ceil = a.ceil or "real18"
        if a.demand == 0.95:
            a.demand = 0.70
        if a.mix == DEFAULT_MIX:
            a.mix = MIX_SHORT
    if a.ceil:
        global CEIL20
        if a.ceil == "real18":
            CEIL20 = dict(CEIL_REAL18)
            if a.mix == DEFAULT_MIX:
                a.mix = MIX_REAL18
        else:
            v = [float(x) for x in a.ceil.split(",")]
            CEIL20 = {"SPECIALTY": (v[0], v[1]), "COMMERCIAL": (v[2], v[3]), "INSTANT": (v[4], v[5])}
    strat, appr = importlib.import_module(a.strategy), importlib.import_module(a.appraisal)
    rows, by_kind = [], defaultdict(list)
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(a.games):
            res = play_one(strat, appr, a.seed + i, a, Path(tmp))
            rows.append(res)
            if a.per_game:
                cols = ("seed", "pr", "rank", "pr_avg", "rank_avg", "ratio", "rank_ratio", "rank_blend", "sold_bags",
                        "total_bags", "revenue", "best_rival_pr", "best_rival_kind")
                new = not Path(a.per_game).exists()
                with open(a.per_game, "a", encoding="utf-8") as f:
                    if new:
                        f.write("game,rounds,capacity,ceil,demand,mix," + ",".join(cols) + "\n")
                    f.write(f"{i + 1},{a.rounds},{a.capacity},{a.ceil or 'default'},{getattr(a, 'demand', 0.95)},"
                            f"\"{a.mix}\"," + ",".join(str(round(res[c], 4) if isinstance(res[c], float) else res[c]) for c in cols) + "\n")
            for kind, pr in res["rivals"].values():
                by_kind[kind].append(pr)
    prs = [r["pr"] for r in rows]
    ranks = [r["rank"] for r in rows]
    print(f"arena20: {a.games} games · {a.teams} teams · {a.rounds} rounds · cap {a.capacity} bags · mix {a.mix}")
    print(f"  ours  PR mean {statistics.mean(prs):.4f} sd {statistics.pstdev(prs):.4f} min {min(prs):.4f}"
          f" · rank mean {statistics.mean(ranks):.1f} median {statistics.median(ranks):.0f} of {rows[0]['sellers']}"
          f" · top-5 {sum(r <= 5 for r in ranks)}/{len(ranks)}")
    print("  rivals PR: " + ", ".join(f"{k} {statistics.mean(v):.4f}" for k, v in sorted(by_kind.items(), key=lambda x: -statistics.mean(x[1]))))
    print("JSON " + json.dumps({"pr": statistics.mean(prs), "pr_sd": statistics.pstdev(prs), "pr_min": min(prs),
                                "rank": statistics.mean(ranks), "rank_median": statistics.median(ranks),
                                "top5": sum(r <= 5 for r in ranks) / len(ranks), "top1": sum(r == 1 for r in ranks) / len(ranks),
                                "rivals": {k: statistics.mean(v) for k, v in by_kind.items()},
                                **{k: statistics.mean(r[k] for r in rows) for k in ("pr_avg", "ratio", "rank_avg", "rank_ratio", "rank_blend")},
                                "top1_blend": sum(r["rank_blend"] == 1 for r in rows) / len(rows)}))
    if a.market:
        for t in ("SPECIALTY", "COMMERCIAL", "INSTANT"):
            line = []
            for r in range(a.rounds):
                fills = [s for g in rows for s in g["trace"][r] if s["buyer_type"] == t and s["wanted_kg"]]
                f = sum(s["bought_kg"] for s in fills) / max(1, sum(s["wanted_kg"] for s in fills))
                line.append(f"{f:.0%}")
            print(f"  fill {t[:4]} by round: " + " ".join(line))


if __name__ == "__main__":
    main()
