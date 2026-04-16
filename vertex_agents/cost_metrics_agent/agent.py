"""Cost metrics specialist for Vertex AI Agent Engine (Gemini + read-only SQL tools)."""

from __future__ import annotations

import json

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from . import db_logic


def query_cloud_costs(question: str) -> str:
    """Answer questions about cloud spend via BigQuery export or PostgreSQL.

    Pass the user's question in natural language; filters for environment, service,
    date, totals vs detail rows are inferred automatically.
    """
    result = db_logic.query_costs(question)
    try:
        payload = json.loads(result)
    except Exception:
        return result
    if isinstance(payload, dict) and payload.get("needs_clarification"):
        q = str(payload.get("question") or "").strip()
        options = payload.get("options")
        if isinstance(options, list) and options:
            opts = "\n".join(f"- {str(x).strip()}" for x in options if str(x).strip())
            if opts:
                return f"CLARIFICATION_REQUIRED:\n{q}\nOptions:\n{opts}".strip()
        return f"CLARIFICATION_REQUIRED:\n{q}".strip()
    return result


root_agent = LlmAgent(
    name="cost_metrics_agent",
    model="gemini-2.5-flash",
    instruction=(
        "You are a cloud cost analyst. For cost answers, use query_cloud_costs as the source of truth. "
        "Never invent values, services, currencies, date windows, rankings, or trends. "
        "If query_cloud_costs returns CLARIFICATION_REQUIRED, ask exactly that clarification and stop. "
        "If the request is ambiguous (missing time window, scope, grouping, or top-N), ask one concise clarification question instead of guessing. "
        "When tool data exists, summarize faithfully and mention the effective window/filters used. "
        "If the tool returns JSON with an \"error\" field, state that you cannot verify from current data, include detail/hint, and ask one actionable follow-up. "
        "Never expose internal chain-of-thought or fabricate fallback results."
    ),
    tools=[FunctionTool(query_cloud_costs)],
)
