# ADK `*_tmp*` directories under `vertex_agents/`

During `adk deploy agent_engine`, the CLI may create folders such as
`cost_metrics_agent_tmp20…` or `pa_orchestrator_agent_tmp20…`. These are **staging
copies**, not the source of truth.

They are **gitignored** on purpose: duplicating the whole agent tree bloats the
repository and can contain stale files. Always edit and version **`cost_metrics_agent/`**
and **`pa_orchestrator_agent/`** only.
