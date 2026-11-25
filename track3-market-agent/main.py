"""Coffee market starter agent: poll the market, appraise once, send offers every round.

    python main.py              # play until the market closes
    python main.py --forever    # sandbox: keep playing game after game
"""
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import console
import strategy
from appraisal import appraisal_items, estimate_lots
from llm import LLM
from market import Market, MarketError
from memory import Memory

HERE = Path(__file__).parent
LOGS = HERE / "logs"


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


GAME_DIR: Path | None = None   # logs/games/<Practice-01 | Competition-Day18 | Mock-01>/ for the game being played


def game_folder(market_url: str, market: str, tag: str) -> Path:
    """One folder per game for analysis: Practice-01, Practice-02 ... (port 8000), Competition-Day18 (port 8001),
    Mock-01 ... (anything else). A restart inside the same game (same lots) goes back to the same folder."""
    from urllib.parse import urlparse
    root = LOGS / "games"
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        index = {}
    key = f"{market}-{tag}"
    if key not in index:
        port = urlparse(market_url).port
        if port == 8001:
            base = f"Competition-Day{datetime.now():%d}"
            name, n = base, 2
            while name in index.values():          # a second competition game on the same day
                name, n = f"{base}-{n}", n + 1
        else:
            prefix = "Practice" if port == 8000 else "Mock"
            n = 1 + sum(1 for v in index.values() if v.startswith(prefix + "-"))
            name = f"{prefix}-{n:02d}"
        index[key] = name
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    folder = root / index[key]
    folder.mkdir(exist_ok=True)
    return folder


def log(event: str, **data) -> None:
    LOGS.mkdir(exist_ok=True)
    row = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with open(LOGS / "decisions.jsonl", "a", encoding="utf-8") as f:   # all games, as the organizer expects
        f.write(line)
    if GAME_DIR is not None:
        try:
            with open(GAME_DIR / "decisions.jsonl", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
    if console.on() and event in ("new_game", "appraisal_sent", "offers_sent", "market_closed"):
        return                                      # console.py shows these in readable form; the file log above is unchanged
    short = {k: v for k, v in data.items() if k not in ("offers", "notes")}
    print(row["time"][11:], event, json.dumps(short, ensure_ascii=False)[:160], flush=True)


def write_status(state: dict, tv: dict, llm_on: bool) -> None:
    """Snapshot for dashboard.py: the last market state plus our TV estimates. Read-only, never sends anything."""
    try:
        LOGS.mkdir(exist_ok=True)
        row = {"time": datetime.now().isoformat(timespec="seconds"), "llm": llm_on, "tv": tv,
               **{k: state.get(k) for k in ("market", "team_id", "phase", "paused", "seconds_left", "round", "lots",
                                            "rfqs", "my_open_offers", "capacity_left_kg", "round_capacity_kg",
                                            "appraisal_submitted", "last_results", "standings", "salvage_ratio")}}
        tmp = LOGS / "status.json.tmp"
        tmp.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        tmp.replace(LOGS / "status.json")
    except OSError:
        pass                                        # the dashboard is optional; never stop the agent for it


_history_seen: set = set()


def record_history(state: dict, tv: dict, tag: str, calendar: dict | None = None) -> None:
    """Keep every market phase for analysis after the game: one full state per (phase, round), appended to
    logs/games/<game>/history.jsonl. Uses the state the agent already read, so no extra API calls."""
    rnd = (state.get("round") or {}).get("round_id")
    key = (tag, state.get("phase"), rnd)
    if key in _history_seen:
        return
    _history_seen.add(key)
    try:
        LOGS.mkdir(exist_ok=True)
        row = {"time": datetime.now().isoformat(timespec="seconds"), "tv": tv, "state": state}
        if calendar is not None:
            row["calendar"] = calendar
        with open((GAME_DIR or LOGS) / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass                                        # analysis only; never stop the agent for it


def record_llm(call: dict) -> None:
    """Every LLM call (prompt, raw reply, parsed answer, error, seconds) -> logs/games/<game>/llm.jsonl."""
    try:
        row = {"time": datetime.now().isoformat(timespec="seconds"), **call}
        with open((GAME_DIR or LOGS) / "llm.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


_backfill_tried: set = set()


def backfill(api: Market, memory, state: dict) -> None:
    """See-It (TGR-23): if the agent missed a round (fast market, restart, network drop), its results never came
    through state.last_results. Fetch each missing real round once with GET /v1/results?round=Rn."""
    rnd = state.get("round") or {}
    if rnd.get("kind") == "practice" or not rnd.get("number"):
        return
    have = {r.get("round_id") for r in memory.rounds}
    for n in range(1, rnd["number"]):
        rid = f"R{n}"
        if rid in have or rid in _backfill_tried:
            continue
        _backfill_tried.add(rid)
        try:
            res = api.results(rid)
            memory.record_results(res)
            log("backfill", round=rid, offers=len(res.get("my_offers") or []))
        except Exception as e:                      # noqa: BLE001 — never stop selling for this
            log("backfill_error", round=rid, error=str(e)[:120])


def send_offers(api: Market, offers: list[dict]) -> list[dict]:
    """Send all offers at once; if the batch is rejected, send them one by one and skip the bad ones."""
    try:
        return api.offer_batch(offers)["offers"]
    except MarketError as e:
        log("batch_rejected", error=str(e))
    sent = []
    for o in offers:
        try:
            sent.append(api.offer(o))
        except MarketError as e:
            log("offer_rejected", offer=o, error=str(e))
    return sent


def play(api: Market, llm: LLM | None, forever: bool) -> None:
    global GAME_DIR
    while True:                                     # started before the market opens: wait instead of crashing
        try:
            info = api.calendar()
            break
        except Exception as e:                      # noqa: BLE001
            log("waiting_for_market", error=str(e)[:120])
            time.sleep(10)
    rules = info["rules"]
    memory, tv, acted, game = None, {}, set(), None
    rng = random.Random()
    while True:
        try:
            state = api.state()
        except Exception as e:                      # noqa: BLE001 — never die on a network error
            log("state_error", error=str(e))
            time.sleep(3)
            continue
        # the sandbox reuses lot ids every game, so the samples are what tell one game from the next
        lots_key = tuple(sorted((l["lot_id"], json.dumps(l["sample"], sort_keys=True)) for l in state["lots"]))
        if lots_key != game:                        # new game (or sandbox reset): start fresh
            game, acted, tv = lots_key, set(), estimate_lots(rules, state["lots"])
            _backfill_tried.clear()
            tag = hashlib.sha1(repr(lots_key).encode()).hexdigest()[:8]
            GAME_DIR = game_folder(os.environ["MARKET_URL"], state["market"], tag)
            memory = Memory(GAME_DIR / "memory.json")
            # the practice market can hand out the same lots again: a memory that already holds real rounds while this
            # market has not reached a real round yet belongs to an older game -> keep it aside and start empty
            rnd_now = state.get("round") or {}
            if any(r.get("kind") != "practice" for r in memory.rounds) and \
                    (state["phase"] in ("WAITING", "SETUP", "APPRAISAL") or rnd_now.get("kind") == "practice"):
                old = GAME_DIR / f"memory-old-{datetime.now():%m%d-%H%M%S}.json"
                (GAME_DIR / "memory.json").rename(old)
                memory = Memory(GAME_DIR / "memory.json")
                log("memory_reset", reason="same lots as an older game", kept=old.name)
            log("new_game", market=state["market"], lots=len(state["lots"]), tv=tv)
            console.show(console.banner, state, tv, info, llm is not None, strategy)
            record_history(state, tv, tag, calendar=info)
        phase = state["phase"]
        write_status(state, tv, llm is not None)
        console.show(console.paused, state)
        record_history(state, tv, tag)

        if phase == "APPRAISAL" and not state["appraisal_submitted"]:
            try:
                api.appraise(appraisal_items(rules, state["lots"], tv))
                log("appraisal_sent", tv=tv)
                console.show(console.appraisal, tv)
            except MarketError as e:
                log("appraisal_error", error=str(e))

        elif phase == "ROUND_OPEN" and not state["paused"] and state["round"]["round_id"] not in acted:
            rid = state["round"]["round_id"]
            acted.add(rid)
            memory.record_results(state["last_results"])
            backfill(api, memory, state)
            console.show(console.results, state, tv)
            if any(o.get("round_id", rid) == rid for o in state.get("my_open_offers") or []):   # open offers can only be this round's
                # restarted after sending this round: the market already holds our offers, and a new batch
                # would replace them (capacity_left_kg no longer counts them, so it would offer less)
                if os.environ.get("RESEND_ON_RESTART", "1") != "1":
                    log("round_already_sent", round=rid, n=len(state["my_open_offers"]))
                    continue
                # 20 Nov race (user OK): decide again with the new settings; the new batch replaces the old one,
                # so plan with the full round capacity. If the market rejects it, the old offers stay.
                log("round_resend", round=rid, n=len(state["my_open_offers"]))
                state = dict(state, my_open_offers=[],
                             capacity_left_kg=state.get("round_capacity_kg") or state.get("capacity_left_kg"))
            ctx = strategy.Context(rules, info["calendar"], tv, rng, memory, llm, state["seconds_left"] or 999)
            t0 = time.time()
            try:
                offers, source = strategy.decide(state, ctx)
            except Exception as e:                  # noqa: BLE001 — a bug in strategy must not stop the agent
                log("strategy_crashed", error=repr(e))
                offers, source = strategy.guard(state, ctx, strategy.rule_plan(state, ctx, strategy.candidates(state, ctx))), "rules"
            sent = send_offers(api, offers) if offers else []
            memory.record_sent(rid, offers, state, tv)
            log("offers_sent", round=rid, source=source, n=len(sent), kg=sum(o["kg"] for o in sent),
                seconds=round(time.time() - t0, 1), offers=offers, notes=ctx.notes)
            console.show(console.offers, state, offers, source, time.time() - t0, ctx.notes, tv, getattr(ctx, "floor", None))
            console.show(console.sent_count, len(offers), len(sent))

        elif phase == "CLOSED" and game is not None and "closed" not in acted:
            acted.add("closed")
            memory.record_results(state["last_results"])
            log("market_closed", standings=state["standings"])
            console.show(console.closed, state, tv)
            if not forever:
                return

        # Read the clock from the server; never hard-code round times.
        left = state["seconds_left"] or 5
        time.sleep(min(5.0, max(0.5, left + 0.2)))


def llm_allowed(market_url: str, llm_url: str) -> bool:
    """Day-18 rules (slide 28): on the competition market (:8001) only the event gateway may be used.
    A local model (Ollama, Qwen) or cloud LLM there gets the team disqualified. Practice markets may use anything."""
    from urllib.parse import urlparse
    m, g = urlparse(market_url), urlparse(llm_url)
    if m.port != 8001:
        return True
    event_hosts = {m.hostname, "161.246.199.191", "202.29.227.22"}   # event WiFi and public IP are the same server
    return g.hostname in event_hosts and g.port == 4000


def main() -> None:
    load_env(HERE / ".env")
    api = Market(os.environ["MARKET_URL"], os.environ["TEAM_KEY"])
    llm = None
    if os.environ.get("USE_LLM", "1") == "1" and not llm_allowed(os.environ["MARKET_URL"], os.environ.get("LLM_BASE_URL", "")):
        print("!! competition market but LLM_BASE_URL is not the event gateway (<market host>:4000)."
              " Rules say any other LLM (cloud or local Ollama/Qwen) = disqualified. LLM OFF, playing with rules only.",
              flush=True)
        os.environ["USE_LLM"] = "0"
    if os.environ.get("USE_LLM", "1") == "1":
        llm = LLM(os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ.get("LLM_API_KEY", ""),
                  timeout=float(os.environ.get("LLM_TIMEOUT", "60")), thinking=os.environ.get("LLM_THINKING") == "1")
        llm.on_call = record_llm
    play(api, llm, "--forever" in sys.argv)


if __name__ == "__main__":
    main()
