"""agent_core — shared infrastructure for every agent-task variant.

Holds the cross-cutting pieces every variant needs:
- `schemas`: Pydantic types for the universal output contract
  (Evidence, Remediation, Improvement, Findings) plus observability
  (ToolMetrics, Metrics, AgentResult) plus sub-agent output
  (LogAnalysis).
- `tools`: the K8s API tool surface (`kubectl_read`, `kubectl_list`,
  `kubectl_get_container_logs`, `emit_findings`) + the parametric
  executor (`execute_tool`) + filter helpers + K8s client setup.
- `log_triage`: the Haiku log-triage sub-agent that wraps large log
  fetches into a structured LogAnalysis.

Variants live as siblings under `src/`:
- `pod_launch_task/` — the first variant (this codebase shipped with
  it in PR-8).
- Future variants (`intra_cluster_traffic_task/`, `hpa_latency_task/`,
  ...) will import from `agent_core` the same way `pod_launch_task`
  does today.

Variants own:
- `prompts.py` — variant-specific diagnostic playbooks.
- `agent.py` — the variant entry point that wires prompt + tools +
  bootstrap into the agent loop body.
- `main.py` — the CLI entry.

When a future PR generalizes the agent loop body (currently in
`pod_launch_task/agent.py`) into `agent_core/loop.py`, only the wiring
in each variant's `main.py` needs to change.
"""
