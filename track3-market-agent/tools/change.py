"""Record a strategy change made during the game, so the judges can trace every offer back to the code that made it.

    python tools/change.py "Instant filled 100% two rounds, rivals undercut: spread 1 bag per RFQ (playbook M1)"

Appends one line to logs/changes.jsonl (time, the round shown in logs/status.json, reason, active strategy.env
lines, changed files) and saves the code diff to logs/changes/NNN.diff. Both go into the hand-in zip with logs/.
Never reads or copies .env (it holds the team key).
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LOGS = HERE / "logs"


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        sys.exit('usage: python tools/change.py "what changed and why"')
    reason = " ".join(sys.argv[1:]).strip()
    LOGS.mkdir(exist_ok=True)
    (LOGS / "changes").mkdir(exist_ok=True)
    n = len(list((LOGS / "changes").glob("*.diff"))) + 1
    env_lines = []
    se = HERE / "strategy.env"
    if se.exists():
        env_lines = [l.strip() for l in se.read_text(encoding="utf-8").splitlines() if l.strip().startswith("STRATEGY_")]
    diff = ""
    try:   # code changes since the last commit, tracked .py files only (never .env)
        diff = subprocess.run(["git", "diff", "--", "*.py", "strategy.env"], cwd=HERE, capture_output=True, text=True).stdout
        files = subprocess.run(["git", "diff", "--name-only", "--", "*.py", "strategy.env"], cwd=HERE,
                               capture_output=True, text=True).stdout.split()
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True, text=True).stdout.strip()
    except OSError:
        files, head = [], ""
    (LOGS / "changes" / f"{n:03d}.diff").write_text(diff, encoding="utf-8")
    rnd = None
    try:
        st = json.loads((LOGS / "status.json").read_text(encoding="utf-8"))
        rnd = (st.get("round") or {}).get("round_id")
        phase = st.get("phase")
    except (OSError, json.JSONDecodeError):
        phase = None
    rec = {"time": datetime.now().isoformat(timespec="seconds"), "change": n, "round": rnd, "phase": phase,
           "reason": reason, "strategy_env": env_lines, "files": files, "base_commit": head,
           "diff": f"logs/changes/{n:03d}.diff"}
    with open(LOGS / "changes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"change {n} recorded at {rec['time']} (round {rnd}, phase {phase})")
    print(f"  active strategy.env: {', '.join(env_lines) or '(none)'}")
    print(f"  files vs commit {head}: {', '.join(files) or '(none)'} → logs/changes/{n:03d}.diff")


if __name__ == "__main__":
    main()
