"""Pydantic schemas — the single source of truth for the agent's typed contracts.

Holds three groups of types:

1. **Variant output contract** (Evidence, Remediation, Improvement,
   Findings, ToolMetrics, Metrics, AgentResult) — what every agent-task
   variant emits. Used by:
   - `emit_findings` tool's `input_schema` (generated via
     `Findings.model_json_schema()` — no hand-maintained JSON Schema
     that can drift from the Python type).
   - `run_agent()` return value (`AgentResult`).
   - The eval harness verifier in `evals/run_evals.py`.
   - Step 4.5's `HandoffRequest.status.findings` + `.metrics` CRD
     fields, whose `openAPIV3Schema` will be generated from these models.

2. **Sub-agent output contracts** (LogAnalysis) — structured outputs
   sub-agents emit to their parent variant. Same `model_json_schema()`
   pattern drives the sub-agent's sentinel tool input_schema.

3. **Uniform across LLM-driven AND deterministic variants.** A
   deterministic agent-task (e.g., a future `pod_existence_check` that
   just returns "pod was deleted") emits the same `Findings` shape.
   The LLM-specific Metrics fields (token counts, cache_*, model,
   turns_used) are Optional / default to 0, so deterministic variants
   populate just `wall_clock_seconds` (and maybe `termination`).

4. **Validation catches structured-output bugs.** When the LLM emits an
   `emit_findings` (or `emit_log_analysis`) call with a malformed shape
   (missing field, wrong type, wrong enum), `model_validate()` surfaces
   it as a structured error result instead of silent corruption
   downstream.

Field naming follows the existing JSON wire format exactly:
- `Findings.alsoCheck` (camelCase, matches what the agent has emitted since PR-8).
- `Metrics.turns_used`, `cache_creation_input_tokens`, etc. (snake_case).
The mixed casing is historical but stable; we preserve it so the JSON
output is byte-compatible with prior versions.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """One observation supporting the problem statement."""

    kind: str = Field(
        description=(
            "Category of evidence, e.g., k8s_event, pod_status, pod_spec, "
            "node_state, k8s_resource."
        ),
    )
    detail: str = Field(
        description="Specific observation supporting the problem statement.",
    )


class Remediation(BaseModel):
    """One ranked fix the operator could apply."""

    action: str = Field(
        description="Concrete change to make — kubectl command, YAML patch, etc.",
    )
    tradeoffs: str = Field(
        description="What could go wrong, what this trades against, when to prefer alternatives.",
    )


class Improvement(BaseModel):
    """One preventive suggestion that would catch this class of issue."""

    category: str = Field(
        description=(
            "Preventive category, e.g., pod_spec, scheduling, image_optimization, "
            "admission_control, monitoring, capacity_planning, pdb_configuration."
        ),
    )
    suggestion: str = Field(description="Concrete preventive change.")
    roi: Literal["low", "medium", "high"] = Field(
        description="Estimated return-on-investment of implementing this preventive change.",
    )


class Findings(BaseModel):
    """Structured Findings — the single output kind for every agent-task variant.

    Both LLM-driven (pod_launch_task) and deterministic (future
    pod_existence_check, metric_query, etc.) variants emit this same shape.
    """

    problem: str = Field(
        description="One-paragraph natural-language root cause statement.",
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "high: evidence directly supports root cause; "
            "medium: reasonable inference from evidence; "
            "low: hypothesis only OR runtime evidence beyond this agent's reach."
        ),
    )
    evidence: list[Evidence] = Field(
        description="Observations supporting the problem statement. Prefer 3-7 entries; avoid padding.",
    )
    remediations: list[Remediation] = Field(
        description="Ranked list of fixes. First entry should be the most likely fix.",
    )
    improvements: list[Improvement] = Field(
        description="Preventive suggestions that would catch this class of issue before it recurs.",
    )
    alsoCheck: list[str] = Field(
        description="Things the operator should verify next, especially if confidence is low.",
    )


class ToolMetricsDelta(BaseModel):
    """What `execute_tool` reports back to the parent agent loop ABOUT
    its own execution — specifically, any sub-agent activity it
    triggered. Defaults are empty/zero so tools that don't fire any
    sub-agents just construct `ToolMetricsDelta()` with no args.

    The parent loop merges these into the corresponding tool's
    `ToolMetrics` entry: this delta says "when you invoked
    kubectl_get_container_logs, log_triage fired once and cost
    $0.0045" — the loop attributes that to
    `metrics.tools["kubectl_get_container_logs"].sub_agent_calls` +
    `.sub_agent_cost_usd`.
    """

    sub_agent_calls: dict[str, int] = Field(default_factory=dict)
    sub_agent_cost_usd: float = 0.0


class ToolExecutionResult(BaseModel):
    """What `execute_tool` returns. Four fields with deliberate
    asymmetry between the raw `content` the LLM consumes (opaque
    forwarded-as-is) and the structured metadata the agent loop
    consumes (typed, validated).

    - `content`: the string the LLM sees as `tool_result.content`.
      May be raw bytes (small log output), JSON (serialized kubectl
      response or LogAnalysis), or an error message. Always `str`
      because Anthropic's tool_use protocol takes content as str
      and the agent code forwards it opaquely — never parses it.

    - `source`: a short label identifying WHO produced this content
      — e.g., "kubectl_read:Pod", "kubectl_list:Event",
      "kubectl_get_container_logs:log_triage" (sub-agent ran),
      "kubectl_get_container_logs:fallback" (sub-agent failed and
      returned truncated raw), "agent_core" (harness-internal
      message). The agent loop wraps content in a
      `<tool_result from='{source}' trust='{trust_tier}'>...</tool_result>`
      block so the LLM has per-result provenance, not just the
      global INPUT BOUNDARY clause in the system prompt.

    - `trust_tier`:
        * `untrusted` — K8s API content (annotations, env vars,
          labels, log lines — all attacker-writable by a pod's
          author).
        * `structured-from-untrusted` — sub-agent outputs that
          digested untrusted data into a Pydantic-validated form
          (e.g., LogAnalysis JSON). The SHAPE is now LLM-validated;
          the underlying source data was still attacker-controlled.
        * `trusted` — harness-internal messages we wrote ourselves
          (e.g., "unknown tool", K8s API error strings,
          "(no log output)").

    - `metrics_delta`: typed sub-agent activity (see
      `ToolMetricsDelta`).

    Replaces the prior `tuple[str, dict[str, Any]]` contract: IDE
    autocomplete works, consumers don't need defensive `.get(...)`
    on the metrics dict, and `source` + `trust_tier` are required
    at construction so every tool helper participates in the
    provenance scheme by default.
    """

    content: str
    source: str
    trust_tier: Literal["trusted", "untrusted", "structured-from-untrusted"] = "untrusted"
    metrics_delta: ToolMetricsDelta = Field(default_factory=ToolMetricsDelta)


class ToolMetrics(BaseModel):
    """Per-tool aggregated metrics for one agent invocation.

    One entry per tool the parent agent actually called. Captures three
    things per tool:
      - call_count: how many times this tool was invoked across all turns.
      - sub_agent_calls: which sub-agents fired during this tool's
        executions (e.g., {'log_triage': 1} for kubectl_get_container_logs
        when raw log output exceeded the 2 KiB threshold). Empty for
        tools that don't delegate to a sub-agent.
      - sub_agent_cost_usd: aggregate USD cost across the sub-agent
        invocations attributed to this tool. Zero when no sub-agent
        fired.

    Attribution lives at the parent-tool boundary: a sub-agent fired
    inside `_exec_logs` is rolled up under `kubectl_get_container_logs`,
    not in a separate top-level field. When more fat tools land (e.g.,
    `consult_runtime_specialist` when node-agent ships in Step 5/7),
    they add their own ToolMetrics entry — no top-level Metrics schema
    change required.
    """

    call_count: int = 0
    sub_agent_calls: dict[str, int] = Field(default_factory=dict)
    sub_agent_cost_usd: float = 0.0


class Metrics(BaseModel):
    """Observability metrics for one agent invocation.

    LLM-driven variants populate every field. Deterministic variants
    populate just `wall_clock_seconds` (+ optionally `termination`); the
    rest stay at their defaults (0 / None / empty dict). Same shape
    end-to-end so the orchestrator (Step 4.5) reads this from
    `HandoffRequest.status.metrics` without conditional handling.

    The parent's `input_tokens` / `output_tokens` / `cost_usd` fields
    cover ONLY the parent's own LLM calls. Tool interactions (including
    sub-agent invocations and their costs) live in `tools`, keyed by
    tool name, so each tool's "footprint" is self-contained.
    """

    model: str | None = None
    turns_used: int | None = None
    wall_clock_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    tools: dict[str, ToolMetrics] = Field(
        default_factory=dict,
        description=(
            "Per-tool aggregated metrics. Keyed by tool name. Empty when "
            "the agent invoked no tools (e.g., diagnosed entirely from "
            "bootstrap state). See ToolMetrics."
        ),
    )
    termination: str | None = None


class AgentResult(BaseModel):
    """Top-level result returned by run_agent() and printed to stdout."""

    findings: Findings | None
    metrics: Metrics


# ─── Sub-agent output schemas ─────────────────────────────────────────────────


class LogAnalysis(BaseModel):
    """Structured output from the log-triage Haiku sub-agent.

    Returned by `kubectl_get_container_logs` when raw log output exceeds the
    bypass threshold (currently 2 KiB). For smaller outputs the parent agent
    receives raw log bytes; for larger outputs it receives a JSON-serialized
    `LogAnalysis` instead — keeps parent context bounded regardless of log
    size. See `design_log_triage_subagent.md` for the full design.
    """

    signature: Literal[
        "panic",
        "crashloop",
        "missing_config",
        "oom",
        "retry_storm",
        "startup_slow",
        "none",
    ] = Field(
        description=(
            "Single best-fit failure signature, or 'none' if no clear failure "
            "is visible. When multiple signatures are present, pick the one "
            "that best explains the MOST RECENT log lines (failures crescendo)."
        ),
    )
    key_lines: list[str] = Field(
        description=(
            "3-7 verbatim log lines that best capture the signature. Prefer "
            "lines near the end of the output (most recent context)."
        ),
    )
    evidence_excerpt: str = Field(
        description=(
            "Short raw extract — stack trace head, actual error message, the "
            "missing-config line, etc. Keep under ~500 chars."
        ),
    )
    suspected_cause: str = Field(
        description="One-paragraph hypothesis about the root cause.",
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "high: signature is unambiguous; medium: reasonable inference; "
            "low: signal is weak or ambiguous."
        ),
    )
