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
from pathlib import Path

# Race-day settings without editing code: STRATEGY_*=value lines in strategy.env (no keys in it; it goes into the
# hand-in zip so the judges see every change). Read here, before the constants below, because main.py imports this
# module before it loads .env (so STRATEGY_* lines in .env do nothing). A variable set in the shell still wins.
_STRATEGY_ENV = Path(__file__).with_name("strategy.env")
_SHELL_VARS = {k for k in os.environ if k.startswith("STRATEGY_")}   # set in the shell before start: these win
if _STRATEGY_ENV.exists():
    for _line in _STRATEGY_ENV.read_text(encoding="utf-8").splitlines():
        if "=" in _line and _line.strip().startswith("STRATEGY_"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.split("#", 1)[0].strip())

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
FLOOR_END_MARGIN = float(os.environ.get("STRATEGY_FLOOR_END_MARGIN", "0.03"))   # 19 Nov HPC 1000 games: 0.03 + no last INSTANT cap, PR +0.001-0.002, price/TV better  # never below salvage: selling under it earns less than not selling
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
STEP_UP = float(os.environ.get("STRATEGY_STEP_UP", "0.02"))
# Practice 19 Nov: COMMERCIAL raised +0.02 after a sale and lost the next round in all 3 games. STEP_UP_COM caps the
# raise for COMMERCIAL only. On from 19 Nov (user): HPC 6 markets x 2,000 games, 0.01 better in 4, equal 1, -0.13 rank in 1.
STEP_UP_COM = float(os.environ.get("STRATEGY_STEP_UP_COM", "0.01"))   # ACCEPTED: everything sold, ask a bit more next time
STEP_OUTBID = 0.03      # OUTBID: rivals were cheaper (used when the RFQ's closing price is unknown)
OUTBID_UNDERCUT = 0.01  # OUTBID with a known closing price: go this far under the highest price that sold
# Cap on how far one buyer's markup may fall in one round after OUTBID with a known closing price.
# Was 0.05 because price_max might come from a rival's lot with a different TV. Stage 2 (18 Nov) and stage 3
# (slide day 20) give every team the same lots, so price_max / our TV is the rivals' winning markup: 0.05 steps
# took SPECIALTY 1.15 -> 1.10 -> 1.05 -> 1.00 while rivals sold at ~0.97 -> 0.95 (13-competition-day18.md).
MAX_DROP = float(os.environ.get("STRATEGY_MAX_DROP", "0.30"))
# A markup this buyer (or its type) has never sold at is a guess: send at most this many bags at it, so one
# wrong guess cannot lose the whole round's capacity (R1 18 Nov: 5 bags to SPECIALTY @1.15, all OUTBID). 0 = off.
UNTESTED_BAGS = int(os.environ.get("STRATEGY_UNTESTED_BAGS", "2"))
# Markups already proven on earlier days (17-18 Nov sold 0.97-0.99 COMMERCIAL, 0.93-0.95 INSTANT): not a guess.
PROVEN_BASE = {"COMMERCIAL": 0.98, "INSTANT": 0.94, "SPECIALTY": 0.0}
# Lots only INSTANT can take: sell them in the first INSTANT_EARLY_FRAC of the rounds, when INSTANT pays most
# (18 Nov: best R2-R5, then down to 0.89 in R7-R8), before the festival rounds need capacity for SPECIALTY. 0 = off.
INSTANT_EARLY_FRAC = float(os.environ.get("STRATEGY_INSTANT_EARLY_FRAC", "0.4"))
# Market anchor (See-It TGR-23, NU XEUS TGR-16, prompt 15): every team has the same lots, so for an RFQ that only ONE
# of our lots can supply, price_max of the sold offers / that lot's TV = the highest markup that still sold there.
# Offer ANCHOR_UNDER below it next time (pay-as-bid: just under the cut-off is the best price that still sells).
ANCHOR = os.environ.get("STRATEGY_ANCHOR", "1") == "1"
ANCHOR_UNDER = float(os.environ.get("STRATEGY_ANCHOR_UNDER", "0.01"))
ANCHOR_AGE = int(os.environ.get("STRATEGY_ANCHOR_AGE", "2"))     # use anchors from the last N rounds only
# NU XEUS: keep a SPECIALTY-grade lot for SPECIALTY only while SPECIALTY pays at least what COMMERCIAL pays
# See-It / NU XEUS / prompt 15: practice rounds P1-P2 do not count and do not use stock, so use them to probe
# prices: 1 bag on every RFQ a lot fits, the markup stepped by PROBE_LADDER around our current price for that buyer.
# Their results feed learned_markups() and market_anchors() before R1. No LLM call in practice when probing.
PRACTICE_PROBE = os.environ.get("STRATEGY_PRACTICE_PROBE", "0") == "1"   # arena20 200 games: no gain; anchors already come from every RFQ in the practice market_summary
PROBE_LADDER = [float(x) for x in os.environ.get("STRATEGY_PROBE_LADDER", "-0.03,0,0.03").split(",")]
# NU XEUS: a buyer that drew many offers per bag last round (18 Nov: ดอยช้าง 21-39 offers for 9-14 bags) is likely
# to drop our offer. Send it at most CROWD_BAGS when the same lot has a less crowded buyer to go to. 0 = off.
CROWD_MAX = float(os.environ.get("STRATEGY_CROWD_MAX", "0"))   # arena20 200 games: within noise, off (3.0 to try)
CROWD_BAGS = int(os.environ.get("STRATEGY_CROWD_BAGS", "1"))
CLOSE_MAX_TV = os.environ.get("STRATEGY_CLOSE_MAX_TV", "0") == "1"   # arena20: mixed (8R worse, 15R better), off
PRESSURE = os.environ.get("STRATEGY_PRESSURE", "1") == "1"
PRESSURE_DROP = float(os.environ.get("STRATEGY_PRESSURE_DROP", "0.08"))   # floor drop when 50% behind schedule
# Stage 3: festival R6-R8 (SPECIALTY wants a lot). Holding SPECIALTY-grade lots stopped at round N/2 (R7 of 15),
# one round before the festival ends: hold up to the last festival round when it is later. 0 = old rule.
HOLD_TO_FESTIVAL_END = os.environ.get("STRATEGY_HOLD_TO_FESTIVAL_END", "1") == "1"   # arena20 200 games: no change, off
# guard: when the same (lot, RFQ) comes again later in the plan, top the first offer up instead of dropping it.
# arena20 200 games: slightly worse on all 3 sets (more INSTANT bags early at the rush price) -> off
MERGE_PAIRS = os.environ.get("STRATEGY_MERGE_PAIRS", "0") == "1"
HOLD_IF_BETTER = os.environ.get("STRATEGY_HOLD_IF_BETTER", "1") == "1"
RUSH_ROUND_DOWN = os.environ.get("STRATEGY_RUSH_ROUND_DOWN", "1") == "1"   # INSTANT rush share of capacity: round down
LAST_NO_STEP = os.environ.get("STRATEGY_LAST_NO_STEP", "0") == "1"         # last round: no STEP_UP after ACCEPTED (mock: no effect, off)
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
# arena20 (19 Nov, mock/diag_winner.py): game winners sent 1-2 fewer bags to INSTANT and more to COMMERCIAL than us.
# A lot that a COMMERCIAL/SPECIALTY RFQ takes this round is not offered to INSTANT until the last KEEP_COM_LAST
# rounds. -1 = off.
KEEP_COM_LAST = int(os.environ.get("STRATEGY_KEEP_COM_LAST", "-1"))
# How several results for one buyer in a round combine: "min" (old: one losing RFQ drags the price down)
# or "median" (prompt 15 item 5, NU XEUS learns per RFQ).
LEARN_AGG = os.environ.get("STRATEGY_LEARN_AGG", "min")   # "sold": a buyer that bought from us this round learns only from the sold offers
# Reviewer 19 Nov: spread bags over RFQs instead of filling one RFQ. Most bags per (lot, RFQ) in the first half /
# second half of the game. 0 = off.
MAX_PER_RFQ = int(os.environ.get("STRATEGY_MAX_PER_RFQ", "0"))
MAX_PER_RFQ_LATE = int(os.environ.get("STRATEGY_MAX_PER_RFQ_LATE", "0"))
# Reviewer 19 Nov: in the last CLEAR_LAST rounds offer everything left (capacity still caps it). 0 = off.
CLEAR_LAST = int(os.environ.get("STRATEGY_CLEAR_LAST", "0"))
# 20-arena test 19 Nov (all off by default):
RFQ_SMALL = os.environ.get("STRATEGY_RFQ_SMALL", "0") == "1"          # same markup: smaller RFQ first (fewer rivals)
EV_CROWD = float(os.environ.get("STRATEGY_EV_CROWD", "0"))            # rank RFQs by markup - k*ln(offers per bag)
UNDER_CROWD = float(os.environ.get("STRATEGY_UNDER_CROWD", "0"))      # anchor undercut + k*(offers per bag - 1)
PRECLOSE_BOOST = float(os.environ.get("STRATEGY_PRECLOSE_BOOST", "0"))  # x budget in the 4 rounds before near close
SPEC_MARGIN = float(os.environ.get("STRATEGY_SPEC_MARGIN", "0"))      # keep this far inside defect limits (moisture /5)
# Falling market (19 Nov test): in the first EARLY_ROUNDS real rounds undercut the market price EARLY_UNDER more, and
# with EARLY_FULL drop the 2-bag limit on prices we have not sold at yet. Both off by default.
EARLY_ROUNDS = int(os.environ.get("STRATEGY_EARLY_ROUNDS", "3"))
EARLY_UNDER = float(os.environ.get("STRATEGY_EARLY_UNDER", "0"))
EARLY_FULL = os.environ.get("STRATEGY_EARLY_FULL", "0") == "1"
FILL_CAP = os.environ.get("STRATEGY_FILL_CAP", "1") == "1"            # use spare capacity with stock beyond the budget
# FILL_CAP on from 19 Nov (user): 40 arenas x 6,000 games + 5 markets x 600 games, better or equal in every market
# Oversupplied market (19 Nov, user's view: buyers want less than the teams bring, most teams take a thin margin):
# when INSTANT bought >= SHORT_FILL of what it wanted last round (offers to spare), let the floor fall to SHORT_FLOOR
# so we still sell at the smallest loss instead of holding at FLOOR_MID and ending in salvage. 0 = off.
SHORT_FLOOR = float(os.environ.get("STRATEGY_SHORT_FLOOR", "0"))
SHORT_FILL = float(os.environ.get("STRATEGY_SHORT_FILL", "0.90"))   # 19 Nov balance test
# HPC 19 Nov (night-1119-0047, 6 rounds x 200 games): in an oversupplied price war STEP_UP 0.005, FLOOR_MID 0.94,
# PACE 1.1 and SHORT_FLOOR 0.90 went from rank ~11.7 to ~5.8 of 56, but lost in the market calibrated to day 18
# (7.8 -> 11.2). WAR_MODE uses those values only in rounds where market_short() says the market is oversupplied.
WAR_MODE = os.environ.get("STRATEGY_WAR_MODE", "1") == "1"   # on from 19 Nov (user chose the balanced setting)
WAR_STEP_UP = float(os.environ.get("STRATEGY_WAR_STEP_UP", "0.01"))   # 0.005: harder in a price war, worse in a day-18 market
WAR_FLOOR_MID = float(os.environ.get("STRATEGY_WAR_FLOOR_MID", "0.94"))
WAR_PACE = float(os.environ.get("STRATEGY_WAR_PACE", "1.1"))
WAR_SHORT_FLOOR = float(os.environ.get("STRATEGY_WAR_SHORT_FLOOR", "0.90"))


# Race day (19 Nov, LLM off): strategy.env is read again at the start of every round, so a teammate with an AI
# coding assistant can switch a tested mode or nudge a buyer type's markup without restarting the agent.
# Only these settings reload; anything else in the file needs a restart. Every reload is written to the round's notes.
OFFSET_LIMIT = 0.01                       # STRATEGY_OFFSET_* can move a buyer type's markup by at most this much
# arena 19 Nov (300 games x 5 markets): one 0.01 notch moves our rank 2-5 places. +0.01 hurt everywhere; -0.01 COM/INST
# helped only in oversupplied markets and hurt in the day-18 market -> limit 0.01, and the playbook only allows -0.01.
TYPE_OFFSET = {"SPECIALTY": 0.0, "COMMERCIAL": 0.0, "INSTANT": 0.0}
_OFFSET_VARS = {"SPECIALTY": "STRATEGY_OFFSET_SPEC", "COMMERCIAL": "STRATEGY_OFFSET_COM", "INSTANT": "STRATEGY_OFFSET_INST"}
_HOT = {  # module setting -> (variable, parser, code default)   keep the defaults equal to the ones above
    "MAX_PER_RFQ": ("STRATEGY_MAX_PER_RFQ", int, "0"),
    "MAX_PER_RFQ_LATE": ("STRATEGY_MAX_PER_RFQ_LATE", int, "0"),
    "WAR_MODE": ("STRATEGY_WAR_MODE", lambda v: v == "1", "1"),
    "FLOOR_END_MARGIN": ("STRATEGY_FLOOR_END_MARGIN", float, "0.03"),
    "LAST_CAP_INST": ("STRATEGY_LAST_CAP_INST", float, "0"),
    "PACE": ("STRATEGY_PACE", float, "1.3"),
    "FILL_CAP": ("STRATEGY_FILL_CAP", lambda v: v == "1", "1"),
    "STEP_UP_COM": ("STRATEGY_STEP_UP_COM", float, "0.01"),
}
_hot_stamp: list = [None]


def _read_strategy_env() -> dict:
    vals = {}
    if _STRATEGY_ENV.exists():
        for line in _STRATEGY_ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and line.strip().startswith("STRATEGY_"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.split("#", 1)[0].strip()
    return vals


_suggest: dict = {}        # setting -> value a teammate/AI wrote in strategy.env (not yet judged)
_base: dict = {}           # setting -> value without strategy.env (code default or shell)
_last_verdict: dict = {}   # setting -> (value, accepted) last round, to log only changes


def _recent(memory) -> dict:
    """Evidence from the last real rounds that the rules use to accept or reject a suggestion."""
    real = [r for r in (memory.rounds if memory is not None else []) if r.get("kind") != "practice"]
    ev = {"rounds": len(real), "fill2": None, "lost": {}, "sold_all": {}, "unfilled": {}}
    offered = sold = 0
    for rnd in real[-2:]:
        for o in rnd.get("my_offers") or []:
            if o.get("status") in ("SPEC_MISMATCH", "CANCELLED"):
                continue
            offered += o.get("kg") or 0
            sold += o.get("filled_kg") or 0
    if offered:
        ev["fill2"] = sold / offered
    if real:
        last = real[-1]
        btype = {r["rfq_id"]: r.get("buyer_type") for r in last.get("market_summary") or [] if r.get("rfq_id")}
        for t in ("SPECIALTY", "COMMERCIAL", "INSTANT"):
            mine = [o for o in last.get("my_offers") or [] if btype.get(o.get("rfq_id")) == t]
            kg = sum(o.get("kg") or 0 for o in mine)
            if kg:
                ev["lost"][t] = sum(o.get("kg") or 0 for o in mine if o.get("status") == "OUTBID") / kg
                ev["sold_all"][t] = sum(o.get("filled_kg") or 0 for o in mine) >= kg
            rows = [r for r in last.get("market_summary") or [] if r.get("buyer_type") == t and r.get("wanted_kg")]
            if rows:
                ev["unfilled"][t] = sum(r.get("bought_kg") or 0 for r in rows) < sum(r["wanted_kg"] for r in rows)
    return ev


def _judge(name: str, value, memory) -> tuple[bool, str]:
    """The rules decide whether a suggested setting fits what the market shows now. Returns (accept, reason)."""
    if value == _base.get(name):
        return True, "back to default"
    ev = _recent(memory)
    short = market_short(memory)
    if name in ("MAX_PER_RFQ", "MAX_PER_RFQ_LATE"):
        if value and (short or (ev["fill2"] is not None and ev["fill2"] < 0.6)):
            return True, "market oversupplied or we sold < 60% in 2 rounds"
        return False, "market not oversupplied and we sell >= 60%"
    if name == "WAR_MODE":
        if not value and ev["rounds"] >= 2 and not short:
            return True, "Instant not oversupplied in the last 2 rounds"
        return bool(value), "Instant still oversupplied" if not value else "on"
    if name == "FILL_CAP":
        if not value and ev["fill2"] is not None and ev["fill2"] < 0.6:
            return True, "we sold < 60% in 2 rounds"
        return bool(value), "we still sell >= 60%" if not value else "on"
    if name == "FLOOR_END_MARGIN":
        return (0.01 <= value <= 0.05), "within 0.01-0.05" if 0.01 <= value <= 0.05 else "outside 0.01-0.05"
    if name == "LAST_CAP_INST":
        ok = value == 0 or 0.84 <= value <= 0.92
        return ok, "0 or 0.84-0.92" if ok else "outside 0.84-0.92"
    if name == "STEP_UP_COM":
        return (0.0 <= value <= 0.02), "within 0-0.02" if 0.0 <= value <= 0.02 else "outside 0-0.02"
    if name == "PACE":
        return (1.0 <= value <= 1.8), "within 1.0-1.8" if 1.0 <= value <= 1.8 else "outside 1.0-1.8"
    if name.startswith("OFFSET_"):
        t = {"SPEC": "SPECIALTY", "COMM": "COMMERCIAL", "INST": "INSTANT"}[name[7:]]
        if value < 0:
            if short and ev["lost"].get(t, 0) >= 0.5:
                return True, f"oversupplied and we lost >= 50% of {t} last round"
            return False, f"not oversupplied or we did not lose most {t} offers"
        if ev["sold_all"].get(t) and ev["unfilled"].get(t):
            return True, f"sold all {t} and buyers still wanted more"
        return False, f"raise only after selling all {t} with buyers unfilled"
    return True, ""


def reload_settings(ctx=None) -> dict:
    """Read strategy.env again if it changed, then let the rules accept or reject every suggestion in it for this
    round (evidence from the last real rounds). Settings set in the shell are applied as they are (simulations).
    Returns the active hot settings."""
    try:
        stamp = _STRATEGY_ENV.stat().st_mtime if _STRATEGY_ENV.exists() else 0
    except OSError:
        stamp = _hot_stamp[0]
    if stamp != _hot_stamp[0]:
        try:
            vals = _read_strategy_env()
            base = {name: parse(os.environ[var] if var in _SHELL_VARS else default) for name, (var, parse, default) in _HOT.items()}
            base.update({f"OFFSET_{t[:4]}": max(-OFFSET_LIMIT, min(OFFSET_LIMIT, float(os.environ.get(v, "0") if v in _SHELL_VARS else 0.0)))
                         for t, v in _OFFSET_VARS.items()})
            sug = {name: parse(vals[var]) for name, (var, parse, _d) in _HOT.items() if var in vals and var not in _SHELL_VARS}
            sug.update({f"OFFSET_{t[:4]}": max(-OFFSET_LIMIT, min(OFFSET_LIMIT, float(vals[v])))
                        for t, v in _OFFSET_VARS.items() if v in vals and v not in _SHELL_VARS})
        except (OSError, ValueError) as e:
            if ctx is not None:
                ctx.notes.append(f"guard: strategy.env unreadable ({e}); kept the previous settings")
            return {**{n: globals()[n] for n in _HOT}, **{f"OFFSET_{t[:4]}": v for t, v in TYPE_OFFSET.items()}}
        _hot_stamp[0] = stamp
        _base.clear(); _base.update(base)
        _suggest.clear(); _suggest.update(sug)
        if ctx is not None and sug:
            ctx.notes.append("guard: strategy.env suggests " + ", ".join(f"{k}={v}" for k, v in sug.items()))
    memory = getattr(ctx, "memory", None) if ctx is not None else None
    active = dict(_base)
    for name, value in _suggest.items():
        ok, why = _judge(name, value, memory)
        if ok:
            active[name] = value
        if ctx is not None and _last_verdict.get(name) != (value, ok):
            ctx.notes.append(f"guard: suggestion {name}={value} {'ACCEPTED' if ok else 'rejected'} ({why})")
        _last_verdict[name] = (value, ok)
    for name in list(_last_verdict):
        if name not in _suggest:
            del _last_verdict[name]
    globals().update({n: active[n] for n in _HOT if n in active})
    TYPE_OFFSET.update({t: active.get(f"OFFSET_{t[:4]}", 0.0) for t in TYPE_OFFSET})
    return {**{n: globals()[n] for n in _HOT}, **{f"OFFSET_{t[:4]}": v for t, v in TYPE_OFFSET.items()}}


def market_short(memory, last: int = 2) -> bool:
    """INSTANT RFQs were (almost) all filled in the last real rounds: more beans on offer than buyers want."""
    rounds = [r for r in (memory.rounds if memory is not None else []) if r.get("kind") != "practice"][-last:]
    wanted = bought = 0
    for rnd in rounds:
        for row in rnd.get("market_summary") or []:
            if row.get("buyer_type") == "INSTANT" and row.get("wanted_kg"):
                wanted += row["wanted_kg"]
                bought += row.get("bought_kg") or 0
    return wanted > 0 and bought / wanted >= SHORT_FILL


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


def learned_markups(memory, step_up: float | None = None, tv_all: dict | None = None) -> dict[str, float]:
    """Markup per buyer (by name, plus a fallback per buyer type) learned from every round so far.

    Rebuilt from memory each round, so a restarted agent learns the same thing again.
      ACCEPTED     -> the buyer's hidden ceiling is at least this markup and rivals were not cheaper: ask more
      PARTIAL      -> right at the cut-off of that RFQ: keep this markup
      OUTBID       -> under the ceiling but rivals were cheaper: ask less
      ABOVE_LIMIT  -> the ceiling is below this markup: remember it and stay under it from now on
    """
    out = dict(MARKUP)
    tv_all = tv_all or {}
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
        sold_moves: dict[str, list[float]] = {}
        for o in rnd.get("my_offers", []):
            s = sent.get((o["rfq_id"], o["lot_id"]))
            if s is None or o["rfq_id"] not in buyer_of:
                continue
            buyer, btype = buyer_of[o["rfq_id"]]
            type_of[buyer] = btype
            m = s["markup"]
            if o["status"] == "ACCEPTED":
                bonus = FILL_BONUS if o["rfq_id"] in unfilled and btype != "INSTANT" else 0.0
                step = STEP_UP if step_up is None else step_up
                if btype == "COMMERCIAL":
                    step = min(step, STEP_UP_COM)
                new = m + step + bonus
            elif o["status"] == "PARTIAL":
                new = m
            elif o["status"] == "OUTBID":
                new = m - STEP_OUTBID
                # price_max may count every offer or only the ones that sold (not documented): use it only
                # when it is below our markup, i.e. it can only be a price that beat us
                if o["rfq_id"] in price_max and s.get("price_per_kg") and m > 0:
                    tv_ours = s["price_per_kg"] / m
                    if CLOSE_MAX_TV and getattr(memory, "rfqs", None):
                        # several of our lots may fit this RFQ and price_max may belong to the best of them: divide
                        # by the highest TV that fits, a safe lower bound of the cut-off (same lots for every team)
                        rq = (memory.rfqs.get(rnd["round_id"]) or {}).get(o["rfq_id"])
                        tvs = [tv_all[lid] for lid, smp in memory.lots.items()
                               if rq and rq.get("spec") and smp and lid in tv_all and spec_ok(smp, rq["spec"])]
                        tv_ours = max([tv_ours] + tvs)
                    closing = price_max[o["rfq_id"]] / tv_ours
                    if closing < m:
                        new = max(closing - OUTBID_UNDERCUT, m - MAX_DROP)
            elif o["status"] == "ABOVE_LIMIT":
                ceiling[buyer] = min(ceiling.get(buyer, 9.0), m)
                new = m - CEILING_MARGIN - STEP_OUTBID
            else:                                   # SPEC_MISMATCH, CANCELLED: says nothing about price
                continue
            moves.setdefault(buyer, []).append(new)
            moves.setdefault(btype, []).append(new)
            if o["status"] in ("ACCEPTED", "PARTIAL"):
                sold_moves.setdefault(buyer, []).append(new)
                sold_moves.setdefault(btype, []).append(new)
        for key, values in moves.items():
            if LEARN_AGG == "sold" and sold_moves.get(key):
                out[key] = min(sold_moves[key])     # a losing high probe does not drag down a price that sold
            elif LEARN_AGG == "median":
                values = sorted(values)
                out[key] = values[(len(values) - 1) // 2]   # lower median
            else:
                out[key] = min(values)              # several lots to one buyer: follow the most cautious result
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


def market_anchors(memory, tv: dict) -> dict[str, tuple[float, bool]]:
    """buyer -> (median winning markup over its single-lot RFQs in the newest round that has one, buyer filled?)."""
    if memory is None or not getattr(memory, "rfqs", None):
        return {}
    out: dict[str, tuple[float, bool]] = {}
    real = [r for r in memory.rounds if r.get("round_id") in memory.rfqs]
    for age, rnd in enumerate(reversed(real)):
        if age >= ANCHOR_AGE:
            break
        info = memory.rfqs[rnd["round_id"]]
        per_buyer: dict[str, list] = {}
        for row in rnd.get("market_summary") or []:
            rq = info.get(row["rfq_id"])
            if not rq or not row.get("price_max") or not rq.get("spec") or row["buyer"] in out:
                continue
            fit = [lid for lid, smp in memory.lots.items() if smp and lid in tv and spec_ok(smp, rq["spec"])]
            if len(fit) != 1:
                continue                                   # several lots fit: price_max may be another lot
            filled = (row.get("bought_kg") or 0) >= (row.get("wanted_kg") or 0)
            per_buyer.setdefault(row["buyer"], []).append((row["price_max"] / tv[fit[0]], filled))
        for b, xs in per_buyer.items():
            vals = sorted(x for x, _ in xs)
            out[b] = (vals[len(vals) // 2], all(f for _, f in xs))
    return out


def crowding(memory) -> dict[str, float]:
    """buyer -> offers per bag wanted in the newest cleared round."""
    rounds = memory.rounds if memory is not None else []
    if not rounds:
        return {}
    offers, bags = {}, {}
    for row in rounds[-1].get("market_summary") or []:
        b = row.get("buyer")
        if not b:
            continue
        offers[b] = offers.get(b, 0) + (row.get("offers") or 0)
        bags[b] = bags.get(b, 0) + max(1, (row.get("wanted_kg") or 0) // BAG)
    return {b: offers[b] / bags[b] for b in offers if bags[b] > 0}


def proven_markups(memory) -> dict[str, float]:
    """Highest markup that sold (ACCEPTED or PARTIAL) per buyer and per buyer type, plus PROVEN_BASE per type."""
    out = dict(PROVEN_BASE)
    for rnd in (memory.rounds if memory is not None else []):
        info = {row["rfq_id"]: (row["buyer"], row["buyer_type"]) for row in rnd.get("market_summary", [])}
        sent = {(o["rfq_id"], o["lot_id"]): o for o in memory.sent.get(rnd["round_id"], [])}
        for o in rnd.get("my_offers", []):
            s = sent.get((o["rfq_id"], o["lot_id"]))
            if o["status"] in ("ACCEPTED", "PARTIAL") and s and s.get("markup") and o["rfq_id"] in info:
                for k in info[o["rfq_id"]]:
                    out[k] = max(out.get(k, 0.0), s["markup"])
    return out


def instant_only(state: dict, ctx, lot: dict) -> bool:
    """This round only INSTANT RFQs take this lot (e.g. moisture over every COMMERCIAL/SPECIALTY spec)."""
    types = {r["buyer_type"] for r in state["rfqs"] if lot_fits(ctx, lot, r)}
    return types == {"INSTANT"}


def instant_early(state: dict, ctx) -> bool:
    rnd = state["round"]
    return bool(INSTANT_EARLY_FRAC) and rnd["kind"] != "practice" \
        and rnd["number"] <= math.ceil(total_rounds(state, ctx) * INSTANT_EARLY_FRAC)


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
    if SPEC_MARGIN and rfq.get("buyer_type") != "INSTANT":
        sp, smp = rfq["spec"], lot["sample"]
        if smp["defect_pct"] > sp["max_defect_pct"] - SPEC_MARGIN or \
                smp["moisture_pct"] > sp["max_moisture_pct"] - SPEC_MARGIN / 5:
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
    value = m.get(buyer, m.get(buyer_type, MARKUP.get(buyer_type, 1.0)))
    anchor = (getattr(ctx, "anchors", None) or {}).get(buyer)
    if anchor:
        a, filled = anchor
        # filled: the cut-off is known, go just under it (up or down) · unfilled: everyone under the ceiling sold,
        # so the anchor is only a lower bound
        under = ANCHOR_UNDER
        if EARLY_UNDER and getattr(ctx, "early", False):
            under += EARLY_UNDER
        if UNDER_CROWD:
            under += UNDER_CROWD * max(0.0, (getattr(ctx, "crowd", None) or {}).get(buyer, 1.0) - 1.0)
        value = a - under if filled else max(value, a - under)
    value += TYPE_OFFSET.get(buyer_type, 0.0)          # teammate's nudge from strategy.env (max ±OFFSET_LIMIT)
    return min(max(value, type_floor(ctx, buyer_type)), cap)


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
    war = getattr(ctx, "war", False)
    fmid = WAR_FLOOR_MID if war else FLOOR_MID
    short_floor = WAR_SHORT_FLOOR if war else SHORT_FLOOR
    floor = fmid - (fmid - floor_end) * late
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
    elif cap and PRESSURE and rnd["kind"] != "practice":
        # See-It: count how much of what we offer really sells. If stock left > capacity x fill rate x rounds
        # after this one, we are behind: drop toward the end floor (seed 3006: Instant-only stock lost R2-R8 at 0.92)
        fill = fill_rate(ctx.memory)
        need = sellable / max(1.0, cap * fill * max(1, rounds_left(state, ctx) - 1))
        if need > 1.0:
            floor = max(floor_end, floor - PRESSURE_DROP * min(1.0, (need - 1.0) / 0.5))
    floor = max(min(floor, max(fmid, FLOOR_EARLY if EARLY_HALF and first_half else fmid)), floor_end)
    if short_floor and rnd["kind"] != "practice" and market_short(ctx.memory):
        floor = max(min(floor, short_floor), floor_end)
        ctx.notes.append(f"guard: market oversupplied (INSTANT filled >= {SHORT_FILL:.0%}) -> floor {floor:.2f}")
    return floor


def fill_rate(memory, last: int = 3, prior_kg: float = 600, prior: float = 0.7) -> float:
    """Share of the kg we offered in the last rounds that sold, pulled toward `prior` (See-It's fill prior)."""
    offered = sold = 0
    for rnd in [r for r in (memory.rounds if memory is not None else []) if r.get("kind") != "practice"][-last:]:
        for o in rnd.get("my_offers") or []:
            if o["status"] in ("SPEC_MISMATCH", "CANCELLED"):
                continue
            offered += o["kg"]
            sold += o.get("filled_kg") or 0
    return (sold + prior * prior_kg) / (offered + prior_kg)


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
                if x is None:
                    continue
                span = re.fullmatch(r"\D*(\d+)\s*[-–]\s*\D*(\d+)\s*", str(x))   # the real calendar says "6-8"
                if span:
                    nums += list(range(int(span.group(1)), int(span.group(2)) + 1))
                else:
                    nums += [int(n) for n in re.findall(r"\d+", str(x))]
        lo, hi = e.get("from", e.get("start")), e.get("to", e.get("end"))
        if lo is not None and hi is not None:
            nums += list(range(int(re.findall(r"\d+", str(lo))[0]), int(re.findall(r"\d+", str(hi))[0]) + 1))
        for n in nums:
            out[n] = label
            if isinstance(e.get("demand_weight"), (int, float)):
                CALENDAR_WEIGHT[n] = float(e["demand_weight"])
    return out


# round number -> demand_weight from GET /v1/calendar when the market gives it (filled by calendar_labels)
CALENDAR_WEIGHT: dict[int, float] = {}


def round_weight(n: int, label) -> float:
    """The market's own demand_weight for round n if it gave one, else a guess from the label."""
    return CALENDAR_WEIGHT.get(n, label_weight(label))


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
        weight_now = round_weight(rnd["number"], labels.get(rnd["number"]))
        weight_left = sum(round_weight(n, labels.get(n)) for n in upcoming)
    out = {}
    early = instant_early(state, ctx)
    early_left = math.ceil(total_rounds(state, ctx) * INSTANT_EARLY_FRAC) - rnd.get("number", 0) + 1
    for lot in state["lots"]:
        avail = lot["kg_available"] // BAG * BAG
        pace = WAR_PACE if getattr(ctx, "war", False) else PACE
        if PRECLOSE_BOOST and rnd["kind"] != "practice" and \
                total_rounds(state, ctx) - 6 <= rnd.get("number", 0) <= total_rounds(state, ctx) - 3:
            pace *= PRECLOSE_BOOST
        share = avail if left <= max(1, CLEAR_LAST) else math.ceil(avail * weight_now / weight_left * pace / BAG) * BAG
        if early and early_left >= 1 and instant_only(state, ctx, lot):
            share = max(share, math.ceil(avail / early_left / BAG) * BAG)   # sold out by the end of the early rounds
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
    keep_com = KEEP_COM_LAST >= 0 and state["round"]["kind"] != "practice" \
        and rounds_left(state, ctx) > KEEP_COM_LAST
    for lot in state["lots"]:
        if budget[lot["lot_id"]] < BAG:
            continue
        better = keep_com and any(r["buyer_type"] != "INSTANT" and lot_fits(ctx, lot, r) for r in state["rfqs"])
        for rfq in state["rfqs"]:
            if better and rfq["buyer_type"] == "INSTANT":
                continue
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
LAST_CAP_INST = float(os.environ.get("STRATEGY_LAST_CAP_INST", "0"))   # 0 = off (default from 19 Nov; was 0.86)
# Hold a lot for SPECIALTY only while it passes a SPECIALTY RFQ that is open now (real spec, e.g. screen >= 16-17),
# not our guessed SPECIALTY_SPEC: Practice-01 held LOT-5 (screen 15) for buyers it could never sell to.
HOLD_REAL_SPEC = os.environ.get("STRATEGY_HOLD_REAL_SPEC", "1") == "1"


def hold_until(state: dict, ctx) -> float:
    half = total_rounds(state, ctx) / 2
    if not HOLD_TO_FESTIVAL_END:
        return half
    fest = [n for n, lab in calendar_labels(ctx.calendar).items() if label_weight(lab) > 1.0]
    fest += [n for n, w in CALENDAR_WEIGHT.items() if w > 1.0]
    return max([half] + fest)


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
            and rnd["number"] <= hold_until(state, ctx):
        if HOLD_REAL_SPEC:
            specs = [r["spec"] for r in state["rfqs"] if r["buyer_type"] == "SPECIALTY"]
            held = {l["lot_id"] for l in state["lots"] if any(spec_ok(l["sample"], sp) for sp in specs)}
            if HOLD_IF_BETTER:
                spec_best = max((markup_for(ctx, r["buyer"], "SPECIALTY") for r in state["rfqs"]
                                 if r["buyer_type"] == "SPECIALTY"), default=0.0)
                com_best = max((markup_for(ctx, r["buyer"], "COMMERCIAL") for r in state["rfqs"]
                                if r["buyer_type"] == "COMMERCIAL"), default=0.0)
                if spec_best < com_best:
                    held = set()                    # SPECIALTY pays less than COMMERCIAL now: do not wait for it
        else:
            held = {l["lot_id"] for l in state["lots"] if passes_specialty(l["sample"])}
    def value_key(c):
        m = markup_for(ctx, c["buyer"], c["buyer_type"])
        if EV_CROWD:
            m -= EV_CROWD * math.log(max(1.0, (getattr(ctx, "crowd", None) or {}).get(c["buyer"], 1.0)))
        return -round(m, 2)

    for c in sorted(cands, key=lambda c: (value_key(c), c["rfq_bags"] if RFQ_SMALL else 0, lot_key(c), order[id(c)])):
        if c["lot_id"] in held and c["buyer_type"] != "SPECIALTY":
            continue
        bags = min(budget[c["lot_id"]] // BAG, rfq_left[c["rfq_id"]])
        per = MAX_PER_RFQ if rnd["kind"] == "practice" or rnd["number"] <= total_rounds(state, ctx) / 2 else MAX_PER_RFQ_LATE
        if per:
            bags = min(bags, per)
        if bags > 0:
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": bags,
                         "markup": markup_for(ctx, c["buyer"], c["buyer_type"])})
            budget[c["lot_id"]] -= bags * BAG
            rfq_left[c["rfq_id"]] -= bags
    cap = state.get("capacity_left_kg")
    if FILL_CAP and rnd["kind"] != "practice" and cap:
        room = cap // BAG - sum(p["bags"] for p in plan)
        stock = {l["lot_id"]: l["kg_available"] // BAG for l in state["lots"]}
        for p in plan:
            stock[p["lot_id"]] -= p["bags"]
        pairs = {(p["lot_id"], p["rfq_id"]) for p in plan}
        for c in sorted(cands, key=lambda c: (value_key(c), lot_key(c), order[id(c)])):
            if room <= 0:
                break
            if (c["lot_id"], c["rfq_id"]) in pairs or (c["lot_id"] in held and c["buyer_type"] != "SPECIALTY"):
                continue
            bags = min(stock[c["lot_id"]], rfq_left[c["rfq_id"]], room, MAX_PER_RFQ or 99)
            if bags > 0:
                plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": bags,
                             "markup": markup_for(ctx, c["buyer"], c["buyer_type"])})
                stock[c["lot_id"]] -= bags
                rfq_left[c["rfq_id"]] -= bags
                room -= bags
                pairs.add((c["lot_id"], c["rfq_id"]))
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
    proven = proven_markups(ctx.memory)
    offers, seen = [], set()
    pair_offer: dict = {}                           # (lot, rfq) -> [offer, kg allowed for that pair]
    for p in plan:
        lot, rfq = lots.get(p["lot_id"]), rfqs.get(p["rfq_id"])
        key = (p["lot_id"], p["rfq_id"])
        if MERGE_PAIRS and key in seen and lot is not None and rfq is not None and key in pair_offer:
            # the same pair again (e.g. the INSTANT rush took 1 bag, the rules wanted 3): top the first offer up
            # with room left over at the end, at the first offer's price, never past that pair's own limit
            o, allowed = pair_offer[key]
            add = min(p["bags"] * BAG - o["kg"], allowed - o["kg"], lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room)
            add = add // BAG * BAG
            if add >= BAG:
                o["kg"] += add
                lot_left[lot["lot_id"]] -= add
                rfq_left[rfq["rfq_id"]] -= add
                room -= add
            continue
        if lot is None or rfq is None or key in seen or not lot_fits(ctx, lot, rfq):
            ctx.notes.append(f"guard dropped {p}")
            continue
        kg = min(p["bags"] * BAG, lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room) // BAG * BAG
        if kg < BAG:
            continue
        btype = rfq.get("buyer_type", "")
        # cap: the type's cap and the buyer's known ceiling, but never under the floor (selling under salvage loses more)
        cap = min(type_cap(btype), ceilings.get(rfq.get("buyer"), 9.0))
        anc = (getattr(ctx, "anchors", None) or {}).get(rfq.get("buyer"))
        if anc and anc[1]:
            cap = min(cap, anc[0])                  # the market just sold this buyer at most this: never ask more
        floor = max(type_floor(ctx, btype), MIN_MARKUP)
        # cap sits CEILING_MARGIN under the markup the buyer refused; only a floor at or above that refused markup
        # is sure to fail again (between the two, the floor is still worth a try)
        if floor >= cap + CEILING_MARGIN - 1e-9:
            ctx.notes.append(f"guard dropped {p}: {rfq.get('buyer')} ceiling {cap:.3f} under floor {floor:.3f}")
            continue
        markup = max(min(p["markup"], cap), floor)
        capped = False
        crowd = getattr(ctx, "crowd", None) or {}
        if CROWD_MAX and crowd.get(rfq.get("buyer"), 0) > CROWD_MAX and kg > CROWD_BAGS * BAG:
            other = any(r["buyer"] != rfq.get("buyer") and crowd.get(r["buyer"], 0) <= CROWD_MAX and lot_fits(ctx, lot, r)
                        for r in state["rfqs"])
            if other:
                ctx.notes.append(f"guard: {rfq.get('buyer')} crowded {crowd[rfq['buyer']]:.1f} offers/bag, "
                                 f"{kg // BAG} -> {CROWD_BAGS} bags")
                kg = CROWD_BAGS * BAG
                capped = True
        tested = max(proven.get(rfq.get("buyer"), 0.0), proven.get(btype, 0.0))
        if UNTESTED_BAGS and markup > tested + 1e-9 and kg > UNTESTED_BAGS * BAG and not (EARLY_FULL and getattr(ctx, "early", False)):
            ctx.notes.append(f"guard: {rfq.get('buyer')} @{markup:.3f} never sold (best {tested:.3f}), "
                             f"{kg // BAG} -> {UNTESTED_BAGS} bags")
            kg = UNTESTED_BAGS * BAG
            capped = True
        offers.append({"rfq_id": rfq["rfq_id"], "lot_id": lot["lot_id"], "kg": kg,
                       "price_per_kg": round(markup * ctx.tv[lot["lot_id"]], 2)})
        # the pair limit: untested price / crowded buyer caps stay, otherwise up to what the plans ask for later
        pair_offer[key] = [offers[-1], kg if capped else 10**9]
        seen.add((p["lot_id"], p["rfq_id"]))
        lot_left[lot["lot_id"]] -= kg
        rfq_left[rfq["rfq_id"]] -= kg
        room -= kg
    return offers


def decide(state: dict, ctx: Context) -> tuple[list[dict], str]:
    """Returns (offers, source) where source is "llm" or "rules"."""
    reload_settings(ctx)
    last = state["round"]["kind"] != "practice" and rounds_left(state, ctx) <= 1
    # last round: what is left goes to salvage 0.80 if it loses, so do not raise a price that just sold
    rnd0 = state["round"]
    ctx.early = rnd0["kind"] != "practice" and (rnd0.get("number") or 99) <= EARLY_ROUNDS
    ctx.war = WAR_MODE and state["round"]["kind"] != "practice" and market_short(ctx.memory)
    if ctx.war:
        ctx.notes.append("guard: price war (market oversupplied) -> step +%.3f, floor %.2f, pace %.1f"
                         % (WAR_STEP_UP, WAR_FLOOR_MID, WAR_PACE))
    step = 0.0 if (last and LAST_NO_STEP) else (WAR_STEP_UP if ctx.war else None)
    ctx.markups = learned_markups(ctx.memory, step_up=step, tv_all=ctx.tv)
    ctx.floor = markup_floor(state, ctx)
    ctx.anchors = market_anchors(ctx.memory, ctx.tv) if ANCHOR else {}
    ctx.crowd = crowding(ctx.memory) if (CROWD_MAX or EV_CROWD or UNDER_CROWD) else {}
    ctx.cap = {}
    if LAST_CAP_INST and state["round"]["kind"] != "practice" and rounds_left(state, ctx) <= 1:
        ctx.cap["INSTANT"] = LAST_CAP_INST
    cands = candidates(state, ctx)
    if not cands:
        return [], "none"
    if PRACTICE_PROBE and state["round"]["kind"] == "practice":
        return guard(state, ctx, probe_plan(state, ctx)), "probe"
    # early rounds: INSTANT-only lots go first (up to half the capacity), so neither the LLM's picks nor the
    # higher-markup buyers use up the round before them
    rules_initial = rule_plan(state, ctx, cands)
    rush = instant_rush(state, ctx, rules_initial)
    if rush:
        rush_seen = {(r["lot_id"], r["rfq_id"]) for r in rush}
        cands_rem = [c for c in cands if (c["lot_id"], c["rfq_id"]) not in rush_seen]
        rules = rule_plan(state, ctx, cands_rem)
    else:
        rules = rules_initial

    plan = llm_plan(state, ctx, cands) if ctx.llm else None
    if plan:
        # The LLM tends to offer too little. Rules fill whatever volume it left unused
        # (guard skips lot×RFQ pairs the LLM already used and stops at the round capacity).
        offers = guard(state, ctx, rush + plan + rules)
        n_llm = len(guard(state, Context(ctx.rules, ctx.calendar, ctx.tv, ctx.rng), plan))
        return offers, "llm" if len(offers) == n_llm else f"llm+rules ({n_llm}+{len(offers) - n_llm})"
    return guard(state, ctx, rush + rules), "rules"


def probe_plan(state: dict, ctx) -> list[dict]:
    """Practice round: 1 bag per RFQ, best-TV lot that fits, markups stepped through PROBE_LADDER per buyer."""
    plan, step = [], {}
    rfqs = list(state["rfqs"])
    ctx.rng.shuffle(rfqs)
    # capacity is small (~5 bags): cover every buyer once first, SPECIALTY (least known, pays most) before the rest
    order = {"SPECIALTY": 0, "COMMERCIAL": 1, "INSTANT": 2}
    seen: set = set()
    first, rest = [], []
    for r in sorted(rfqs, key=lambda r: order.get(r["buyer_type"], 3)):
        (rest if r["buyer"] in seen else first).append(r)
        seen.add(r["buyer"])
    for r in first + rest:
        fits = [l for l in state["lots"] if l["kg_available"] >= BAG and lot_fits(ctx, l, r)]
        if not fits:
            continue
        lot = max(fits, key=lambda l: ctx.tv[l["lot_id"]])
        i = step.get(r["buyer"], 0)
        step[r["buyer"]] = i + 1
        base = markup_for(ctx, r["buyer"], r["buyer_type"])
        plan.append({"lot_id": lot["lot_id"], "rfq_id": r["rfq_id"], "bags": 1,
                     "markup": base + PROBE_LADDER[i % len(PROBE_LADDER)]})
    return plan


def instant_rush(state: dict, ctx, rules: list[dict]) -> list[dict]:
    """Early rounds: the rule offers of INSTANT-only lots, sent first (before the LLM's picks and the higher
    markups) but only up to half the round capacity, so SPECIALTY/COMMERCIAL lots still get offered."""
    if not instant_early(state, ctx):
        return []
    lots = {l["lot_id"]: l for l in state["lots"]}
    room = state.get("capacity_left_kg")
    # round DOWN (at least 1 bag): with a 3-bag round, rounding up gave INSTANT 2 of 3 bags (mock 5 lots, seed 2020)
    if RUSH_ROUND_DOWN:
        half = max(1, (room // BAG) // 2) * BAG if room is not None else 10**9
    else:
        half = (room // BAG + 1) // 2 * BAG if room is not None else 10**9
    out = []
    for p in rules:
        if instant_only(state, ctx, lots[p["lot_id"]]) and half >= BAG:
            bags = min(p["bags"], half // BAG)
            out.append({**p, "bags": bags})
            half -= bags * BAG
    return out
