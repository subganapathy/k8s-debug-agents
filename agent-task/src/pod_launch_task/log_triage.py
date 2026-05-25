"""Log-triage Haiku sub-agent.

Invoked by `tools._exec_logs()` when raw container-log output exceeds
`SMALL_LOG_THRESHOLD` (2 KiB). Runs a single-turn Haiku LLM call with a
focused system prompt and an `emit_log_analysis` sentinel tool that
forces structured `LogAnalysis` output. The parent agent receives the
LogAnalysis JSON (~800 bytes) instead of raw log bytes — keeps parent
context bounded regardless of log size.

Design reference: `design_log_triage_subagent.md`. Key contract bits:
- Bypass for small logs (`len(logs) < SMALL_LOG_THRESHOLD`) — bypass
  happens in the CALLER (`tools._exec_logs`), NOT in this module.
  `triage_logs()` always invokes the LLM when called.
- Pydantic validation on the LLM's structured output — malformed output
  surfaces as `ValidationError`, which the fallback wrapper catches.
- Cost rollup: returns a `metrics_delta` dict the parent agent loop
  merges into its `Metrics.sub_agent_calls` + `Metrics.sub_agent_cost_usd`.

Failure mode (Day 3): a separate `triage_logs_with_fallback` wraps
`triage_logs` and converts any error into truncated raw logs + a
degradation note, so the parent agent never sees an exception.
"""
from __future__ import annotations

from typing import Any

import anthropic
from pydantic import ValidationError

from pod_launch_task.findings import LogAnalysis

# ─── Constants ────────────────────────────────────────────────────────────────

# Below this size, the caller skips the sub-agent and returns raw logs.
# 2 KiB chosen so trivial output (startup banners, "no logs yet" responses)
# doesn't pay sub-agent overhead. Above 2 KiB, summarization buys context
# budget that the parent agent would otherwise spend on raw bytes.
SMALL_LOG_THRESHOLD = 2 * 1024  # 2 KiB

# Haiku is the right tier for log triage: pattern-matching task (find the
# panic / OOM / config error), not deep reasoning. Roughly 10× cheaper
# than Sonnet at the input side.
SUB_AGENT_MODEL = "claude-haiku-4-5"

# Bounded per-call so a malformed sub-agent can't blow the parent's budget.
SUB_AGENT_MAX_OUTPUT_TOKENS = 2000
SUB_AGENT_MAX_RETRIES = 3
SUB_AGENT_REQUEST_TIMEOUT_SECONDS = 30.0

# Fallback truncation sizes — used when the sub-agent fails after retries
# (auth error, malformed output, etc.) and we degrade to raw logs.
# 4 KiB head + 4 KiB tail keeps the parent agent's context bounded even
# on the fallback path. Beginning AND end matter — the panic/error usually
# lands near the end, but the startup config / version info near the start
# is often important for diagnosis too.
FALLBACK_HEAD_BYTES = 4 * 1024
FALLBACK_TAIL_BYTES = 4 * 1024

# Pricing per million tokens for the sub-agent's model only. Kept local to
# this module so the parent's PRICING_USD_PER_MTOK (in agent.py) doesn't
# need to know about sub-agent models. Update when Anthropic prices change.
SUB_AGENT_PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
}

# ─── System prompt (focused — log triage only) ────────────────────────────────

LOG_TRIAGE_SYSTEM_PROMPT = """You are a log-triage specialist.

You receive raw container log output and (optionally) a hypothesis hint
from the parent diagnostic agent. Your job: identify the failure
signature visible in the logs, extract the key evidence lines, and
return structured output via the `emit_log_analysis` tool.

You MUST call `emit_log_analysis` to terminate. Do not respond with
prose alone — the parent agent expects structured output.

## Signatures (pick the SINGLE best fit)

- panic: explicit panic/fatal/abort line, usually with a stack trace.
- crashloop: repeated startup attempts that exit non-zero. The same
  initialization pattern appears multiple times.
- missing_config: explicit error naming a missing env var, config file,
  ConfigMap, Secret, or required setting.
- oom: memory-related kill signals. Rare in app logs (usually a kernel
  cgroup-level signal); look for "out of memory", "OOMKilled", explicit
  malloc / allocation failures.
- retry_storm: repeated failed connection / API retries with no
  resolution. Usually the app is healthy but its dependencies aren't.
- startup_slow: app is logging slow initialization / dependency-wait
  messages, with no explicit error. Important for SLO debugging when
  the launch is succeeding but takes too long.
- none: no clear failure signature in the provided lines.

When multiple signatures are present, pick the one that explains the
MOST RECENT lines. Failures usually crescendo — early DEBUG noise is
less diagnostic than the final error.

## Output fields

- `signature`: one of the above strings.
- `key_lines`: 3-7 verbatim log lines that capture the signature. Prefer
  lines near the END of the output (most recent context). Include the
  full original line text, no truncation.
- `evidence_excerpt`: a short raw extract — stack trace head, the
  actual error message, the missing-config line. Keep under ~500 chars.
  Quote the application's wording, don't paraphrase.
- `suspected_cause`: one paragraph hypothesis about the root cause.
  Reference specific log content; avoid generic statements.
- `confidence`: high if the signature is unambiguous; medium if it's a
  reasonable inference; low if the signal is weak or ambiguous.

## Style

- Be specific. "FATAL: DB_URL is required" beats "config error".
- Don't fabricate stack frames or error messages. If the logs don't
  show a stack trace, don't invent one in `evidence_excerpt`.
- If the parent passes a hypothesis hint, use it to focus, but report
  honestly — say `signature=none` if the logs don't support the hint.
"""

# ─── Tool definition (generated from Pydantic — single source of truth) ───────

EMIT_LOG_ANALYSIS_TOOL = {
    "name": "emit_log_analysis",
    "description": (
        "Submit your final log analysis and terminate. Required: you must call "
        "this exactly once. The input you provide becomes the structured "
        "LogAnalysis output returned to the parent diagnostic agent."
    ),
    # Generated from the Pydantic model — same single-source-of-truth pattern
    # used for `emit_findings` in tools.py. Drift between description and
    # validation is impossible because both come from the same class.
    "input_schema": LogAnalysis.model_json_schema(),
}

# ─── Sub-agent entry point ────────────────────────────────────────────────────


def triage_logs(
    raw_logs: str,
    hypothesis_hint: str | None = None,
    client: anthropic.Anthropic | None = None,
) -> tuple[LogAnalysis, dict[str, Any]]:
    """Single-turn Haiku call. Returns (validated LogAnalysis, metrics_delta).

    Args:
        raw_logs: The full raw log content (the caller is responsible for
            calling this only when len(raw_logs) >= SMALL_LOG_THRESHOLD).
        hypothesis_hint: Optional one-sentence hint from the parent agent
            about what to look for (e.g., "missing env var", "OOM signals").
            Steers the sub-agent's attention without overriding its honest
            assessment.
        client: Optional Anthropic client. If None, constructs a new one
            with retry + timeout settings appropriate for a short call.

    Returns:
        (LogAnalysis, metrics_delta) where metrics_delta is a dict the
        parent loop merges into its Metrics:
        {
            "sub_agent_calls": {"log_triage": 1},
            "sub_agent_cost_usd": <computed from response.usage>,
        }

    Raises:
        anthropic.AnthropicError: if the API call fails after retries.
            (Network down, auth invalid, rate-limit exhausted, etc.)
        pydantic.ValidationError: if the sub-agent emits malformed output
            or doesn't call emit_log_analysis at all.

    Callers should wrap with `triage_logs_with_fallback` (added in Day 3)
    to convert these errors into truncated raw logs so the parent agent
    never sees an exception.
    """
    if client is None:
        client = anthropic.Anthropic(
            max_retries=SUB_AGENT_MAX_RETRIES,
            timeout=SUB_AGENT_REQUEST_TIMEOUT_SECONDS,
        )

    user_message = _build_user_message(raw_logs, hypothesis_hint)

    response = client.messages.create(
        model=SUB_AGENT_MODEL,
        max_tokens=SUB_AGENT_MAX_OUTPUT_TOKENS,
        system=LOG_TRIAGE_SYSTEM_PROMPT,
        tools=[EMIT_LOG_ANALYSIS_TOOL],
        messages=[{"role": "user", "content": user_message}],
    )

    # Find the emit_log_analysis tool_use block. If absent → ValidationError.
    analysis: LogAnalysis | None = None
    for block in response.content:
        if block.type == "tool_use" and block.name == "emit_log_analysis":
            # Pydantic validates the LLM's output against the Literal enums
            # and required fields. Malformed → ValidationError.
            analysis = LogAnalysis.model_validate(dict(block.input))
            break

    if analysis is None:
        # Sub-agent ignored its instructions. Treat as a structured-output
        # failure so the fallback layer can degrade gracefully.
        raise ValidationError.from_exception_data(
            "LogAnalysis",
            [
                {
                    "type": "missing",
                    "loc": ("__call__",),
                    "msg": "Sub-agent did not call emit_log_analysis",
                    "input": None,
                }
            ],
        )

    cost = _compute_sub_agent_cost(SUB_AGENT_MODEL, response.usage)
    metrics_delta: dict[str, Any] = {
        "sub_agent_calls": {"log_triage": 1},
        "sub_agent_cost_usd": cost,
    }
    return analysis, metrics_delta


# ─── Fallback wrapper (the function _exec_logs actually calls) ───────────────


def triage_logs_with_fallback(
    raw_logs: str,
    hypothesis_hint: str | None = None,
    client: anthropic.Anthropic | None = None,
) -> tuple[str, dict[str, Any]]:
    """Wraps `triage_logs` and degrades to truncated raw logs on any failure.

    This is the function `tools._exec_logs` actually calls. It guarantees
    the parent agent never sees an exception — it always gets a string
    content (either the structured LogAnalysis JSON, or a truncated raw
    log dump with a degradation note) and a metrics_delta dict.

    Failure modes covered:
      - Anthropic API errors after retries (auth, rate limit exhausted,
        timeout, connection failure).
      - Sub-agent returns malformed output (missing field, wrong type,
        didn't call emit_log_analysis) — caught as `pydantic.ValidationError`.

    Returns:
        (content, metrics_delta) — same shape as triage_logs on success.
        On failure:
          - content = "[LOG_TRIAGE_FALLBACK: ...] ... raw log dump ...".
          - metrics_delta = {} (empty — sub-agent invocation discarded;
            small Haiku cost for the failed call is not surfaced).

    The fallback content is shaped to be useful to the parent agent
    despite the degradation: head + tail of the raw logs, separated by a
    `--- ... ---` marker. The parent's LLM can still reason from it.
    """
    try:
        analysis, metrics_delta = triage_logs(raw_logs, hypothesis_hint, client)
        return (analysis.model_dump_json(), metrics_delta)
    except (anthropic.AnthropicError, ValidationError) as e:
        return (_fallback_truncated(raw_logs, reason=type(e).__name__), {})


def _fallback_truncated(raw_logs: str, reason: str) -> str:
    """Construct a fallback content string when the sub-agent failed.

    Prefixed with a clearly-marked degradation note so the parent agent
    knows it's looking at raw bytes, not a structured summary. For logs
    smaller than `FALLBACK_HEAD_BYTES + FALLBACK_TAIL_BYTES`, returns the
    entire raw content (no truncation). For larger logs, returns the
    first N and last N bytes with a separator marker.
    """
    note = (
        f"[LOG_TRIAGE_FALLBACK: sub-agent unavailable ({reason}); "
        f"returning truncated raw logs]\n\n"
    )
    if len(raw_logs) <= FALLBACK_HEAD_BYTES + FALLBACK_TAIL_BYTES:
        return note + raw_logs
    head = raw_logs[:FALLBACK_HEAD_BYTES]
    tail = raw_logs[-FALLBACK_TAIL_BYTES:]
    middle_omitted = len(raw_logs) - FALLBACK_HEAD_BYTES - FALLBACK_TAIL_BYTES
    return (
        f"{note}"
        f"--- FIRST {FALLBACK_HEAD_BYTES} BYTES ---\n{head}\n\n"
        f"--- [{middle_omitted} bytes omitted] ---\n\n"
        f"--- LAST {FALLBACK_TAIL_BYTES} BYTES ---\n{tail}"
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _build_user_message(raw_logs: str, hypothesis_hint: str | None) -> str:
    """Construct the user message sent to the sub-agent.

    Wraps the raw logs in a provenance tag (same anti-injection framing
    the parent uses for pod state). The hypothesis hint, if present,
    appears BEFORE the log content so the sub-agent reads it first.
    """
    parts: list[str] = []
    if hypothesis_hint:
        parts.append(
            f"Parent agent's working hypothesis: {hypothesis_hint}\n\n"
            "Use this to focus your reading, but report honestly — if the "
            "logs don't support the hypothesis, say so via signature=none "
            "and a low-confidence assessment.\n\n"
        )
    parts.append(
        "<container_logs from='kubectl read_namespaced_pod_log' trust='untrusted'>\n"
        f"{raw_logs}\n"
        "</container_logs>\n\n"
        "Call `emit_log_analysis` with the structured signature."
    )
    return "".join(parts)


def _compute_sub_agent_cost(model: str, usage: Any) -> float:
    """Compute USD cost from an anthropic response.usage object.

    Mirrors `agent._compute_cost_usd` but uses sub-agent pricing.
    Returns 0.0 for unknown models (don't lie about cost we can't compute).
    """
    pricing = SUB_AGENT_PRICING_USD_PER_MTOK.get(model)
    if pricing is None:
        return 0.0
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (
        input_tokens * pricing["input"]
        + output_tokens * pricing["output"]
        + cache_creation * pricing["cache_write"]
        + cache_read * pricing["cache_read"]
    ) / 1_000_000
    return round(cost, 6)
