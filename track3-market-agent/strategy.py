"""Decide this round's offers.  <- ไฟล์หลักที่ทีมควรแก้

Every round:
  1. candidates()  every (lot, RFQ) pair whose sample meets the RFQ spec
  2. llm_plan()    the LLM picks offers from those candidates: which, how many bags, what markup
  3. rule_plan()   used instead when the LLM is off, too slow, or answers nonsense
  4. guard()       every offer, from the LLM or the rules, is checked before it is sent

price_per_kg = markup × our TV estimate. A markup of 1.00 asks exactly what we think the lot is worth.
"""
import json
import math
import os
import random
import re
from dataclasses import dataclass, field

BAG = 60
# Markup per buyer type. The starter asks 1.00 × TV from everyone.
# TODO: buyer types do not pay the same (Specialty pays the most, Instant the least), and these never change.
#       Price each type differently and move the markups after each round's results
#       (ACCEPTED -> ask a bit more, OUTBID / ABOVE_LIMIT -> ask less) — จุดนี้ปรับแล้วได้ผลมากที่สุด
# Starting markup per buyer type, then learned per buyer. Scored round 17 Nov (56 teams, manual): COMMERCIAL 1.05
# and INSTANT 1.00 sold nothing in round 1; what sold was COMMERCIAL ~0.97-0.99 and INSTANT ~0.93-0.95
# (Net-market/10-scored-round-17nov.md). Override with STRATEGY_MARKUP_COM / _INST / _SPEC.
MARKUP = {"SPECIALTY": float(os.environ.get("STRATEGY_MARKUP_SPEC", "1.15")),
          "COMMERCIAL": float(os.environ.get("STRATEGY_MARKUP_COM", "0.98")),
          "INSTANT": float(os.environ.get("STRATEGY_MARKUP_INST", "0.94"))}
MIN_MARKUP, MAX_MARKUP = 0.81, 1.50   # hard limits; unsold beans go to salvage at 0.80 × TV
PACE = float(os.environ.get("STRATEGY_PACE", "1.3"))   # offer PACE × an even share of each lot per remaining round

# Markup floor per round (see markup_floor(), docs/bidding-strategy.md 10.3 item 5).
FLOOR_EARLY = float(os.environ.get("STRATEGY_FLOOR_EARLY", "1.00"))  # first half (<= N/2): do not undercut below TV
EARLY_HALF = os.environ.get("STRATEGY_EARLY_HALF", "0") == "1"       # 1 = use FLOOR_EARLY up to round N/2 (tested 17 Nov)
# Which lot goes to which RFQ in rule_plan():
#   tvHigh  = best lot first for every buyer (starter behaviour)
#   cheapFit = SPECIALTY gets the best lot; other buyers get the cheapest lot that passes their spec
#   reserve = cheapFit, and up to round N/2 lots that pass a SPECIALTY spec are kept for SPECIALTY buyers
LOT_ORDER = os.environ.get("STRATEGY_LOT_ORDER", "reserve")   # reserve tested best or equal, 17 Nov
# 1 = reserve holds lots only after a SPECIALTY RFQ has been seen (none came on 17 Nov). Off by default: with the
# mock fixed (18 Nov) holding from round 1 scored higher PR in every scenario, even with no SPECIALTY buyer at all.
RESERVE_WAIT_SPEC = os.environ.get("STRATEGY_RESERVE_WAIT_SPEC", "0") == "1"
FLOOR_MID = float(os.environ.get("STRATEGY_FLOOR_MID", "0.97"))      # floor for most of the game (first half too unless EARLY_HALF)
LATE_FROM = 0.8          # over the last 20% of rounds the floor slides down to salvage + FLOOR_END_MARGIN
FLOOR_END_MARGIN = 0.01  # never below salvage: selling under it earns less than not selling
# INSTANT ceilings sit under TV (17 Nov: 0.95 sold, 0.95-0.98 came back ABOVE_LIMIT/OUTBID), so FLOOR_MID 0.97 would
# block every INSTANT sale. INSTANT may go this far under the round's floor (never under salvage + margin).
INSTANT_FLOOR_GAP = float(os.environ.get("STRATEGY_INSTANT_FLOOR_GAP", "0.05"))
TOTAL_TEAMS = int(os.environ.get("STRATEGY_TEAMS", "56"))            # K = 56 sellers in market (43 competing + 13 observer)

# How the markup for one buyer moves after a round (see learned_markups()).
# Practice 18 Nov (rules only, 56 teams): +0.03 after ACCEPTED lost the next round almost every time
# (COMMERCIAL 0.98 sold, 1.01-1.03 OUTBID; Practice-01/02). Small steps = learn demand without losing sales
# (Researcher: Chen-Lyu-Wang-Zhou 2022, price-based NRM with demand learning).
# 18 Nov 16:40 merge: mock (fixed, 5 sets) ranks 0.03 > 0.02 > 0.01 on every set; 0.02 is within 0.003 of 0.03
# and halves the overshoot seen in the real practice games, so 0.02 (docs/bidding-strategy.md 12.9).
STEP_UP = float(os.environ.get("STRATEGY_STEP_UP", "0.02"))   # ACCEPTED: everything sold, ask a bit more next time
STEP_OUTBID = 0.03      # OUTBID: rivals were cheaper (used when the RFQ's closing price is unknown)
OUTBID_UNDERCUT = 0.01  # OUTBID with a known closing price: go this far under the highest price that sold
MAX_DROP = 0.05         # never cut one buyer's markup by more than this in one round: price_max may come from
                        # a rival's lot with a different TV, so price_max / our TV can be far off
CEILING_MARGIN = 0.02   # stay this far under the lowest markup that came back ABOVE_LIMIT
# market_summary: an RFQ that ended unfilled (bought_kg < wanted_kg) had no rival pressure, so an ACCEPTED offer
# there can rise faster. Set STRATEGY_FILL_BONUS=0 to switch this off.
FILL_BONUS = float(os.environ.get("STRATEGY_FILL_BONUS", "0.0"))   # 18 Nov: +0.03+0.02 pushed 1.01 -> 1.06 ABOVE_LIMIT   # unfilled RFQ = rivals lacked matching beans; step up faster
# INSTANT buyers buy under TV (instructor, 18 Nov; scored round 17 Nov sold at 0.88-0.95). Their RFQs end unfilled
# because most offers sit above the ceiling, not because rivals lack beans, so FILL_BONUS never applies to INSTANT,
# and no INSTANT markup (learned, rule or LLM) may reach 1.00.
INSTANT_MAX_MARKUP = float(os.environ.get("STRATEGY_INSTANT_MAX", "0.99"))
# COMMERCIAL across practice games 18 Nov (Practice-01..09): 0.97-1.00 sold 64 / lost 8, 1.01-1.03 sold 10 / lost 15,
# 1.05+ sold 3 / lost 15, 1.10-1.15 never sold. The LLM sometimes gave COMMERCIAL a SPECIALTY-like 1.10-1.15 and lost
# the whole round (Practice-07 R2: 7 bags ABOVE_LIMIT). guard() and markup_for() both clamp to this.
COMMERCIAL_MAX_MARKUP = float(os.environ.get("STRATEGY_COMMERCIAL_MAX", "1.03"))


def type_cap(buyer_type: str) -> float:
    """Highest markup allowed for one buyer type."""
    if buyer_type == "INSTANT":
        return min(INSTANT_MAX_MARKUP, MAX_MARKUP)
    if buyer_type == "COMMERCIAL":
        return min(COMMERCIAL_MAX_MARKUP, MAX_MARKUP)
    return MAX_MARKUP


@dataclass
class Context:
    rules: dict                       # GET /v1/calendar -> rules
    calendar: list                    # GET /v1/calendar -> calendar
    tv: dict                          # lot_id -> our TV estimate (baht/kg)
    rng: random.Random
    memory: object = None
    llm: object = None
    seconds_left: float = 999         # time left in this round when the state was read
    notes: list = field(default_factory=list)
    markups: dict = field(default_factory=dict)   # learned per buyer and per buyer type, set in decide()
    floor: float = MIN_MARKUP                     # lowest markup this round, set in decide()


def learned_markups(memory) -> dict[str, float]:
    """Markup per buyer (by name, plus a fallback per buyer type) learned from every round so far.

    Rebuilt from memory each round, so a restarted agent learns the same thing again.
      ACCEPTED     -> the buyer's hidden ceiling is at least this markup and rivals were not cheaper: ask more
      PARTIAL      -> right at the cut-off of that RFQ: keep this markup
      OUTBID       -> under the ceiling but rivals were cheaper: ask less
      ABOVE_LIMIT  -> the ceiling is below this markup: remember it and stay under it from now on
    """
    out = dict(MARKUP)
    ceiling: dict[str, float] = {}
    type_of: dict[str, str] = {"INSTANT": "INSTANT", "COMMERCIAL": "COMMERCIAL", "SPECIALTY": "SPECIALTY"}
    if memory is None:
        return {k: min(max(v, MIN_MARKUP), type_cap(k)) for k, v in out.items()}
    for rnd in memory.rounds:
        buyer_of = {row["rfq_id"]: (row["buyer"], row["buyer_type"]) for row in rnd.get("market_summary", [])}
        price_max = {row["rfq_id"]: row["price_max"] for row in rnd.get("market_summary", []) if row.get("price_max")}
        unfilled = {row["rfq_id"] for row in rnd.get("market_summary", [])
                    if row.get("bought_kg") is not None and row.get("wanted_kg") and row["bought_kg"] < row["wanted_kg"]}
        sent = {(o["rfq_id"], o["lot_id"]): o for o in memory.sent.get(rnd["round_id"], [])}
        moves: dict[str, list[float]] = {}
        for o in rnd.get("my_offers", []):
            s = sent.get((o["rfq_id"], o["lot_id"]))
            if s is None or o["rfq_id"] not in buyer_of:
                continue
            buyer, btype = buyer_of[o["rfq_id"]]
            type_of[buyer] = btype
            m = s["markup"]
            if o["status"] == "ACCEPTED":
                bonus = FILL_BONUS if o["rfq_id"] in unfilled and btype != "INSTANT" else 0.0
                new = m + STEP_UP + bonus
            elif o["status"] == "PARTIAL":
                new = m
            elif o["status"] == "OUTBID":
                new = m - STEP_OUTBID
                # price_max may count every offer or only the ones that sold (not documented): use it only
                # when it is below our markup, i.e. it can only be a price that beat us
                if o["rfq_id"] in price_max and s.get("price_per_kg") and m > 0:
                    closing = price_max[o["rfq_id"]] / (s["price_per_kg"] / m)
                    if closing < m:
                        new = max(closing - OUTBID_UNDERCUT, m - MAX_DROP)
            elif o["status"] == "ABOVE_LIMIT":
                ceiling[buyer] = min(ceiling.get(buyer, 9.0), m)
                new = m - CEILING_MARGIN - STEP_OUTBID
            else:                                   # SPEC_MISMATCH, CANCELLED: says nothing about price
                continue
            moves.setdefault(buyer, []).append(new)
            moves.setdefault(btype, []).append(new)
        for key, values in moves.items():
            out[key] = min(values)                  # several lots to one buyer: follow the most cautious result
    for buyer, c in ceiling.items():
        out[buyer] = min(out.get(buyer, c), c - CEILING_MARGIN)
    return {k: min(max(v, MIN_MARKUP), type_cap(type_of.get(k, ""))) for k, v in out.items()}


def known_ceilings(memory) -> dict[str, float]:
    """Highest markup each buyer may get from now on: CEILING_MARGIN under the lowest markup it sent back ABOVE_LIMIT."""
    out: dict[str, float] = {}
    for rnd in (memory.rounds if memory is not None else []):
        buyer_of = {row["rfq_id"]: row["buyer"] for row in rnd.get("market_summary", [])}
        sent = {(o["rfq_id"], o["lot_id"]): o for o in memory.sent.get(rnd["round_id"], [])}
        for o in rnd.get("my_offers", []):
            s = sent.get((o["rfq_id"], o["lot_id"]))
            if o["status"] == "ABOVE_LIMIT" and s and s.get("markup") and o["rfq_id"] in buyer_of:
                b = buyer_of[o["rfq_id"]]
                out[b] = min(out.get(b, 9.0), s["markup"] - CEILING_MARGIN)
    return out


def failed_specs(memory) -> dict[str, list[dict]]:
    """lot_id -> RFQ specs that lot failed (SPEC_MISMATCH). The sample said it passed, so its true quality does not.

    Only offers recorded with their spec can be learned from (memory.record_sent keeps it).
    """
    out: dict[str, list[dict]] = {}
    for rnd in (memory.rounds if memory is not None else []):
        sent = {(o["rfq_id"], o["lot_id"]): o for o in memory.sent.get(rnd["round_id"], [])}
        for o in rnd.get("my_offers", []):
            s = sent.get((o["rfq_id"], o["lot_id"]))
            if o["status"] == "SPEC_MISMATCH" and s and s.get("spec"):
                out.setdefault(o["lot_id"], []).append(s["spec"])
    return out


def at_least_as_strict(spec: dict, failed: dict) -> bool:
    """True if every limit in spec is the same as or tighter than in failed: a lot that failed `failed` fails `spec` too."""
    def screen(x):
        return x.get("min_screen") or 0
    return (spec["max_defect_pct"] <= failed["max_defect_pct"] and spec["max_moisture_pct"] <= failed["max_moisture_pct"]
            and screen(spec) >= screen(failed))


def lot_fits(ctx, lot: dict, rfq: dict) -> bool:
    """spec_ok on the sample, and the lot has not already failed an equally strict spec in an earlier round."""
    if not spec_ok(lot["sample"], rfq["spec"]):
        return False
    return not any(at_least_as_strict(rfq["spec"], f) for f in failed_specs(ctx.memory).get(lot["lot_id"], []))


def rival_tracker(state: dict, memory, teams: int = TOTAL_TEAMS) -> dict:
    """Estimate rival inventory across all (K - 1) rival teams from market_summary bought_kg.

    Every team starts with the same total kg (S0 = sum of our lots' kg_total).
    Each round r, rivals sold: sum(bought_kg in market_summary) - sum(our filled_kg).
    """
    s0 = sum(l.get("kg_total") or 0 for l in state.get("lots") or [])
    rivals_count = max(1, teams - 1)
    rivals_initial_kg = rivals_count * s0
    rivals_sold_kg = 0
    last_wanted_kg, last_bought_kg = 0, 0
    unfilled_buyers = []
    if memory is not None:
        # practice rounds do not count for score and may not use up real stock: skip them
        real = [rnd for rnd in memory.rounds if rnd.get("kind") != "practice"]
        for rnd in real:
            ms = rnd.get("market_summary") or []
            mkt_bought = sum(r.get("bought_kg") or 0 for r in ms)
            my_sold = sum(o.get("filled_kg") or 0 for o in rnd.get("my_offers") or [])
            rivals_sold_kg += max(0, mkt_bought - my_sold)
        if real:
            last_ms = real[-1].get("market_summary") or []
            last_wanted_kg = sum(r.get("wanted_kg") or 0 for r in last_ms)
            last_bought_kg = sum(r.get("bought_kg") or 0 for r in last_ms)
            unfilled_buyers = [r.get("buyer") or r.get("rfq_id", "?") for r in last_ms
                               if r.get("bought_kg") is not None and r.get("wanted_kg") and r["bought_kg"] < r["wanted_kg"]]
    rivals_left_kg = max(0, rivals_initial_kg - rivals_sold_kg)
    depletion = rivals_sold_kg / rivals_initial_kg if rivals_initial_kg > 0 else 0.0
    cap = state.get("round_capacity_kg") or max(BAG, s0 // 5)
    effective_rivals = rivals_left_kg / cap if cap > 0 else rivals_count
    rfq_demand_now = sum(r.get("kg") or 0 for r in state.get("rfqs") or [])
    return {
        "teams": teams,
        "rivals_count": rivals_count,
        "s0_kg": s0,
        "rivals_initial_kg": rivals_initial_kg,
        "rivals_sold_kg": rivals_sold_kg,
        "rivals_left_kg": rivals_left_kg,
        "depletion": round(depletion, 3),
        "avg_bags_left_per_rival": round(rivals_left_kg / (rivals_count * BAG), 1),
        "effective_rivals": round(effective_rivals, 1),
        "last_wanted_kg": last_wanted_kg,
        "last_bought_kg": last_bought_kg,
        "last_fill_rate": round(last_bought_kg / last_wanted_kg, 3) if last_wanted_kg > 0 else 1.0,
        "unfilled_buyers": unfilled_buyers,
        # needs s0 > 0: with no lots known yet rivals_left_kg is 0 and every round would look like a squeeze
        "squeeze": bool(s0 > 0 and rfq_demand_now > 0 and rivals_left_kg < rfq_demand_now),
    }


def type_floor(ctx, buyer_type: str) -> float:
    """This round's floor for one buyer type: INSTANT may go INSTANT_FLOOR_GAP under it, never under salvage."""
    if buyer_type != "INSTANT":
        return ctx.floor
    floor_end = ctx.rules.get("salvage_ratio", 0.80) + FLOOR_END_MARGIN
    return max(ctx.floor - INSTANT_FLOOR_GAP, floor_end)


def markup_for(ctx, buyer: str, buyer_type: str) -> float:
    m = ctx.markups if ctx.markups else MARKUP
    cap = min(type_cap(buyer_type), getattr(ctx, "cap", {}).get(buyer_type) or 9.0)
    return min(max(m.get(buyer, m.get(buyer_type, MARKUP.get(buyer_type, 1.0))), type_floor(ctx, buyer_type)), cap)


def markup_floor(state: dict, ctx) -> float:
    """Lowest markup to offer this round.

    FLOOR_MID for most of the game, sliding to salvage + FLOOR_END_MARGIN over the last rounds.
    If we are behind schedule (more beans left than full-capacity rounds after this one can sell),
    drop straight to the end floor so the beans sell instead of going to salvage — UNLESS all rivals
    combined have less stock left than this round's total RFQ demand (squeeze).
    """
    floor_end = ctx.rules.get("salvage_ratio", 0.80) + FLOOR_END_MARGIN
    rnd = state["round"]
    rt = rival_tracker(state, ctx.memory)
    if rnd["kind"] == "practice":
        late = 0.0
    else:
        total = total_rounds(state, ctx)
        late = min(1.0, max(0.0, (rnd["number"] - 1 - LATE_FROM * total) / ((1 - LATE_FROM) * total)))
    floor = FLOOR_MID - (FLOOR_MID - floor_end) * late
    first_half = rnd["kind"] != "practice" and rnd["number"] <= total_rounds(state, ctx) / 2
    if EARLY_HALF and first_half:
        floor = max(floor, FLOOR_EARLY)
    cap = state.get("round_capacity_kg")
    # count only lots some RFQ this round accepts: a lot no buyer can take would otherwise make us look
    # behind schedule and drop the floor for every other lot
    sellable = sum(l["kg_available"] for l in state["lots"]
                   if any(spec_ok(l["sample"], r["spec"]) for r in state["rfqs"]))
    if cap and sellable > cap * (rounds_left(state, ctx) - 1) and not rt["squeeze"]:
        floor = floor_end
    return max(min(floor, max(FLOOR_MID, FLOOR_EARLY if EARLY_HALF and first_half else FLOOR_MID)), floor_end)


def spec_ok(sample: dict, spec: dict) -> bool:
    # TODO: the sample is noisy. A lot just inside the limit may really be outside -> SPEC_MISMATCH.
    #       ลองเว้น margin เล็กน้อยสำหรับล็อตที่ค่าใกล้ขอบสเปก
    return (sample["defect_pct"] <= spec["max_defect_pct"] and sample["moisture_pct"] <= spec["max_moisture_pct"]
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def total_rounds(state: dict, ctx: Context) -> int:
    # round.of is this game's count; rules.timeline can be a fixed default (the mock always says 8)
    return state["round"].get("of") or ctx.rules["timeline"]["rounds"]


def rounds_left(state: dict, ctx: Context) -> int:
    rnd, total = state["round"], total_rounds(state, ctx)
    return total if rnd["kind"] == "practice" else total - rnd["number"] + 1


# Market Calendar: how much of the remaining stock a round deserves, judged from its label only
# (the calendar gives labels such as ปกติ / เทศกาล, not demand numbers).
FESTIVAL_WEIGHT, LOW_WEIGHT = 1.5, 0.7
FESTIVAL_WORDS = ("เทศกาล", "festival", "peak", "high")
LOW_WORDS = ("ซบเซา", "ต่ำ", "ใกล้ปิด", "ท้าย", "low", "closing", "slow")


def label_weight(label) -> float:
    text = str(label or "").lower()
    if any(w in text for w in FESTIVAL_WORDS):
        return FESTIVAL_WEIGHT
    if any(w in text for w in LOW_WORDS):
        return LOW_WEIGHT
    return 1.0


def calendar_labels(calendar) -> dict[int, str]:
    """Round number -> label, read leniently because the calendar's exact shape is not documented.

    Accepts entries like {"round": "R3", "label": "เทศกาล"}, {"rounds": [3, 4], ...} or {"from": 3, "to": 5, ...}.
    """
    out = {}
    for e in calendar if isinstance(calendar, list) else []:
        if not isinstance(e, dict):
            continue
        label = e.get("label") or e.get("name") or e.get("calendar") or e.get("kind")
        nums = []
        for key in ("round", "rounds", "round_id", "round_ids"):
            v = e.get(key)
            for x in (v if isinstance(v, list) else [v]):
                nums += [int(n) for n in re.findall(r"\d+", str(x))] if x is not None else []
        lo, hi = e.get("from", e.get("start")), e.get("to", e.get("end"))
        if lo is not None and hi is not None:
            nums += list(range(int(re.findall(r"\d+", str(lo))[0]), int(re.findall(r"\d+", str(hi))[0]) + 1))
        if len(nums) == 2 and isinstance(e.get("round"), str) and "-" in e["round"]:
            nums = list(range(nums[0], nums[1] + 1))
        for n in nums:
            out[n] = label
    return out


def sell_budget(state: dict, ctx: Context) -> dict[str, int]:
    """How many kg of each lot to offer this round: spread sales over the rounds that are left,
    more in festival rounds and less in slow ones (Market Calendar)."""
    left = rounds_left(state, ctx)
    rnd = state["round"]
    labels = calendar_labels(ctx.calendar)
    if rnd.get("calendar") is not None and rnd["kind"] != "practice":
        labels[rnd["number"]] = rnd["calendar"]
    if rnd["kind"] == "practice":
        weight_now, weight_left = 1.0, float(left)
    else:
        upcoming = range(rnd["number"], rnd["number"] + left)
        weight_now = label_weight(labels.get(rnd["number"]))
        weight_left = sum(label_weight(labels.get(n)) for n in upcoming)
    out = {}
    for lot in state["lots"]:
        avail = lot["kg_available"] // BAG * BAG
        share = avail if left <= 1 else math.ceil(avail * weight_now / weight_left * PACE / BAG) * BAG
        out[lot["lot_id"]] = min(avail, max(BAG, share)) if avail >= BAG else 0
    return out


def calendar_ahead(state: dict, ctx: Context, n: int = 4) -> str:
    """Short text for the LLM: the labels of the next few rounds."""
    rnd = state["round"]
    if rnd["kind"] == "practice":
        return "practice round"
    labels = calendar_labels(ctx.calendar)
    ahead = [f"R{k} {labels[k]}" for k in range(rnd["number"] + 1, rnd["number"] + 1 + n) if k in labels]
    return ", ".join(ahead) or "unknown"


def candidates(state: dict, ctx: Context) -> list[dict]:
    budget = sell_budget(state, ctx)
    out = []
    for lot in state["lots"]:
        if budget[lot["lot_id"]] < BAG:
            continue
        for rfq in state["rfqs"]:
            if lot_fits(ctx, lot, rfq):
                out.append({"lot_id": lot["lot_id"], "rfq_id": rfq["rfq_id"], "buyer": rfq["buyer"],
                            "buyer_type": rfq["buyer_type"], "rfq_bags": rfq["bags"],
                            "max_bags": min(budget[lot["lot_id"]], rfq["kg"]) // BAG,
                            "tv": ctx.tv[lot["lot_id"]]})
    # Shuffle (weighted by RFQ size): if 40 teams all chase the first RFQ, they all get OUTBID.
    keyed = [(ctx.rng.random() ** (1 / c["rfq_bags"]), c) for c in out]
    return [c for _, c in sorted(keyed, key=lambda x: -x[0])]


# The loosest SPECIALTY spec seen so far (mock and docs): defect <= 3%, moisture <= 12.5%, screen >= 15.
SPECIALTY_SPEC = {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 15}


def passes_specialty(sample: dict) -> bool:
    return spec_ok(sample, SPECIALTY_SPEC)


# Last round: unsold beans fall to 0.80 x TV, so a lot's opportunity cost is just salvage (Researcher: Jiang 2023,
# bid-price control). INSTANT is the dump outlet; cap its markup there so the last bags sell.
# Practice-02: INSTANT-only lots offered at 0.92-0.95 in R7-R8 came back OUTBID, 8 of 16 bags went to salvage.
LAST_CAP_INST = float(os.environ.get("STRATEGY_LAST_CAP_INST", "0.86"))   # 0 = off
# Hold a lot for SPECIALTY only while it passes a SPECIALTY RFQ that is open now (real spec, e.g. screen >= 16-17),
# not our guessed SPECIALTY_SPEC: Practice-01 held LOT-5 (screen 15) for buyers it could never sell to.
HOLD_REAL_SPEC = os.environ.get("STRATEGY_HOLD_REAL_SPEC", "1") == "1"


def specialty_seen(state: dict, ctx) -> bool:
    """A SPECIALTY RFQ is open now or appeared in an earlier round's market_summary."""
    if any(r.get("buyer_type") == "SPECIALTY" for r in state.get("rfqs") or []):
        return True
    rounds = ctx.memory.rounds if ctx.memory is not None else []
    return any(row.get("buyer_type") == "SPECIALTY" for rnd in rounds for row in rnd.get("market_summary", []))


def rule_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict]:
    """No LLM: buyers that pay more get the best lots first, each at its learned markup.

    Within the same markup the shuffled candidate order is kept, so 40 teams do not all chase one RFQ.
    """
    budget = sell_budget(state, ctx)
    rfq_left = {r["rfq_id"]: r["bags"] for r in state["rfqs"]}
    plan = []
    order = {id(c): i for i, c in enumerate(cands)}

    def lot_key(c):
        if LOT_ORDER == "tvHigh" or c["buyer_type"] == "SPECIALTY":
            return -c["tv"]
        return c["tv"]                              # cheapest lot that passes this buyer's spec

    held = set()
    rnd = state["round"]
    # STRATEGY_RESERVE_WAIT_SPEC=1: hold only once a SPECIALTY buyer has shown up (now or in an earlier round).
    # Mock 18 Nov (fixed), 56 teams, 6 rounds, PR with/without waiting: no SPECIALTY 0.9594/0.9600,
    # + noisy samples 0.9542/0.9553, old mock 0.9578/0.9665 -> off by default.
    if LOT_ORDER == "reserve" and (specialty_seen(state, ctx) or not RESERVE_WAIT_SPEC) and rnd["kind"] != "practice" \
            and rnd["number"] <= total_rounds(state, ctx) / 2:
        if HOLD_REAL_SPEC:
            specs = [r["spec"] for r in state["rfqs"] if r["buyer_type"] == "SPECIALTY"]
            held = {l["lot_id"] for l in state["lots"] if any(spec_ok(l["sample"], sp) for sp in specs)}
        else:
            held = {l["lot_id"] for l in state["lots"] if passes_specialty(l["sample"])}
    for c in sorted(cands, key=lambda c: (-round(markup_for(ctx, c["buyer"], c["buyer_type"]), 2), lot_key(c), order[id(c)])):
        if c["lot_id"] in held and c["buyer_type"] != "SPECIALTY":
            continue
        bags = min(budget[c["lot_id"]] // BAG, rfq_left[c["rfq_id"]])
        if bags > 0:
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": bags,
                         "markup": markup_for(ctx, c["buyer"], c["buyer_type"])})
            budget[c["lot_id"]] -= bags * BAG
            rfq_left[c["rfq_id"]] -= bags
    return plan


SYSTEM = """You are the sales agent of a coffee farm selling green robusta beans in a sealed-offer market.
Each round buyers post RFQs. You choose which RFQs to offer your lots to, how many 60 kg bags, and a markup.
price = markup x the lot's True Value estimate (tv). Buyers rank offers by price / true value, cheapest first,
and reject offers above their hidden ceiling. Unsold beans are worth only 0.80 x tv at the end.
Buyer types pay differently: SPECIALTY the most, then COMMERCIAL, INSTANT the least. Ceilings are hidden;
learn them from the results (ABOVE_LIMIT means the price was too high for that buyer).
INSTANT buyers pay under true value: keep every INSTANT markup between 0.81 and 0.99, never 1.00 or more.
Never offer a buyer a markup at or above one it already sent back ABOVE_LIMIT.
About 56 teams compete; raising a markup that just sold by even 1-2% often loses the whole sale.
Answer only JSON matching the schema."""

SCHEMA = {"type": "object", "required": ["offers", "reason"], "properties": {
    "offers": {"type": "array", "items": {"type": "object", "required": ["candidate", "bags", "markup"], "properties": {
        "candidate": {"type": "integer"}, "bags": {"type": "integer"}, "markup": {"type": "number"}}}},
    "reason": {"type": "string", "maxLength": 300}}}   # short: a long reason can run past max_tokens and cut the JSON


CALENDAR_EN = {"ปกติ": "normal", "เทศกาล": "festival", "ใกล้ปิดตลาด": "near close"}


def ascii_prompt(text: str, state: dict, memory) -> str:
    """Thai costs several times more tokens than English (gateway context is 8,192 tokens). Swap Thai buyer names
    for short ids (buyer_id, else B + the name's leading number) and Thai calendar labels for English.
    The LLM answers with candidate numbers, so nothing has to be mapped back."""
    names = {}
    rows = list(state.get("rfqs") or [])
    for rnd in (getattr(memory, "rounds", None) or []):
        rows += rnd.get("market_summary") or []
    for r in rows:
        name = r.get("buyer")
        if name and not name.isascii() and name not in names:
            alias = r.get("buyer_id") or "B" + (name.split()[0] if name.split()[0].isdigit() else str(len(names) + 1))
            names[name] = str(alias)
    for name in sorted(names, key=len, reverse=True):      # longer first so one name never eats part of another
        text = text.replace(name, names[name])
    for th, en in CALENDAR_EN.items():
        text = text.replace(th, en)
    return text


def llm_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict] | None:
    rnd = state["round"]
    target = sum(sell_budget(state, ctx).values()) // BAG
    if state["capacity_left_kg"] is not None:
        target = min(target, state["capacity_left_kg"] // BAG)
    time_for_llm = ctx.seconds_left - 5          # keep 5 s to send the offers before the round closes
    if time_for_llm < 3:
        ctx.notes.append("no time for the LLM this round")
        return None
    table = [{"candidate": i, **{k: c[k] for k in ("lot_id", "buyer", "buyer_type", "rfq_bags", "max_bags", "tv")}}
             for i, c in enumerate(cands)]
    user = "\n".join([
        f"Round {rnd['round_id']} ({rnd['kind']}), market calendar: {rnd['calendar']}. Rounds left: {rounds_left(state, ctx)}.",
        f"Calendar ahead: {calendar_ahead(state, ctx)}",
        f"You may offer at most {state['capacity_left_kg'] // BAG if state['capacity_left_kg'] is not None else 'any'} bags in total this round.",
        f"Plan to offer about {target} bags in total this round: unsold beans lose 20% of their value at the end.",
        f"Suggested markups (learned from results, per buyer and type): {json.dumps({k: round(v, 3) for k, v in ctx.markups.items()}, ensure_ascii=False)}",
        f"Rival inventory radar ({TOTAL_TEAMS} teams): {json.dumps(rival_tracker(state, ctx.memory), ensure_ascii=False)}",
        "Recent results:", ctx.memory.summary() if ctx.memory else "none",
        "Candidates (lot x RFQ, already spec-checked):", json.dumps(table, ensure_ascii=False),
        "Pick offers. Use each candidate at most once; bags <= max_bags.",
    ])
    user = ascii_prompt(user, state, ctx.memory)
    # Gateway context is 8,192 tokens per call (slide day 18 p.13): system + user + max_tokens 1,500 must fit.
    # ~3 chars per token for this JSON-heavy English is a safe guess; drop the last candidates until it fits.
    budget_chars = (8192 - ctx.llm.max_tokens - 400) * 3
    while len(SYSTEM) + len(user) > budget_chars and len(table) > 1:
        table = table[:max(1, len(table) * 3 // 4)]
        head = user[:user.index("Candidates (lot x RFQ")]
        user = head + "Candidates (lot x RFQ, already spec-checked):\n" + ascii_prompt(json.dumps(table, ensure_ascii=False), state, ctx.memory) \
            + "\nPick offers. Use each candidate at most once; bags <= max_bags."
        ctx.notes.append(f"prompt trimmed to {len(table)} candidates to fit 8k tokens")
    # TODO: give the LLM more to reason about — buyer personas' habits, the calendar ahead, price trends
    answer = ctx.llm.ask_json(SYSTEM, user, SCHEMA, timeout=time_for_llm)
    if answer is None or not isinstance(answer.get("offers"), list):
        ctx.notes.append(f"llm failed: {ctx.llm.last_error}")
        return None
    plan = []
    for o in answer["offers"]:
        try:
            c = cands[int(o["candidate"])]
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": int(o["bags"]), "markup": float(o["markup"])})
        except (KeyError, IndexError, TypeError, ValueError):
            ctx.notes.append(f"llm bad offer ignored: {o}")
    ctx.notes.append(f"llm reason: {str(answer.get('reason'))[:200]}")
    return plan


def guard(state: dict, ctx: Context, plan: list[dict]) -> list[dict]:
    """Turn a plan into offers the market will accept. Never trust the LLM's numbers as they are."""
    lots = {l["lot_id"]: l for l in state["lots"]}
    rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
    room = state["capacity_left_kg"] if state["capacity_left_kg"] is not None else 10**9
    lot_left = {i: l["kg_available"] for i, l in lots.items()}
    rfq_left = {i: r["kg"] for i, r in rfqs.items()}
    ceilings = known_ceilings(ctx.memory)
    offers, seen = [], set()
    for p in plan:
        lot, rfq = lots.get(p["lot_id"]), rfqs.get(p["rfq_id"])
        if lot is None or rfq is None or (p["lot_id"], p["rfq_id"]) in seen or not lot_fits(ctx, lot, rfq):
            ctx.notes.append(f"guard dropped {p}")
            continue
        kg = min(p["bags"] * BAG, lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room) // BAG * BAG
        if kg < BAG:
            continue
        btype = rfq.get("buyer_type", "")
        # cap: the type's cap and the buyer's known ceiling, but never under the floor (selling under salvage loses more)
        cap = min(type_cap(btype), ceilings.get(rfq.get("buyer"), 9.0))
        floor = max(type_floor(ctx, btype), MIN_MARKUP)
        # cap sits CEILING_MARGIN under the markup the buyer refused; only a floor at or above that refused markup
        # is sure to fail again (between the two, the floor is still worth a try)
        if floor >= cap + CEILING_MARGIN - 1e-9:
            ctx.notes.append(f"guard dropped {p}: {rfq.get('buyer')} ceiling {cap:.3f} under floor {floor:.3f}")
            continue
        markup = max(min(p["markup"], cap), floor)
        offers.append({"rfq_id": rfq["rfq_id"], "lot_id": lot["lot_id"], "kg": kg,
                       "price_per_kg": round(markup * ctx.tv[lot["lot_id"]], 2)})
        seen.add((p["lot_id"], p["rfq_id"]))
        lot_left[lot["lot_id"]] -= kg
        rfq_left[rfq["rfq_id"]] -= kg
        room -= kg
    return offers


def decide(state: dict, ctx: Context) -> tuple[list[dict], str]:
    """Returns (offers, source) where source is "llm" or "rules"."""
    ctx.markups = learned_markups(ctx.memory)
    ctx.floor = markup_floor(state, ctx)
    ctx.cap = {}
    if LAST_CAP_INST and state["round"]["kind"] != "practice" and rounds_left(state, ctx) <= 1:
        ctx.cap["INSTANT"] = LAST_CAP_INST
    cands = candidates(state, ctx)
    if not cands:
        return [], "none"
    plan = llm_plan(state, ctx, cands) if ctx.llm else None
    if plan:
        # The LLM tends to offer too little. Rules fill whatever volume it left unused
        # (guard skips lot×RFQ pairs the LLM already used and stops at the round capacity).
        offers = guard(state, ctx, plan + rule_plan(state, ctx, cands))
        n_llm = len(guard(state, Context(ctx.rules, ctx.calendar, ctx.tv, ctx.rng), plan))
        return offers, "llm" if len(offers) == n_llm else f"llm+rules ({n_llm}+{len(offers) - n_llm})"
    return guard(state, ctx, rule_plan(state, ctx, cands)), "rules"
