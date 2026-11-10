"""Read-only live dashboard for the agent. Open it from a phone or another laptop while the agent runs.

    python dashboard.py                 # http://<this laptop's IP>:8080 on the same network
    python dashboard.py --port 9000

It only reads files the agent writes in logs/ (status.json, decisions.jsonl, memory-*.json).
It never calls the market or the LLM and has no buttons, so it cannot send or change an offer.
Other teams share the competition network, so the page needs a random token in the URL (printed at start).
"""
import argparse
import json
import secrets
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import strategy
from memory import Memory

HERE = Path(__file__).parent
LOGS = HERE / "logs"
PAGE = HERE / "dashboard.html"
TOKEN = ""                                          # set in main(); empty = no check (--no-token)


def tail_jsonl(path: Path, n: int) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def latest_memory() -> Memory | None:
    files = sorted(LOGS.glob("memory-*.json"), key=lambda p: p.stat().st_mtime)
    return Memory(str(files[-1])) if files else None


def estimated_pr(status: dict) -> float | None:
    """PR = (revenue + salvage x TV of unsold) / TV of all, with our own TV estimates (the true TV is hidden)."""
    tv, lots = status.get("tv") or {}, status.get("lots") or []
    total = sum(tv.get(l["lot_id"], 0) * l["kg_total"] for l in lots)
    if not total:
        return None
    unsold = sum(tv.get(l["lot_id"], 0) * l["kg_remaining"] for l in lots)
    revenue = (status.get("standings") or {}).get("revenue_thb") or 0
    return round((revenue + (status.get("salvage_ratio") or 0.8) * unsold) / total, 4)


def snapshot() -> dict:
    status_file = LOGS / "status.json"
    status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    memory = latest_memory()
    events = tail_jsonl(LOGS / "decisions.jsonl", 60)
    alerts = [e for e in events if e.get("event") in ("strategy_crashed", "batch_rejected", "offer_rejected",
                                                       "appraisal_error", "state_error")]
    llm_fails = [e for e in events if e.get("event") == "offers_sent"
                 and any("llm failed" in str(n) for n in e.get("notes") or [])]
    rounds = memory.rounds if memory else []
    return {
        "status": status,
        "estimated_pr": estimated_pr(status) if status else None,
        "markups": strategy.learned_markups(memory) if memory else strategy.MARKUP,
        "rounds": rounds[-8:],
        "sent": memory.sent if memory else {},
        "events": events[-25:],
        "alerts": alerts[-10:],
        "llm_failures": len(llm_fails),
        "status_age_s": round(time.time() - status_file.stat().st_mtime, 1) if status_file.exists() else None,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if TOKEN and parse_qs(url.query).get("t", [""])[0] != TOKEN:
            return self._send(403, "ต้องเปิดด้วยลิงก์ที่มี ?t=... ตามที่ dashboard.py พิมพ์ไว้".encode(), "text/plain; charset=utf-8")
        if url.path == "/api/status":
            try:
                body = json.dumps(snapshot(), ensure_ascii=False).encode()
                return self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:                  # noqa: BLE001 — a half-written log must not kill the page
                return self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        self._send(404, b"not found", "text/plain")


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from a phone on the same network")
    p.add_argument("--token", default=None, help="fixed token instead of a random one")
    p.add_argument("--no-token", action="store_true", help="no token check (only on a private network)")
    a = p.parse_args()
    global TOKEN
    TOKEN = "" if a.no_token else (a.token or secrets.token_urlsafe(8))
    q = f"/?t={TOKEN}" if TOKEN else "/"
    print(f"dashboard: http://127.0.0.1:{a.port}{q}  ·  same network: http://{lan_ip()}:{a.port}{q}", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
