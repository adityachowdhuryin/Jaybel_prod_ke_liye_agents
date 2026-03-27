"""Smoke test: stream_query on deployed PA orchestrator Agent Engine (ADC)."""

from __future__ import annotations

import os
import sys
import uuid

import vertexai
import vertexai.agent_engines as agent_engines


def main() -> int:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
    resource = os.environ.get("ORCHESTRATOR_AGENT_ENGINE_RESOURCE", "").strip()
    if not project:
        print("Set GOOGLE_CLOUD_PROJECT.", file=sys.stderr)
        return 2
    if not resource:
        print(
            "Set ORCHESTRATOR_AGENT_ENGINE_RESOURCE to "
            "projects/PROJECT/locations/REGION/reasoningEngines/ID",
            file=sys.stderr,
        )
        return 2

    vertexai.init(project=project, location=location)
    engine = agent_engines.get(resource)
    user_id = f"smoke-{uuid.uuid4().hex[:8]}"
    session = engine.create_session(user_id=user_id)
    session_id = session.get("id") if isinstance(session, dict) else None
    msg = os.environ.get("SMOKE_MESSAGE", "What were our top cloud costs in prod last week?")
    print(f"message={msg!r}\nresource={resource}\n--- stream ---")
    for ev in engine.stream_query(message=msg, user_id=user_id, session_id=session_id):
        print(ev)
    print("--- end ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
