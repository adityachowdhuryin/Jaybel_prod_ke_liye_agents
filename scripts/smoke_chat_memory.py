#!/usr/bin/env python3
"""
Exercise orchestrator chat + Postgres session: multi-turn + optional simulated restart.
Usage:
  python scripts/smoke_chat_memory.py              # two turns only
  python scripts/smoke_chat_memory.py --restart   # kill uvicorn, start-all.sh, third turn
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import httpx

BASE = os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8000")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def parse_sse_text(raw: bytes) -> str:
    text_parts: list[str] = []
    s = raw.decode("utf-8", errors="replace")
    while "\n\n" in s:
        ev, s = s.split("\n\n", 1)
        line = next((ln for ln in ev.split("\n") if ln.startswith("data:")), None)
        if not line:
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if obj.get("error"):
            return f"[ERROR] {obj.get('detail', obj)}"
        status = obj.get("status") if isinstance(obj.get("status"), dict) else {}
        message = status.get("message") if isinstance(status.get("message"), dict) else {}
        artifact = obj.get("artifact") if isinstance(obj.get("artifact"), dict) else {}
        parts = message.get("parts") or artifact.get("parts") or []
        if isinstance(parts, list):
            for p in parts:
                if isinstance(p, dict) and p.get("text"):
                    text_parts.append(str(p["text"]))
    return "".join(text_parts)


def chat(message: str, session_id: str | None) -> tuple[str, str | None]:
    body: dict = {"message": message}
    if session_id:
        body["session_id"] = session_id
    out = bytearray()
    new_sid = session_id
    with httpx.Client(timeout=600.0) as client:
        with client.stream(
            "POST",
            f"{BASE}/chat/stream",
            json=body,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        ) as r:
            r.raise_for_status()
            new_sid = r.headers.get("x-session-id") or new_sid
            for chunk in r.iter_bytes():
                out.extend(chunk)
    return parse_sse_text(bytes(out)), new_sid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--restart",
        action="store_true",
        help="After turn 2: pkill uvicorn, run start-all.sh, send turn 3",
    )
    args = ap.parse_args()

    sid: str | None = None
    m1 = "What was our Cloud Build spend in jaybel-dev yesterday?"
    print("--- Turn 1 ---", flush=True)
    r1, sid = chat(m1, sid)
    print("session_id:", sid, flush=True)
    print("reply_preview:", (r1[:600] + "…") if len(r1) > 600 else r1, flush=True)

    m2 = "And what about Artifact Registry?"
    print("--- Turn 2 ---", flush=True)
    r2, sid = chat(m2, sid)
    print("reply_preview:", (r2[:600] + "…") if len(r2) > 600 else r2, flush=True)
    assert sid, "missing session id"

    if args.restart:
        print("--- Simulated crash: killing uvicorn + next ---", flush=True)
        subprocess.run(["pkill", "-f", "uvicorn main:app"], check=False)
        subprocess.run(["pkill", "-f", "next dev"], check=False)
        time.sleep(3)
        print("--- start-all.sh ---", flush=True)
        subprocess.run(["bash", "scripts/start-all.sh"], cwd=ROOT, check=True)
        time.sleep(8)

        m3 = "What about Cloud Run?"
        print("--- Turn 3 (after restart, same session_id) ---", flush=True)
        r3, sid3 = chat(m3, sid)
        print("session_id after:", sid3, flush=True)
        print("reply_preview:", (r3[:700] + "…") if len(r3) > 700 else r3, flush=True)
        if r3.startswith("[ERROR]"):
            print("FAIL: error after restart", file=sys.stderr)
            return 1
        low = r3.lower()
        if "cloud run" in low or "run" in low or "inr" in low or "cost" in low:
            print("OK: third reply looks on-task (heuristic).", flush=True)
        else:
            print(
                "WARN: third reply may be weak; inspect full text and BigQuery data.",
                flush=True,
            )

    print("Done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
