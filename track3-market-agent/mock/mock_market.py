"""Mock coffee market for testing the agent offline. Follows คู่มือ-API.md (v1).

    python mock/mock_market.py                         # port 8765, key sk-T01-mock
    python mock/mock_market.py --round-seconds 4 --seed 7

What is copied from the organizer's material (คู่มือ-API.md, ใบประเมินมูลค่า.xlsx, slides 17-18):
  - endpoints, phases, JSON shapes, error codes, kg in multiples of 60
  - TV formula and its numbers, salvage 0.80 x TV, PR = (revenue + 0.8 x TV unsold) / TV total
  - matching: drop offers failing spec (true quality) or above the buyer's hidden ceiling,
    sort by price / true TV, fill the RFQ, pay as bid
  - round capacity about 1.5 x the even share per round

What is guessed (change with the flags or the constants below once the real market shows otherwise):
  - buyer ceilings, RFQ sizes, the festival calendar, sample noise, how rival teams bid
"""
import argparse
import json
import math
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BAG = 60

RULES = {
    "p0": 120.0,
    "defect_steps": [
        {"max_defect_pct": 1, "step": 1.08, "grade": "PREMIUM"},
        {"max_defect_pct": 3, "step": 1.04, "grade": "GRADE_1"},
        {"max_defect_pct": 5, "step": 1.00, "grade": "GRADE_2"},
        {"max_defect_pct": 7, "step": 0.96, "grade": "GRADE_3"},
        {"max_defect_pct": 15, "step": 0.85, "grade": "BELOW_STANDARD"},
        {"max_defect_pct": 100, "step": 0.60, "grade": "REJECT"},
    ],
    "moisture": {"ref": 12.5, "penalty_per_pp": 0.02, "hard_limit": 15.0, "hard_factor": 0.6},
    "screen_factors": {"12": 0.92, "13": 0.97, "14": 1.0, "15": 1.0, "16": 1.03, "17": 1.03, "18": 1.06},
    "bag_kg": BAG,
    "salvage_ratio": 0.8,
    "scoring": {"appraisal_points": 20, "sales_points": 80, "pr_zero_at": 0.8,
                "pr": "(revenue + salvage_ratio x TV of unsold) / TV of all"},
    "timeline": {"rounds": 8},
}

# Guessed hidden ceilings (as a multiple of true TV) per buyer type: (low, high) of a uniform draw per buyer.
CEILINGS = {"SPECIALTY": (1.22, 1.40), "COMMERCIAL": (1.06, 1.18), "INSTANT": (0.96, 1.06)}
BUYERS = [
    ("1 ชุมพรคั่วบด", "COMMERCIAL"), ("2 ปะทิวโรสเตอรี่", "COMMERCIAL"), ("3 สยามคอมเมิร์ซ", "COMMERCIAL"),
    ("4 อินสแตนท์ไทย", "INSTANT"), ("5 ทรีอินวัน", "INSTANT"),
    ("6 สเปเชียลตี้ชุมพร", "SPECIALTY"), ("7 ไฟน์โรบัสตา", "SPECIALTY"),
]
SPECS = {
    "SPECIALTY": [{"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 15},
                  {"max_defect_pct": 1.5, "max_moisture_pct": 12.0, "min_screen": 16}],
    "COMMERCIAL": [{"max_defect_pct": 7.0, "max_moisture_pct": 12.5, "min_screen": None},
                   {"max_defect_pct": 5.0, "max_moisture_pct": 13.0, "min_screen": 13}],
    "INSTANT": [{"max_defect_pct": 15.0, "max_moisture_pct": 14.0, "min_screen": None}],
}


def grade(d):
    for s in RULES["defect_steps"]:
        if d <= s["max_defect_pct"]:
            return s["grade"], s["step"]
    return RULES["defect_steps"][-1]["grade"], RULES["defect_steps"][-1]["step"]


def tv_of(q):
    d, m, s = q["defect_pct"], q["moisture_pct"], q["screen"]
    mo = RULES["moisture"]
    if m <= mo["ref"]:
        mf = 1.0
    elif m <= mo["hard_limit"]:
        mf = (1 - m / 100) / (1 - mo["ref"] / 100) - mo["penalty_per_pp"] * (m - mo["ref"])
    else:
        mf = mo["hard_factor"]
    sf = RULES["screen_factors"][str(min(max(int(s), 12), 18))]
    return RULES["p0"] * (1 - d / 100) * grade(d)[1] * mf * sf


def passes(q, spec):
    return (q["defect_pct"] <= spec["max_defect_pct"] and q["moisture_pct"] <= spec["max_moisture_pct"]
            and (spec["min_screen"] is None or q["screen"] >= spec["min_screen"]))


class ApiError(Exception):
    def __init__(self, status, code, message, field=None, hint=None, allowed=None):
        super().__init__(message)
        self.status, self.body = status, {"error": {"code": code, "message": message, "field": field,
                                                     "hint": hint, "allowed": allowed}}


class RivalTeam:
    """A rival seller. Kinds roughly match the bots named in the starter README."""

    def __init__(self, tid, kind, lots, rng):
        self.tid, self.kind, self.rng = tid, kind, rng
        self.lots = lots                      # list of dicts: lot_id, true, kg_remaining
        base = {"fixed": 1.00, "undercut": 0.93, "adaptive": 1.05, "random": 1.0}[kind]
        self.markup = {"SPECIALTY": base + (0.12 if kind == "adaptive" else 0), "COMMERCIAL": base, "INSTANT": base}

    def offers(self, rfqs, rounds_left, capacity):
        out, room = [], capacity
        order = self.rng.sample(rfqs, len(rfqs))
        for lot in sorted(self.lots, key=lambda l: -tv_of(l["true"])):
            budget = min(lot["kg_remaining"], max(BAG, math.ceil(lot["kg_remaining"] / max(rounds_left, 1) * 1.3 / BAG) * BAG))
            for r in order:
                if budget < BAG or room < BAG:
                    break
                if not passes(lot["sample"], r["spec"]):
                    continue
                kg = min(budget, r["kg"], room) // BAG * BAG
                if kg < BAG:
                    continue
                m = self.markup[r["buyer_type"]]
                if self.kind == "random":
                    m = self.rng.uniform(0.9, 1.25)
                tv_est = tv_of(lot["sample"])
                out.append({"team": self.tid, "lot": lot, "rfq_id": r["rfq_id"], "kg": kg,
                            "price_per_kg": round(m * tv_est * self.rng.uniform(0.985, 1.015), 2)})
                budget -= kg
                room -= kg
        return out

    def learn(self, results):
        if self.kind != "adaptive":
            return
        for o in results:
            t = o["buyer_type"]
            if o["status"] == "ACCEPTED":
                self.markup[t] = min(1.45, self.markup[t] + 0.02)
            elif o["status"] in ("OUTBID", "ABOVE_LIMIT"):
                self.markup[t] = max(0.86, self.markup[t] - 0.03)


class Market:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.seed = args.seed
        self.reset()

    # ------------------------------------------------------------------ game setup
    def reset(self):
        with self.lock:
            a = self.args
            self.rng = random.Random(self.seed)
            self.seed = (self.seed or 0) + 1 if self.seed is not None else None
            self.rounds = a.rounds
            self.calendar = []
            fest = set(self.rng.sample(range(2, self.rounds), k=min(2, max(0, self.rounds - 2))))
            for i in range(1, self.rounds + 1):
                label = "เทศกาล" if i in fest else ("ใกล้ปิดตลาด" if i == self.rounds else "ปกติ")
                self.calendar.append({"round": f"R{i}", "label": label,
                                      "demand": 1.4 if label == "เทศกาล" else (0.8 if label == "ใกล้ปิดตลาด" else 1.0)})
            templates = [self._random_quality() for _ in range(a.lots)]
            self.lots = []
            for i, (q, bags) in enumerate(templates, 1):
                self.lots.append(self._make_lot(f"LOT-T01-{i}", q, bags))
            self.rivals = []
            kinds = getattr(a, "rival_kinds", "fixed,undercut,adaptive,random").split(",")
            for t in range(2, a.teams + 1):
                lots = [self._make_lot(f"LOT-T{t:02d}-{i}", self._jitter(q), bags) for i, (q, bags) in enumerate(templates, 1)]
                self.rivals.append(RivalTeam(f"T{t:02d}", kinds[t % len(kinds)], lots, random.Random(self.rng.random())))
            shift = getattr(a, "ceiling_shift", 0.0)
            self.ceiling = {name: self.rng.uniform(*CEILINGS[typ]) + shift for name, typ in BUYERS}
            total_kg = sum(l["kg_total"] for l in self.lots)
            self.capacity = math.ceil(total_kg / self.rounds * 1.5 / BAG) * BAG
            # WAITING until the agent first reads /v1/state, like the private scoring market ("รอทีมกดเริ่ม")
            self.phase, self.round_no, self.round_kind = "WAITING", 0, None
            self.phase_ends = float("inf")
            self.appraisal = None
            self.rfqs, self.my_offers, self.results, self.next_offer = [], [], {}, 1
            self.revenue, self.kg_sold = 0.0, 0
            self.rival_revenue = {rv.tid: 0.0 for rv in self.rivals}

    def _random_quality(self):
        r = self.rng
        return ({"defect_pct": round(r.choice([r.uniform(0.3, 1.2), r.uniform(1, 3.5), r.uniform(3, 7.5), r.uniform(6, 12)]), 2),
                 "moisture_pct": round(r.uniform(10.8, 13.8), 2), "screen": r.randint(12, 18)},
                r.randint(3, 7))

    def _jitter(self, q):
        return {"defect_pct": max(0.0, round(q["defect_pct"] + self.rng.gauss(0, 0.2), 2)),
                "moisture_pct": round(q["moisture_pct"] + self.rng.gauss(0, 0.1), 2), "screen": q["screen"]}

    def _make_lot(self, lot_id, true, bags):
        a = self.args
        sample = {"defect_pct": max(0.0, round(true["defect_pct"] + self.rng.gauss(0, a.defect_noise), 2)),
                  "moisture_pct": round(true["moisture_pct"] + self.rng.gauss(0, a.moisture_noise), 2),
                  "screen": true["screen"]}
        return {"lot_id": lot_id, "true": true, "sample": sample, "kg_total": bags * BAG, "kg_remaining": bags * BAG,
                "origin": "ชุมพร - ปะทิว", "process": self.rng.choice(["Washed", "Natural", "Honey"])}

    def _new_rfqs(self):
        cal = self.calendar[self.round_no - 1]
        supply = sum(l["kg_total"] for l in self.lots) * self.args.teams
        per_round = supply * 0.9 / self.rounds * cal["demand"]
        buyers = [b for b in BUYERS if b[1] != "SPECIALTY" or self.round_no >= 3]
        weights = {"SPECIALTY": 0.6, "COMMERCIAL": 1.4, "INSTANT": 1.0}
        wsum = sum(weights[t] for _, t in buyers)
        rfqs = []
        for i, (name, typ) in enumerate(buyers, 1):
            bags = max(1, round(per_round * weights[typ] / wsum / BAG * self.rng.uniform(0.7, 1.3)))
            rfqs.append({"rfq_id": f"R{self.round_no}-{i:02d}", "buyer": name, "buyer_type": typ,
                         "kg": bags * BAG, "bags": bags, "spec": self.rng.choice(SPECS[typ])})
        return rfqs

    # ------------------------------------------------------------------ clock
    def tick(self):
        with self.lock:
            now = time.time()
            if now < self.phase_ends or self.phase in ("CLOSED", "WAITING"):
                return
            a = self.args
            if self.phase == "SETUP":
                self.phase, self.phase_ends = "APPRAISAL", now + a.appraisal_seconds
            elif self.phase in ("APPRAISAL", "ROUND_CLEARING"):
                if self.phase == "ROUND_CLEARING":
                    self._clear()
                if self.round_no >= self.rounds:
                    self._close()
                    return
                self.round_no += 1
                self.rfqs, self.my_offers = self._new_rfqs(), []
                self.phase, self.phase_ends = "ROUND_OPEN", now + a.round_seconds
            elif self.phase == "ROUND_OPEN":
                self.phase, self.phase_ends = "ROUND_CLEARING", now + 0.5

    def _clear(self):
        rid = f"R{self.round_no}"
        left = self.rounds - self.round_no + 1
        rival_cap = self.capacity
        all_offers = [dict(o, team="T01", lot=self._lot(o["lot_id"])) for o in self.my_offers if o["status"] == "OPEN"]
        for rv in self.rivals:
            all_offers += rv.offers(self.rfqs, left, rival_cap)
        summary, rival_results = [], {rv.tid: [] for rv in self.rivals}
        for rfq in self.rfqs:
            mine = [o for o in all_offers if o["rfq_id"] == rfq["rfq_id"]]
            ok = []
            for o in mine:
                tv = tv_of(o["lot"]["true"])
                if not passes(o["lot"]["true"], rfq["spec"]):
                    o["status"] = "SPEC_MISMATCH"
                elif o["price_per_kg"] / tv > self.ceiling[rfq["buyer"]]:
                    o["status"] = "ABOVE_LIMIT"
                else:
                    o["ratio"] = o["price_per_kg"] / tv
                    ok.append(o)
            self.rng.shuffle(ok)
            ok.sort(key=lambda o: round(o["ratio"], 4))
            need, bought, prices = rfq["kg"], 0, []
            for o in ok:
                take = min(o["kg"], need, o["lot"]["kg_remaining"]) // BAG * BAG
                o["filled_kg"] = take
                if take <= 0:
                    o["status"] = "OUTBID"
                    continue
                need -= take
                bought += take
                o["lot"]["kg_remaining"] -= take
                o["status"] = "ACCEPTED" if take == o["kg"] else "PARTIAL"
                prices.append(o["price_per_kg"])
                if o["team"] == "T01":
                    self.revenue += take * o["price_per_kg"]
                    self.kg_sold += take
                else:
                    self.rival_revenue[o["team"]] += take * o["price_per_kg"]
            all_prices = [o["price_per_kg"] for o in mine]
            summary.append({"rfq_id": rfq["rfq_id"], "buyer": rfq["buyer"], "buyer_type": rfq["buyer_type"],
                            "wanted_kg": rfq["kg"], "bought_kg": bought, "offers": len(mine),
                            "price_min": min(all_prices) if all_prices else None,
                            "price_max": max(all_prices) if all_prices else None,
                            "price_avg": round(sum(all_prices) / len(all_prices), 2) if all_prices else None})
            for o in mine:
                o.setdefault("filled_kg", 0)
                if o["team"] != "T01":
                    rival_results[o["team"]].append({"buyer_type": rfq["buyer_type"], "status": o["status"]})
        for rv in self.rivals:
            rv.learn(rival_results[rv.tid])
        for o in self.my_offers:
            o.setdefault("filled_kg", 0)
            if o["status"] == "OPEN":
                o["status"] = "OUTBID"
        self.results[rid] = {"round_id": rid, "kind": "real",
                             "my_offers": [{k: o[k] for k in ("offer_id", "rfq_id", "lot_id", "kg", "price_per_kg", "status", "filled_kg")}
                                           for o in self.my_offers],
                             "market_summary": summary}

    def _close(self):
        self.phase, self.phase_ends = "CLOSED", float("inf")

    def _lot(self, lot_id):
        return next(l for l in self.lots if l["lot_id"] == lot_id)

    # ------------------------------------------------------------------ views
    def kg_in_open(self, lot_id):
        return sum(o["kg"] for o in self.my_offers if o["lot_id"] == lot_id and o["status"] == "OPEN")

    def capacity_left(self):
        return self.capacity - sum(o["kg"] for o in self.my_offers if o["status"] == "OPEN")

    def start_if_waiting(self):
        with self.lock:
            if self.phase == "WAITING":
                self.phase, self.phase_ends = "SETUP", time.time() + self.args.setup_seconds

    def state(self):
        with self.lock:
            self.start_if_waiting()
            lots = []
            for l in self.lots:
                k_open = self.kg_in_open(l["lot_id"])
                lots.append({"lot_id": l["lot_id"], "kg_total": l["kg_total"], "kg_remaining": l["kg_remaining"],
                             "kg_in_open_offers": k_open, "kg_available": l["kg_remaining"] - k_open,
                             "origin": l["origin"], "process": l["process"], "sample": l["sample"]})
            rnd = None
            if self.round_no:
                rnd = {"round_id": f"R{self.round_no}", "kind": "real", "number": self.round_no, "of": self.rounds,
                       "counts_for_score": True, "calendar": self.calendar[self.round_no - 1]["label"]}
            last = self.results.get(f"R{self.round_no - (0 if self.phase in ('CLOSED',) else 1)}") \
                if self.round_no else None
            if self.phase == "CLOSED":
                last = self.results.get(f"R{self.round_no}")
            open_offers = [o for o in self.my_offers if o["status"] == "OPEN"] if self.phase == "ROUND_OPEN" else []
            return {"market": "mock", "team_id": "T01", "phase": self.phase, "paused": False,
                    "seconds_left": None if self.phase in ("CLOSED", "WAITING") else round(max(0.0, self.phase_ends - time.time()), 1),
                    "phase_ends_at": None if self.phase in ("CLOSED", "WAITING") else self.phase_ends,
                    "round": rnd, "lots": lots, "rfqs": self.rfqs if self.phase == "ROUND_OPEN" else [],
                    "my_open_offers": [self._public(o) for o in open_offers],
                    "round_capacity_kg": self.capacity,
                    "capacity_left_kg": self.capacity_left() if self.phase == "ROUND_OPEN" else self.capacity,
                    "appraisal_submitted": self.appraisal is not None,
                    "last_results": last or {"round_id": None, "my_offers": [], "market_summary": []},
                    "standings": self.standings(), "salvage_ratio": RULES["salvage_ratio"]}

    def standings(self):
        total = sum(l["kg_total"] for l in self.lots)
        return {"revenue_thb": round(self.revenue, 2), "kg_sold": self.kg_sold, "kg_remaining": total - self.kg_sold,
                "kg_total": total, "avg_price_per_kg": round(self.revenue / self.kg_sold, 2) if self.kg_sold else None}

    def reveal(self):
        with self.lock:
            if self.phase != "CLOSED":
                raise ApiError(409, "NOT_AVAILABLE", "reveal is only available after the market closes",
                               hint="wait for phase CLOSED")
            tv_total = sum(tv_of(l["true"]) * l["kg_total"] for l in self.lots)
            unsold = sum(tv_of(l["true"]) * l["kg_remaining"] for l in self.lots)
            pr = (self.revenue + RULES["salvage_ratio"] * unsold) / tv_total
            errs = []
            if self.appraisal:
                for l in self.lots:
                    est = self.appraisal.get(l["lot_id"])
                    true = tv_of(l["true"])
                    errs.append(abs(est - true) / true if est is not None else 1.0)
            mean_err = sum(errs) / len(errs) if errs else None
            appraisal_pts = 0 if mean_err is None else max(0.0, 20 * (1 - mean_err / 0.10))
            return {"lots": [{"lot_id": l["lot_id"], "true": l["true"], "sample": l["sample"],
                              "tv": round(tv_of(l["true"]), 2), "your_estimate": (self.appraisal or {}).get(l["lot_id"]),
                              "kg_unsold": l["kg_remaining"]} for l in self.lots],
                    "pr": round(pr, 4), "appraisal_mean_error": None if mean_err is None else round(mean_err, 4),
                    "appraisal_points": round(appraisal_pts, 2), "ceilings": {k: round(v, 3) for k, v in self.ceiling.items()},
                    "standings": self.standings()}

    def rival_prs(self):
        """PR of every rival team, for ranking a finished game."""
        out = {}
        for rv in self.rivals:
            total = sum(tv_of(l["true"]) * l["kg_total"] for l in rv.lots)
            unsold = sum(tv_of(l["true"]) * l["kg_remaining"] for l in rv.lots)
            out[rv.tid] = (rv.kind, (self.rival_revenue[rv.tid] + RULES["salvage_ratio"] * unsold) / total)
        return out

    @staticmethod
    def _public(o):
        return {k: o[k] for k in ("offer_id", "rfq_id", "lot_id", "kg", "price_per_kg", "status")}

    # ------------------------------------------------------------------ actions
    def appraise(self, body):
        with self.lock:
            if self.phase != "APPRAISAL":
                raise ApiError(409, "WRONG_PHASE", f"appraisals are accepted in APPRAISAL, not {self.phase}",
                               allowed={"phase": "APPRAISAL"})
            if self.appraisal is not None:
                raise ApiError(409, "APPRAISAL_ALREADY_SUBMITTED", "appraisal already submitted")
            items = body.get("items")
            if not isinstance(items, list):
                raise ApiError(400, "INVALID_FIELD", "items must be a list", field="items")
            self.appraisal = {i["lot_id"]: float(i["est_value_per_kg"]) for i in items}
            return {"items": items}

    def _check(self, o, cap_left, lot_left):
        if self.phase != "ROUND_OPEN":
            raise ApiError(409, "WRONG_PHASE", f"offers are accepted in ROUND_OPEN, not {self.phase}")
        for f in ("rfq_id", "lot_id", "kg", "price_per_kg"):
            if f not in o:
                raise ApiError(400, "INVALID_FIELD", f"missing {f}", field=f)
        rfq = next((r for r in self.rfqs if r["rfq_id"] == o["rfq_id"]), None)
        if rfq is None:
            raise ApiError(404, "UNKNOWN_RFQ", f"{o['rfq_id']} is not open this round", field="rfq_id")
        lot = next((l for l in self.lots if l["lot_id"] == o["lot_id"]), None)
        if lot is None:
            raise ApiError(404, "NOT_YOUR_LOT" if re.match(r"LOT-T\d+", str(o["lot_id"])) else "UNKNOWN_LOT",
                           f"{o['lot_id']} is not one of your lots", field="lot_id")
        kg = o["kg"]
        if not isinstance(kg, int) or kg <= 0 or kg % BAG:
            raise ApiError(400, "NOT_MULTIPLE_OF_BAG", "kg must be a positive multiple of 60", field="kg",
                           allowed={"nearest": max(BAG, round((kg or 0) / BAG) * BAG) if isinstance(kg, (int, float)) else BAG})
        if kg > rfq["kg"]:
            raise ApiError(400, "INVALID_FIELD", f"kg exceeds the RFQ ({rfq['kg']} kg)", field="kg", allowed={"max_kg": rfq["kg"]})
        if not (0 < float(o["price_per_kg"]) <= 1000):
            raise ApiError(400, "INVALID_FIELD", "price_per_kg must be in (0, 1000]", field="price_per_kg")
        if kg > lot_left[o["lot_id"]]:
            raise ApiError(400, "EXCEEDS_REMAINING", f"{o['lot_id']} has {lot_left[o['lot_id']]} kg available",
                           field="kg", allowed={"max_kg": lot_left[o["lot_id"]]})
        if kg > cap_left:
            raise ApiError(400, "OVER_ROUND_CAPACITY", f"round capacity left is {cap_left} kg", field="kg",
                           allowed={"max_kg": cap_left})

    def _add(self, o):
        rec = {"offer_id": f"O{self.next_offer:05d}", "rfq_id": o["rfq_id"], "lot_id": o["lot_id"], "kg": o["kg"],
               "price_per_kg": round(float(o["price_per_kg"]), 2), "status": "OPEN"}
        self.next_offer += 1
        self.my_offers.append(rec)
        return rec

    def offer(self, o):
        with self.lock:
            lot_left = {l["lot_id"]: l["kg_remaining"] - self.kg_in_open(l["lot_id"]) for l in self.lots}
            self._check(o, self.capacity_left(), lot_left)
            return self._public(self._add(o))

    def offer_batch(self, body):
        with self.lock:
            offers = body.get("offers") or []
            replace = bool(body.get("replace_existing"))
            open_now = [x for x in self.my_offers if x["status"] == "OPEN"]
            cap = self.capacity if replace else self.capacity_left()
            lot_left = {l["lot_id"]: l["kg_remaining"] - (0 if replace else self.kg_in_open(l["lot_id"])) for l in self.lots}
            errors = []
            for i, o in enumerate(offers):
                try:
                    self._check(o, cap, lot_left)
                    cap -= o["kg"]
                    lot_left[o["lot_id"]] -= o["kg"]
                except ApiError as e:
                    errors.append({"index": i, **e.body["error"]})
            if errors:
                raise ApiError(400, "BATCH_REJECTED", f"{len(errors)} offer(s) invalid; nothing was saved",
                               allowed={"errors": errors})
            if replace:
                for x in open_now:
                    x["status"] = "CANCELLED"
            return {"offers": [self._public(self._add(o)) for o in offers]}

    def edit(self, offer_id, body):
        with self.lock:
            o = next((x for x in self.my_offers if x["offer_id"] == offer_id), None)
            if o is None:
                raise ApiError(404, "UNKNOWN_OFFER", f"no offer {offer_id}")
            if o["status"] != "OPEN" or self.phase != "ROUND_OPEN":
                raise ApiError(409, "ROUND_CLOSED", "offer is no longer open")
            if "price_per_kg" in body:
                o["price_per_kg"] = round(float(body["price_per_kg"]), 2)
            if "kg" in body:
                o["kg"] = int(body["kg"])
            return self._public(o)

    def cancel(self, offer_id):
        with self.lock:
            o = next((x for x in self.my_offers if x["offer_id"] == offer_id), None)
            if o is None:
                raise ApiError(404, "UNKNOWN_OFFER", f"no offer {offer_id}")
            o["status"] = "CANCELLED"
            return self._public(o)


def make_handler(market: Market, key: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _route(self, method):
            market.tick()
            url = urlparse(self.path)
            path, q = url.path, parse_qs(url.query)
            try:
                if path == "/v1/version":
                    return self._send(200, {"version": "mock-1", "mode": "mock", "server_time": time.time()})
                if self.headers.get("Authorization") != f"Bearer {key}":
                    raise ApiError(401, "UNAUTHORIZED", "missing or wrong key")
                routes = {
                    ("GET", "/v1/state"): lambda: market.state(),
                    ("GET", "/v1/calendar"): lambda: {"rules": RULES, "calendar": market.calendar},
                    ("GET", "/v1/lots"): lambda: market.state()["lots"],
                    ("GET", "/v1/rfqs"): lambda: market.state()["rfqs"],
                    ("GET", "/v1/standings"): lambda: market.standings(),
                    ("GET", "/v1/appraisals"): lambda: {"items": [{"lot_id": k, "est_value_per_kg": v}
                                                                   for k, v in (market.appraisal or {}).items()]},
                    ("POST", "/v1/appraisals"): lambda: market.appraise(self._body()),
                    ("GET", "/v1/offers"): lambda: {"offers": [market._public(o) for o in market.my_offers]},
                    ("POST", "/v1/offers"): lambda: market.offer(self._body()),
                    ("POST", "/v1/offers:batch"): lambda: market.offer_batch(self._body()),
                    ("POST", "/v1/sandbox/reset"): lambda: (market.reset(), {"ok": True})[1],
                    ("GET", "/v1/sandbox/reveal"): lambda: market.reveal(),
                    ("GET", "/v1/leaderboard"): lambda: {"note": "mock has no leaderboard"},
                }
                if (method, path) in routes:
                    status = 201 if method == "POST" and path.startswith("/v1/offers") else 200
                    return self._send(status, routes[(method, path)]())
                if method == "GET" and path == "/v1/results":
                    rid = (q.get("round") or [None])[0] or f"R{market.round_no - (0 if market.phase == 'CLOSED' else 1)}"
                    if rid not in market.results:
                        raise ApiError(409, "NOT_AVAILABLE", f"no results for {rid} yet")
                    return self._send(200, market.results[rid])
                m = re.fullmatch(r"/v1/offers/(O\d+)", path)
                if m and method == "PUT":
                    return self._send(200, market.edit(m.group(1), self._body()))
                if m and method == "DELETE":
                    return self._send(200, market.cancel(m.group(1)))
                if path.startswith("/v1/"):
                    raise ApiError(404, "UNKNOWN_ENDPOINT", f"no endpoint {method} {path}")
                raise ApiError(404, "UNKNOWN_ENDPOINT", "every path starts with /v1")
            except ApiError as e:
                return self._send(e.status, e.body)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

    return H


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--key", default="sk-T01-mock")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--teams", type=int, default=40, help="sellers in the market, including you")
    p.add_argument("--lots", type=int, default=6)
    p.add_argument("--rounds", type=int, default=RULES["timeline"]["rounds"])
    p.add_argument("--setup-seconds", type=float, default=1)
    p.add_argument("--appraisal-seconds", type=float, default=4)
    p.add_argument("--round-seconds", type=float, default=6)
    p.add_argument("--defect-noise", type=float, default=0.5, help="sd of sample defect_pct vs the true value")
    p.add_argument("--moisture-noise", type=float, default=0.15, help="sd of sample moisture_pct vs the true value")
    p.add_argument("--ceiling-shift", type=float, default=0.0, help="add to every buyer's hidden ceiling (x TV)")
    p.add_argument("--rival-kinds", default="fixed,undercut,adaptive,random", help="comma list cycled over rivals")
    args = p.parse_args()
    RULES["timeline"]["rounds"] = args.rounds
    market = Market(args)

    def clock():
        while True:
            market.tick()
            time.sleep(0.2)

    threading.Thread(target=clock, daemon=True).start()
    print(f"mock market on http://127.0.0.1:{args.port}  key={args.key}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(market, args.key)).serve_forever()


if __name__ == "__main__":
    main()
