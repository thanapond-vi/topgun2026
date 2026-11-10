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
MARKUP = {"SPECIALTY": 1.15, "COMMERCIAL": 1.05, "INSTANT": 1.00}   # starting point per type, then learned per buyer
MIN_MARKUP, MAX_MARKUP = 0.85, 1.50   # unsold beans go to salvage at 0.80 × TV, so never go near that
PACE = 1.3                            # offer 1.3 × an even share of each lot per remaining round

# How the markup for one buyer moves after a round (see learned_markups()).
STEP_UP = 0.03          # ACCEPTED: everything sold, ask a bit more next time
STEP_OUTBID = 0.03      # OUTBID: rivals were cheaper
CEILING_MARGIN = 0.02   # stay this far under the lowest markup that came back ABOVE_LIMIT
# market_summary: an RFQ that ended unfilled (bought_kg < wanted_kg) had no rival pressure, so an ACCEPTED offer
# there can rise faster. Set STRATEGY_FILL_BONUS=0 to switch this off.
FILL_BONUS = float(os.environ.get("STRATEGY_FILL_BONUS", "0"))   # off: untested, the mock RFQs always fill


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
    if memory is None:
        return out
    for rnd in memory.rounds:
        buyer_of = {row["rfq_id"]: (row["buyer"], row["buyer_type"]) for row in rnd.get("market_summary", [])}
        unfilled = {row["rfq_id"] for row in rnd.get("market_summary", [])
                    if row.get("bought_kg") is not None and row.get("wanted_kg") and row["bought_kg"] < row["wanted_kg"]}
        sent = {(o["rfq_id"], o["lot_id"]): o for o in memory.sent.get(rnd["round_id"], [])}
        moves: dict[str, list[float]] = {}
        for o in rnd.get("my_offers", []):
            s = sent.get((o["rfq_id"], o["lot_id"]))
            if s is None or o["rfq_id"] not in buyer_of:
                continue
            buyer, btype = buyer_of[o["rfq_id"]]
            m = s["markup"]
            if o["status"] == "ACCEPTED":
                new = m + STEP_UP + (FILL_BONUS if o["rfq_id"] in unfilled else 0.0)
            elif o["status"] == "PARTIAL":
                new = m
            elif o["status"] == "OUTBID":
                new = m - STEP_OUTBID
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
    return {k: min(max(v, MIN_MARKUP), MAX_MARKUP) for k, v in out.items()}


def markup_for(ctx, buyer: str, buyer_type: str) -> float:
    m = ctx.markups if ctx.markups else MARKUP
    return m.get(buyer, m.get(buyer_type, MARKUP.get(buyer_type, 1.0)))


def spec_ok(sample: dict, spec: dict) -> bool:
    # TODO: the sample is noisy. A lot just inside the limit may really be outside -> SPEC_MISMATCH.
    #       ลองเว้น margin เล็กน้อยสำหรับล็อตที่ค่าใกล้ขอบสเปก
    return (sample["defect_pct"] <= spec["max_defect_pct"] and sample["moisture_pct"] <= spec["max_moisture_pct"]
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def rounds_left(state: dict, ctx: Context) -> int:
    rnd, total = state["round"], ctx.rules["timeline"]["rounds"]
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
            if spec_ok(lot["sample"], rfq["spec"]):
                out.append({"lot_id": lot["lot_id"], "rfq_id": rfq["rfq_id"], "buyer": rfq["buyer"],
                            "buyer_type": rfq["buyer_type"], "rfq_bags": rfq["bags"],
                            "max_bags": min(budget[lot["lot_id"]], rfq["kg"]) // BAG,
                            "tv": ctx.tv[lot["lot_id"]]})
    # Shuffle (weighted by RFQ size): if 40 teams all chase the first RFQ, they all get OUTBID.
    keyed = [(ctx.rng.random() ** (1 / c["rfq_bags"]), c) for c in out]
    return [c for _, c in sorted(keyed, key=lambda x: -x[0])]


def rule_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict]:
    """No LLM: buyers that pay more get the best lots first, each at its learned markup.

    Within the same markup the shuffled candidate order is kept, so 40 teams do not all chase one RFQ.
    """
    budget = sell_budget(state, ctx)
    rfq_left = {r["rfq_id"]: r["bags"] for r in state["rfqs"]}
    plan = []
    order = {id(c): i for i, c in enumerate(cands)}
    for c in sorted(cands, key=lambda c: (-round(markup_for(ctx, c["buyer"], c["buyer_type"]), 2), -c["tv"], order[id(c)])):
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
Answer only JSON matching the schema."""

SCHEMA = {"type": "object", "required": ["offers", "reason"], "properties": {
    "offers": {"type": "array", "items": {"type": "object", "required": ["candidate", "bags", "markup"], "properties": {
        "candidate": {"type": "integer"}, "bags": {"type": "integer"}, "markup": {"type": "number"}}}},
    "reason": {"type": "string", "maxLength": 300}}}   # short: a long reason can run past max_tokens and cut the JSON


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
        "Recent results:", ctx.memory.summary() if ctx.memory else "none",
        "Candidates (lot x RFQ, already spec-checked):", json.dumps(table, ensure_ascii=False),
        "Pick offers. Use each candidate at most once; bags <= max_bags.",
    ])
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
    offers, seen = [], set()
    for p in plan:
        lot, rfq = lots.get(p["lot_id"]), rfqs.get(p["rfq_id"])
        if lot is None or rfq is None or (p["lot_id"], p["rfq_id"]) in seen or not spec_ok(lot["sample"], rfq["spec"]):
            ctx.notes.append(f"guard dropped {p}")
            continue
        kg = min(p["bags"] * BAG, lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room) // BAG * BAG
        if kg < BAG:
            continue
        markup = min(max(p["markup"], MIN_MARKUP), MAX_MARKUP)
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
