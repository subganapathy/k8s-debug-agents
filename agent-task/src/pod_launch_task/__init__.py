"""Pod-launch diagnostic task — one variant of the agent-task family.

Phase 1 / PR #8 — runs as a standalone Python process. No CRDs, no K8s
Job, no orchestrator. Takes (namespace, pod_name) as CLI args, calls
Anthropic Sonnet 4.6 in a tool-use loop with four tools (kubectl_read,
kubectl_list, kubectl_get_container_logs, emit_findings — the two
parametric tools enforce supported `kind` enums in code), prints
structured Findings.

This is the FIRST agent-task variant. Future siblings (each a separate
Python package under src/) will follow the same shape:
  - intra_cluster_traffic_task  — Service / EndpointSlice / NetworkPolicy
  - hpa_latency_task            — HPA scaling delays
  - pvc_stuck_pending_task      — PVC binding issues

Shared infrastructure (the agent loop body, tool modules, observability)
will eventually migrate to an `agent_core` package once a second variant
makes the duplication concrete. YAGNI for now.

Demonstrates the agent-programming patterns documented in ARCHITECTURE.md:
- Single LLM context per task
- Bootstrap-in-task (deterministic pre-fetch before LLM loop)
- Forced structured output via emit_findings tool
- Exhaustive stop_reason handling
- Multi-tool-use-block-per-response handling
- "Data not instructions" prompt-level discipline against indirect injection
- Prompt caching on the system prompt (~21-40% input savings)
- Observability metrics shaped to match DiagnosisResponse.status.metrics
- Retry resilience (every Anthropic exception class handled structurally)
"""

__version__ = "0.1.0"
