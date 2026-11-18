"""ที่ปรึกษาสำหรับขายด้วยมือ (ช่วงแข่ง 1): อ่านตลาด แล้วพิมพ์ TV และราคาที่ควรเสนอ — ไม่ส่งคำสั่งใด ๆ

    python3 advise.py            # อัปเดตหน้าจอทุก 3 วินาที จนตลาดปิด (Ctrl+C เพื่อออก)
    python3 advise.py --once     # พิมพ์ครั้งเดียวแล้วจบ

ใช้ GET /v1/state และ GET /v1/calendar เท่านั้น คนเป็นคนกดส่งบนหน้าเว็บเอง
ราคาที่แนะนำมาจากตรรกะเดียวกับ agent (strategy.py): markup ต่อผู้ซื้อที่เรียนจากผลรอบก่อน × TV
การเรียนรู้ใช้ราคาที่ทีมส่งจริง (จาก last_results) ไม่ใช่ราคาที่แนะนำ จึงถูกต้องแม้คนกดราคาอื่น
"""
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import strategy
from appraisal import estimate_lots, grade
from main import load_env
from market import Market

HERE = Path(__file__).parent
LOGS = HERE / "logs"


def remember(memory: dict, results: dict | None, tv: dict) -> None:
    """เก็บผลรอบที่ปิดแล้ว พร้อม markup ที่ทีมใช้จริง = ราคาที่ส่ง ÷ TV ของเรา"""
    if not results or not results.get("round_id") or any(r["round_id"] == results["round_id"] for r in memory["rounds"]):
        return
    memory["rounds"].append(results)
    memory["sent"][results["round_id"]] = [
        {"rfq_id": o["rfq_id"], "lot_id": o["lot_id"], "markup": round(o["price_per_kg"] / tv[o["lot_id"]], 3)}
        for o in results.get("my_offers", []) if tv.get(o["lot_id"])]


class Mem:
    """หน้าตาเดียวกับ memory.Memory พอให้ strategy.learned_markups ใช้ได้"""
    def __init__(self, d):
        self.rounds, self.sent = d["rounds"], d["sent"]


def fmt(x, w=8, p=2):
    return f"{x:>{w}.{p}f}" if isinstance(x, (int, float)) else f"{str(x):>{w}}"


def render(state: dict, info: dict, tv: dict, memory: dict) -> list[str]:
    rules = info["rules"]
    salvage = state.get("salvage_ratio") or rules.get("salvage_ratio", 0.8)
    rnd = state.get("round") or {}
    out = [f"{datetime.now():%H:%M:%S}  ตลาด {state.get('market')}  ทีม {state.get('team_id')}  "
           f"ช่วง {state['phase']}{' (หยุดชั่วคราว)' if state.get('paused') else ''}  "
           f"เหลือ {state.get('seconds_left') or 0:.0f} วิ  "
           f"รอบ {rnd.get('round_id', '-')}/{rnd.get('of', '-')} {rnd.get('calendar') or ''}", ""]

    out.append("ล็อตของเรา (TV จากสูตรใน /v1/calendar)")
    out.append(f"  {'lot':<14}{'เกรด':<12}{'TV/kg':>8}{'ห้ามต่ำกว่า':>12}{'เหลือขาย kg':>13}  sample")
    for lot in state["lots"]:
        s = lot["sample"]
        out.append(f"  {lot['lot_id']:<14}{grade(rules, s['defect_pct'])[0]:<12}{fmt(tv[lot['lot_id']])}"
                   f"{fmt(salvage * tv[lot['lot_id']], 12)}{lot['kg_available']:>13}  "
                   f"ตำหนิ {s['defect_pct']}% ชื้น {s['moisture_pct']}% ตะแกรง {s['screen']}")
    out.append(f"  ห้ามต่ำกว่า = {salvage} × TV: ไม่ขายเลยยังได้เท่านี้ตอนจบ")
    if state["phase"] == "APPRAISAL":
        out.append("")
        out.append("  >>> ช่วงประเมิน: กรอก est_value_per_kg = TV/kg และเกรดตามตารางบน (ส่งได้ครั้งเดียว ตรวจก่อนกด)"
                   + ("  [ส่งแล้ว]" if state.get("appraisal_submitted") else "  [ยังไม่ส่ง]"))

    mem = Mem(memory)
    markups = strategy.learned_markups(mem)
    rt = strategy.rival_tracker(state, mem)
    out.append("")
    out.append(f"เรดาร์คู่แข่ง ({rt['rivals_count']} ทีม จากทั้งหมด {rt['teams']} ทีม): "
               f"ขายไปแล้ว {rt['rivals_sold_kg']:,}/{rt['rivals_initial_kg']:,} kg ({rt['depletion']*100:.1f}%)  "
               f"เหลือเฉลี่ยทีมละ {rt['avg_bags_left_per_rival']} กระสอบ  "
               f"(เทียบเท่าคู่แข่งเต็มโควตา {rt['effective_rivals']} ทีม)")
    if rt["last_wanted_kg"] > 0:
        out.append(f"  รอบก่อนตลาดรับซื้อ {rt['last_bought_kg']:,}/{rt['last_wanted_kg']:,} kg "
                   f"(Fill {rt['last_fill_rate']*100:.0f}%)"
                   + (f"  ผู้ซื้อได้ของไม่ครบ: {', '.join(rt['unfilled_buyers'])}"
                      if rt["unfilled_buyers"] else ""))
    if rt["unfilled_buyers"] and not rt["squeeze"]:
        # tested 17 Nov: with 56 teams rivals still hold most of their stock to the last round, so an unfilled RFQ
        # usually means offers were above that buyer's ceiling or failed its spec, not that rivals ran out
        out.append("    คู่แข่งยังมีของเหลือมาก: ไม่ครบเพราะเสนอแพงเกินเพดานหรือไม่ผ่านสเปก ไม่ใช่ขาดของ "
                   "-> ดันราคาเฉพาะผู้ซื้อที่เรารับได้ (ACCEPTED) รอบก่อน และดูราคาสูงสุดที่ขายได้ในตารางผลก่อน")
    if rt["squeeze"]:
        out.append("  >>> [SQUEEZE] ของที่คู่แข่งทั้งตลาดเหลือ น้อยกว่าความต้องการรอบนี้! ดันราคาขึ้นใกล้เพดานได้เลย")
    out.append("markup ที่เรียนได้ (ราคา ÷ TV): " + ", ".join(f"{k} {v:.2f}" for k, v in sorted(markups.items())))

    last = state.get("last_results") or {}
    if last.get("my_offers") or last.get("market_summary"):
        out.append("")
        out.append(f"ผลรอบ {last.get('round_id')}")
        for o in last.get("my_offers", []):
            t = tv.get(o["lot_id"])
            ratio = f" (x{o['price_per_kg'] / t:.2f})" if t else ""
            out.append(f"  เรา {o['rfq_id']:<8}{o['lot_id']:<14}{o['kg']:>5} kg @{fmt(o['price_per_kg'])}{ratio}"
                       f"  {o['status']}  ได้ {o.get('filled_kg', 0)} kg")
        for r in last.get("market_summary", []):
            out.append(f"  ตลาด {r['rfq_id']:<8}{r['buyer'][:18]:<19}{r['buyer_type']:<11}"
                       f"ซื้อ {r.get('bought_kg')}/{r.get('wanted_kg')} kg  ข้อเสนอ {r.get('offers')}  "
                       f"ราคา {r.get('price_min')}–{r.get('price_max')} เฉลี่ย {r.get('price_avg')}")

    if state["phase"] == "ROUND_OPEN" and not state.get("paused"):
        seed = int(hashlib.sha1(str(rnd.get("round_id")).encode()).hexdigest()[:8], 16)
        ctx = strategy.Context(rules, info.get("calendar", []), tv, random.Random(seed), mem, None,
                               state.get("seconds_left") or 999)
        ctx.markups = markups
        ctx.floor = strategy.markup_floor(state, ctx)
        offers = strategy.guard(state, ctx, strategy.rule_plan(state, ctx, strategy.candidates(state, ctx)))
        rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
        out.append("")
        out.append(f"แนะนำให้เสนอรอบนี้ (ความจุเหลือ {state.get('capacity_left_kg')} kg) — กดเองบนหน้าเว็บ")
        out.append(f"  {'RFQ':<9}{'ผู้ซื้อ':<20}{'ประเภท':<12}{'lot':<14}{'kg':>6}{'ราคา/kg':>10}{'x TV':>7}")
        for o in offers:
            r = rfqs[o["rfq_id"]]
            out.append(f"  {o['rfq_id']:<9}{r['buyer'][:19]:<20}{r['buyer_type']:<12}{o['lot_id']:<14}"
                       f"{o['kg']:>6}{fmt(o['price_per_kg'], 10)}{o['price_per_kg'] / tv[o['lot_id']]:>7.2f}")
        if not offers:
            out.append("  (ไม่มี: ล็อตไม่ผ่านสเปก RFQ ไหนเลย หรือไม่มีของเหลือ/ความจุ)")
        if state.get("my_open_offers"):
            out.append(f"  ข้อเสนอที่เปิดอยู่แล้ว {len(state['my_open_offers'])} รายการ (ความจุด้านบนหักไปแล้ว)")
        skipped = [r for r in state["rfqs"] if not any(strategy.spec_ok(l["sample"], r["spec"]) for l in state["lots"])]
        if skipped:
            out.append("  RFQ ที่ไม่มีล็อตไหนผ่านสเปก (อย่าเสนอ จะได้ SPEC_MISMATCH): "
                       + ", ".join(r["rfq_id"] for r in skipped))
    out.append("")
    st = state.get("standings") or {}
    out.append(f"ยอดรวม: รายได้ {st.get('revenue_thb')} บาท  ขายแล้ว {st.get('kg_sold')}/{st.get('kg_total')} kg  "
               f"ราคาเฉลี่ย {st.get('avg_price_per_kg')}")
    return out


def main() -> None:
    load_env(HERE / ".env")
    if not os.environ.get("MARKET_URL") or not os.environ.get("TEAM_KEY"):
        sys.exit("ยังไม่มี MARKET_URL / TEAM_KEY: สร้าง .env ด้วย `cp .env.example .env` แล้วใส่ค่าที่ผู้จัดแจก\n"
                 "ลองกับตลาดจำลอง: MARKET_URL=http://127.0.0.1:8765 TEAM_KEY=sk-T01-mock python3 advise.py")
    api = Market(os.environ["MARKET_URL"], os.environ["TEAM_KEY"])
    info = api.calendar()
    once = "--once" in sys.argv
    game, tv, memory, mem_path = None, {}, None, None
    while True:
        try:
            state = api.state()
        except Exception as e:                      # noqa: BLE001 — keep advising through network blips
            print("อ่านตลาดไม่ได้:", e, flush=True)
            time.sleep(3)
            continue
        key = tuple(sorted((l["lot_id"], json.dumps(l["sample"], sort_keys=True)) for l in state["lots"]))
        if key != game:
            game, tv = key, estimate_lots(info["rules"], state["lots"])
            tag = hashlib.sha1(repr(key).encode()).hexdigest()[:8]
            mem_path = LOGS / f"advise-{state['market']}-{tag}.json"
            memory = json.loads(mem_path.read_text(encoding="utf-8")) if mem_path.exists() else {"rounds": [], "sent": {}}
        n = len(memory["rounds"])
        remember(memory, state.get("last_results"), tv)
        if len(memory["rounds"]) != n:
            LOGS.mkdir(exist_ok=True)
            mem_path.write_text(json.dumps(memory, ensure_ascii=False, indent=1), encoding="utf-8")
        lines = render(state, info, tv, memory)
        if not once:
            print("\033[2J\033[H", end="")
        print("\n".join(lines), flush=True)
        if once or state["phase"] == "CLOSED":
            return
        time.sleep(3)


if __name__ == "__main__":
    main()
