"""Readable terminal output for the person watching the agent. Display only.

Uses the state and decisions the agent already has: no API calls, nothing written to logs, never prints a key.
Every function is wrapped by show(), so a bug here can never stop the agent from selling.
VERBOSE=0 in .env (or the shell) turns it off and leaves the old one-line output.
"""
import os
from urllib.parse import urlparse

BAG = 60
EVENT_IPS = {"161.246.199.191": "WiFi ในงาน", "202.29.227.22": "Public IP"}

_seen_results: set = set()
_last_round_no = {"n": 0}
_was_paused = {"v": False}


def on() -> bool:
    return os.environ.get("VERBOSE", "1") == "1"   # read at call time: .env is loaded after import


def show(fn, *args, **kwargs) -> None:
    if not on():
        return
    try:
        fn(*args, **kwargs)
    except Exception as e:                          # noqa: BLE001 — display must never stop the agent
        print(f"   (console: {e!r})", flush=True)


def _p(text: str = "") -> None:
    print(text, flush=True)


def _host(url: str) -> str:
    u = urlparse(url or "")
    tag = EVENT_IPS.get(u.hostname or "", "")
    return f"{u.hostname}:{u.port}" + (f" ({tag})" if tag else "")


def _short(name: str, width: int = 14) -> str:
    name = str(name or "?")
    return name[:width].ljust(width)


def reset() -> None:
    _seen_results.clear()
    _last_round_no["n"] = 0


def banner(state: dict, tv: dict, calendar_info: dict, llm_on: bool, strategy) -> None:
    reset()
    market_url = os.environ.get("MARKET_URL", "")
    port = urlparse(market_url).port
    mode = "COMPETITION · แข่งจริง" if port == 8001 else "PRACTICE · ตลาดฝึก" if port == 8000 else "MOCK / อื่นๆ"
    _p()
    _p(f"================ {state.get('team_id', '?')} · {mode} (:{port}) ================")
    _p(f"market   {_host(market_url)}   phase {state.get('phase')}")
    if llm_on:
        _p(f"LLM      {os.environ.get('LLM_MODEL', '?')} @ {_host(os.environ.get('LLM_BASE_URL', ''))}  ON  (ล่ม/ช้า → กฎขายแทน)")
    else:
        _p("LLM      OFF → กฎล้วน (USE_LLM=0 หรือ gateway ไม่ถูกต้อง)")
    m, s = strategy.MARKUP, strategy
    _p(f"strategy เริ่ม Spec {m['SPECIALTY']:.2f} · Com {m['COMMERCIAL']:.2f} (≤{s.COMMERCIAL_MAX_MARKUP:.2f})"
       f" · Inst {m['INSTANT']:.2f} (≤{s.INSTANT_MAX_MARKUP:.2f})")
    _p(f"         ราคาเดา ≤{s.UNTESTED_BAGS} กระสอบ · Instant-only ขายก่อน {s.INSTANT_EARLY_FRAC:.0%} แรก"
       f" · ลดได้ ≤{s.MAX_DROP:.2f}/รอบ · step +{s.STEP_UP:.2f}")
    rfqs = state.get("rfqs") or []
    total_tv = 0.0
    for lot in state.get("lots") or []:
        t = tv.get(lot["lot_id"], 0)
        total_tv += t * lot["kg_total"]
        smp = lot.get("sample") or {}
        fits = sorted({r["buyer_type"][:4] for r in rfqs if strategy.spec_ok(smp, r["spec"])})
        _p(f"lot      {lot['lot_id']:<11} TV {t:7.2f} ×{lot['kg_total'] // BAG:<2}"
           f" defect {smp.get('defect_pct')} moist {smp.get('moisture_pct')} screen {smp.get('screen')}"
           + (f"  → {' '.join(fits)}" if fits else ""))
    cal = (calendar_info or {}).get("calendar") or []
    if cal:
        parts = []
        for c in cal:
            w = c.get("demand_weight")
            rr = str(c.get("rounds", c.get("round", "?")))
            parts.append(f"{rr if rr.upper().startswith('R') else 'R' + rr} {c.get('label', '')}" + (f" ×{w}" if w not in (None, 1, 1.0) else ""))
        _p("calendar " + " · ".join(parts))
    _p(f"         TV รวม {total_tv:,.0f} บาท · ซื้อคืนตอนจบ {state.get('salvage_ratio', 0.8):.0%} ของ TV")
    _p("=" * 60)


def appraisal(tv: dict) -> None:
    _p("ส่งผลประเมินแล้ว: " + " · ".join(f"{k[-5:]} {v:.2f}" for k, v in tv.items()))


def paused(state: dict) -> None:
    now = bool(state.get("paused"))
    if now and not _was_paused["v"]:
        _p("!! ตลาดหยุดชั่วคราว (paused) รอผู้จัด ไม่ต้องทำอะไร")
    elif _was_paused["v"] and not now:
        _p("   ตลาดเดินต่อแล้ว")
    _was_paused["v"] = now


def results(state: dict, tv: dict) -> None:
    """Print the last round's results once (they arrive in state.last_results at the next ROUND_OPEN)."""
    r = state.get("last_results") or {}
    rid = r.get("round_id")
    if not rid or rid in _seen_results:
        return
    _seen_results.add(rid)
    ms = {s["rfq_id"]: s for s in r.get("market_summary") or []}
    _p(f"── ผล {rid}{' (ซ้อม ไม่นับ)' if r.get('kind') == 'practice' else ''} ──")
    if not r.get("my_offers"):
        _p("   (เราไม่ได้ส่งรอบนี้)")
    for o in r.get("my_offers") or []:
        s = ms.get(o["rfq_id"], {})
        t = tv.get(o["lot_id"]) or 0
        mk = f"({o['price_per_kg'] / t:.2f})" if t else ""
        sold = f"ตลาดขายได้ {s['price_min']}–{s['price_max']}" if s.get("price_min") is not None else "ตลาดไม่มีใครขายได้"
        mark = "✓" if o["status"] in ("ACCEPTED", "PARTIAL") else "✗"
        _p(f" {mark} {o['rfq_id']:<6} {o['lot_id'][-5:]} → {_short(s.get('buyer'))} {o['status']:<13}"
           f"{(o.get('filled_kg') or 0) // BAG}/{o['kg'] // BAG}b @ {o['price_per_kg']:.2f} {mk:<7} {sold}")
    st = state.get("standings") or {}
    if st:
        left_tv = sum(tv.get(l["lot_id"], 0) * l["kg_remaining"] for l in state.get("lots") or [])
        all_tv = sum(tv.get(l["lot_id"], 0) * l["kg_total"] for l in state.get("lots") or [])
        pr = (st.get("revenue_thb", 0) + state.get("salvage_ratio", 0.8) * left_tv) / all_tv if all_tv else 0
        _p(f"   รวม: รายได้ {st.get('revenue_thb', 0):,.0f} · ขายแล้ว {st.get('kg_sold', 0) // BAG}/{st.get('kg_total', 0) // BAG} กระสอบ"
           f" · PR ถ้าจบตอนนี้ ~{pr:.3f} (TV ที่เราประเมิน)")


def offers(state: dict, offers: list, source: str, seconds: float, notes: list, tv: dict, floor: float | None) -> None:
    rnd = state.get("round") or {}
    n = rnd.get("number") or 0
    if rnd.get("kind") != "practice" and _last_round_no["n"] and n > _last_round_no["n"] + 1:
        _p(f"!! ข้ามรอบ R{_last_round_no['n'] + 1}–R{n - 1} (agent ไม่เห็นรอบนั้นเปิด)")
    if rnd.get("kind") != "practice":
        _last_round_no["n"] = n
    kind = "ซ้อม ไม่นับ" if rnd.get("kind") == "practice" else rnd.get("calendar") or ""
    cap = state.get("capacity_left_kg")
    _p(f"── {rnd.get('round_id')} ({kind}) · เหลือ {state.get('seconds_left') or 0:.0f} วิ"
       f" · เพดาน {cap // BAG if cap is not None else '∞'} กระสอบ · {source} · {seconds:.1f} วิ"
       + (f" · floor {floor:.2f}" if floor else "") + " ──")
    rfqs = {r["rfq_id"]: r for r in state.get("rfqs") or []}
    if not offers:
        _p("   ไม่ส่งรอบนี้ (ไม่มีล็อตที่เข้าสเปก RFQ หรือเก็บไว้รอบหลัง)")
    for o in offers:
        r = rfqs.get(o["rfq_id"], {})
        t = tv.get(o["lot_id"]) or 0
        _p(f"   {o['rfq_id']:<6} {o['lot_id'][-5:]} → {_short(r.get('buyer'))} {str(r.get('buyer_type', '?'))[:4]:<4}"
           f" {o['kg'] // BAG}b @ {o['price_per_kg']:8.2f}" + (f" ({o['price_per_kg'] / t:.2f})" if t else ""))
    dropped = sum(1 for x in notes if str(x).startswith("guard dropped"))
    for x in notes:
        x = str(x)
        if x.startswith("guard:"):
            _p("   · " + x[7:])
        elif x.startswith("llm failed") or x.startswith("no time for the LLM"):
            _p("!! " + x[:140] + " → ใช้กฎแทน")
        elif x.startswith("llm reason:"):
            _p("   LLM: " + x[12:150])
        elif x.startswith("prompt trimmed"):
            _p("   · " + x)
    if dropped:
        _p(f"   · guard ข้าม {dropped} ซองในแผน (ส่วนใหญ่ซ้ำกับซองข้างบน · ที่เหลือเกินเพดานหรือผิดสเปก)")


def sent_count(n_planned: int, n_sent: int) -> None:
    if n_sent < n_planned:
        _p(f"!! ตลาดรับ {n_sent} จาก {n_planned} ซอง (ดู offer_rejected ใน log)")


def closed(state: dict, tv: dict) -> None:
    results(state, tv)
    st = state.get("standings") or {}
    _p("=" * 60)
    _p(f"ตลาดปิด · รายได้ {st.get('revenue_thb', 0):,.0f} บาท · ขาย {st.get('kg_sold', 0) // BAG}/{st.get('kg_total', 0) // BAG} กระสอบ"
       f" · เฉลี่ย {st.get('avg_price_per_kg')} บาท/กก.")
    _p("ถ้าทีมงานเรียก: python pack.py --send ภายใน 10 นาที")
    _p("=" * 60)
