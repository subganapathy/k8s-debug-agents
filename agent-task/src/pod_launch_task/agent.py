"""The agent loop.

This is the heart of an LLM-driven task: a while-loop that alternates
between calling the model and executing the model's tool_use requests.

Pseudocode:
    messages = [first_user_message]
    while not done:
        response = anthropic.messages.create(...)
        match response.stop_reason:
            case "tool_use":      execute tools, append tool_results, continue
            case "end_turn":      model finished without emit_findings; degenerate case
            case "max_tokens":    output truncated; degenerate case
            case other:           unexpected; degenerate case
        if exceeded max_turns: bail out

Important properties illustrated here:
- The full assistant response (text + tool_use blocks) is appended to
  messages verbatim. We must NOT reconstruct it — Anthropic requires the
  exact assistant content to be present.
- A single response can contain MULTIPLE tool_use blocks. We execute all
  of them and return ALL tool_results in a SINGLE user message.
- `stop_reason` is the branch instruction. Every possible value is handled
  explicitly; we never assume tool_use.
- `emit_findings` is recognized as the termination tool. When the model
  calls it, the input becomes the final Findings.

See ARCHITECTURE.md §10 (Two LLMs, symmetric pattern) for the broader
context.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

import anthropic
from pydantic import ValidationError

from pod_launch_task.findings import AgentResult, Findings, Metrics
from pod_launch_task.prompts import SYSTEM_PROMPT
from pod_launch_task.tools import TOOLS, bootstrap_pod_context, execute_tool

# Initial bound values per ARCHITECTURE.md §15 Decision #1. Re-tuned in
# Step 8 from measured eval data.
MAX_TURNS = 10
MAX_OUTPUT_TOKENS_PER_TURN = 8000
MODEL = "claude-sonnet-4-6"

# Resilience knobs. The Anthropic SDK retries 5xx / 429 / connection errors
# internally with exponential backoff. MAX_RETRIES=5 is more generous than
# the SDK's default of 2; we'd rather wait than fail an investigation that
# would have succeeded on the next try. REQUEST_TIMEOUT_SECONDS caps any
# single API call (across all internal retries).
MAX_RETRIES = 5
REQUEST_TIMEOUT_SECONDS = 60.0

# Pricing per million tokens. Used for the observability `cost_usd` field
# in Findings.metrics. Keep this updated when models change. Values are
# the public Anthropic pricing for Sonnet 4.6 as of 2026-05.
#
# Cache write costs ~1.25× input (Anthropic charges for the write
# operation). Cache read costs ~0.10× input (this is the win — the more
# turns you have, the more it amortizes).
PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
}


def run_agent(namespace: str, pod_name: str, verbose: bool = True) -> AgentResult:
    """Run the agent loop end-to-end against one pod.

    Returns:
        An AgentResult containing the structured Findings (problem,
        confidence, evidence, remediations, improvements, alsoCheck) plus
        a Metrics record (turns, wall-clock, tokens with cache breakdown,
        cost, tool calls, termination). On degenerate paths (max_turns
        exhausted, API failure, malformed emit_findings, etc.) `findings`
        carries a low-confidence placeholder and `metrics.termination`
        identifies why.
    """
    # SDK retries on 5xx / 429 / connection errors with exponential
    # backoff. `timeout` caps the total wall-time of any single call
    # (including all internal retries).
    client = anthropic.Anthropic(
        max_retries=MAX_RETRIES,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    start_time = time.monotonic()

    # Pre-flight: deterministic fetch of initial pod state. This is the
    # "bootstrap" step described in ARCHITECTURE.md §15 — it runs before
    # the LLM loop and feeds the model ground-truth context in turn 1.
    if verbose:
        print(f"[bootstrap] Fetching initial state for {namespace}/{pod_name}...", file=sys.stderr)
    boot = bootstrap_pod_context(namespace, pod_name)

    # The first user message includes the investigation goal and the
    # bootstrapped context. We wrap the bootstrapped data with provenance
    # tags (see ARCHITECTURE.md §16 content-boundary defenses) so the model
    # sees the trust boundary clearly.
    first_user_content = (
        f"Investigate why pod {namespace}/{pod_name} is having launch trouble.\n\n"
        f"Initial pod state, fetched at {boot['fetched_at_iso']} via kubectl get pod\n"
        f"(annotations and managedFields filtered out for prompt-injection hygiene):\n\n"
        f"<pod_state from='kubectl_read' trust='untrusted'>\n"
        f"{boot['pod_state_filtered']}\n"
        f"</pod_state>"
    )

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": first_user_content}
    ]

    # Metrics accumulators. We track cache_creation_input_tokens (the
    # first turn writes the system-prompt cache) and cache_read_input_tokens
    # (subsequent turns read it back at ~10% of normal input cost).
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_creation_tokens = 0
    total_cache_read_tokens = 0
    tool_calls_summary: dict[str, int] = {}

    for turn in range(1, MAX_TURNS + 1):
        if verbose:
            print(f"\n[turn {turn}/{MAX_TURNS}] Calling Anthropic...", file=sys.stderr)

        # Wrap the API call so we can produce a structured error result
        # if Anthropic returns a final failure after the SDK's retries.
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_OUTPUT_TOKENS_PER_TURN,
                # System prompt wrapped as a content block with cache_control
                # so Anthropic caches it for 5 minutes. First turn pays the
                # cache-write cost (~1.25× normal input); turns 2+ pay only
                # ~0.10× normal input for the cache hit. ~30% input-cost
                # reduction on a typical 3-turn investigation.
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                messages=messages,
            )
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.BadRequestError,
        ) as e:
            # Non-retryable. SDK does not retry these.
            return _build_error_result(
                reason=f"Anthropic API rejected the request: {type(e).__name__}: {e}",
                model=MODEL,
                start_time=start_time,
                turns_used=turn,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cache_creation_tokens=total_cache_creation_tokens,
                total_cache_read_tokens=total_cache_read_tokens,
                tool_calls_summary=tool_calls_summary,
                termination=f"api_error_{type(e).__name__}",
            )
        except (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.APIStatusError,
        ) as e:
            # Retryable in principle; the SDK already retried MAX_RETRIES
            # times. If we're here, retries are exhausted.
            return _build_error_result(
                reason=f"Anthropic API call failed after {MAX_RETRIES} retries: {type(e).__name__}: {e}",
                model=MODEL,
                start_time=start_time,
                turns_used=turn,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cache_creation_tokens=total_cache_creation_tokens,
                total_cache_read_tokens=total_cache_read_tokens,
                tool_calls_summary=tool_calls_summary,
                termination=f"api_retries_exhausted_{type(e).__name__}",
            )

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens
        # The cache_* fields are only present when prompt caching is in use.
        # `getattr ... or 0` handles both the missing-attribute case (older
        # SDK) and the None-value case (no cache hit/write this turn).
        total_cache_creation_tokens += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        total_cache_read_tokens += getattr(response.usage, "cache_read_input_tokens", 0) or 0

        if verbose:
            cache_note = ""
            cc = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            cr = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            if cc:
                cache_note = f" cache_write={cc}"
            elif cr:
                cache_note = f" cache_read={cr}"
            print(
                f"[turn {turn}] stop_reason={response.stop_reason} "
                f"in={response.usage.input_tokens} out={response.usage.output_tokens}{cache_note}",
                file=sys.stderr,
            )

        # The full assistant response (including text + tool_use blocks)
        # MUST be appended to messages exactly as received. Anthropic
        # rejects requests where assistant content has been mangled.
        messages.append({"role": "assistant", "content": response.content})

        # Branch on stop_reason. Every possible value handled explicitly.
        if response.stop_reason == "tool_use":
            # The model wants to call one or more tools.
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "text" and verbose:
                    print(f"[turn {turn}] model says: {block.text[:200]}", file=sys.stderr)
                if block.type != "tool_use":
                    continue

                # Special-case: emit_findings terminates the agent.
                if block.name == "emit_findings":
                    if verbose:
                        print(f"[turn {turn}] >>> emit_findings called; agent terminating", file=sys.stderr)
                    # Pydantic validation enforces the output contract. If the
                    # LLM emitted a malformed Findings (missing field, wrong
                    # enum value, etc.), we surface a structured error result
                    # instead of returning corrupt JSON downstream.
                    try:
                        findings = Findings.model_validate(dict(block.input))
                    except ValidationError as e:
                        return _build_error_result(
                            reason=f"emit_findings input failed schema validation: {e}",
                            model=MODEL,
                            start_time=start_time,
                            turns_used=turn,
                            total_input_tokens=total_input_tokens,
                            total_output_tokens=total_output_tokens,
                            total_cache_creation_tokens=total_cache_creation_tokens,
                            total_cache_read_tokens=total_cache_read_tokens,
                            tool_calls_summary=tool_calls_summary,
                            termination="emit_findings_validation_error",
                        )
                    return AgentResult(
                        findings=findings,
                        metrics=_build_metrics(
                            model=MODEL,
                            turns_used=turn,
                            start_time=start_time,
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                            cache_creation_input_tokens=total_cache_creation_tokens,
                            cache_read_input_tokens=total_cache_read_tokens,
                            tool_calls_summary=tool_calls_summary,
                        ),
                    )

                # Real tool calls (anything other than emit_findings) get
                # counted in the tool_calls_summary metric.
                tool_calls_summary[block.name] = tool_calls_summary.get(block.name, 0) + 1

                # Otherwise execute the tool and capture its result.
                if verbose:
                    print(f"[turn {turn}] tool call: {block.name}({json.dumps(dict(block.input))})", file=sys.stderr)
                result = execute_tool(block.name, dict(block.input))
                if verbose:
                    print(f"[turn {turn}] tool result: {len(result)} bytes, head: {result[:150]!r}", file=sys.stderr)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

            # All tool_results from this turn go back in a SINGLE user
            # message. (One user message can contain multiple tool_result
            # content blocks — that's how multi-tool-call responses are
            # handled.)
            messages.append({"role": "user", "content": tool_results})
            continue

        elif response.stop_reason == "end_turn":
            # Model decided to stop without calling emit_findings. This is
            # degenerate — we required emit_findings as the termination
            # path. The model is likely confused or the prompt failed to
            # constrain it. We capture any text content as a salvage.
            text = "".join(
                getattr(b, "text", "") for b in response.content if b.type == "text"
            )
            return _build_degraded_result(
                reason=f"Agent ended turn without calling emit_findings. Salvaged text: {text}",
                model=MODEL,
                start_time=start_time,
                turns_used=turn,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cache_creation_tokens=total_cache_creation_tokens,
                total_cache_read_tokens=total_cache_read_tokens,
                tool_calls_summary=tool_calls_summary,
                termination="end_turn_without_findings",
            )

        elif response.stop_reason == "max_tokens":
            # The model's response was truncated by our max_tokens cap.
            # In production we'd continue with the same messages array
            # and let it complete. For Phase 1 we report and exit.
            return _build_degraded_result(
                reason="Agent response was truncated by max_tokens cap mid-turn.",
                model=MODEL,
                start_time=start_time,
                turns_used=turn,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cache_creation_tokens=total_cache_creation_tokens,
                total_cache_read_tokens=total_cache_read_tokens,
                tool_calls_summary=tool_calls_summary,
                termination="max_tokens",
            )

        else:
            # Includes "pause_turn", "stop_sequence", "refusal", and any
            # future stop_reasons Anthropic might add. Treat as
            # unexpected; fail loudly so we notice during dev.
            return _build_degraded_result(
                reason=f"Agent terminated with unexpected stop_reason={response.stop_reason}.",
                model=MODEL,
                start_time=start_time,
                turns_used=turn,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cache_creation_tokens=total_cache_creation_tokens,
                total_cache_read_tokens=total_cache_read_tokens,
                tool_calls_summary=tool_calls_summary,
                termination=f"unexpected_{response.stop_reason}",
            )

    # Fell out of the loop — MAX_TURNS reached without emit_findings.
    return _build_degraded_result(
        reason=(
            f"Agent exhausted max_turns ({MAX_TURNS}) without calling emit_findings. "
            f"Investigation incomplete; consider raising the cap or tightening the prompt."
        ),
        model=MODEL,
        start_time=start_time,
        turns_used=MAX_TURNS,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cache_creation_tokens=total_cache_creation_tokens,
        total_cache_read_tokens=total_cache_read_tokens,
        tool_calls_summary=tool_calls_summary,
        termination="max_turns_exhausted",
    )


def _degraded_findings(reason: str) -> Findings:
    """Construct a placeholder Findings for degenerate termination paths.

    The eval harness can detect these via low confidence + the empty
    arrays. In Phase 1, we just report; in later PRs the orchestrator's
    LLM will read these and decide whether to retry / escalate.
    """
    return Findings(
        problem=reason,
        confidence="low",
        evidence=[],
        remediations=[],
        improvements=[],
        alsoCheck=[
            "Re-run the agent with verbose output to inspect the trajectory.",
            "Check whether the system prompt's definition-of-done is being respected.",
        ],
    )


def _compute_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> float:
    """Compute monetary cost in USD from token usage.

    Returns 0.0 for unknown models (we don't lie about cost we can't
    compute). Rounded to 6 decimal places — cents are small here.
    """
    pricing = PRICING_USD_PER_MTOK.get(model)
    if pricing is None:
        return 0.0
    cost = (
        input_tokens * pricing["input"]
        + output_tokens * pricing["output"]
        + cache_creation_input_tokens * pricing["cache_write"]
        + cache_read_input_tokens * pricing["cache_read"]
    ) / 1_000_000
    return round(cost, 6)


def _build_metrics(
    *,
    model: str,
    turns_used: int,
    start_time: float,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    tool_calls_summary: dict[str, int],
    termination: str | None = None,
) -> Metrics:
    """Build the Metrics record that accompanies every Findings.

    Shape is intentionally close to ARCHITECTURE.md's DiagnosisResponse
    schema so the orchestrator (Step 4.5) can project these directly into
    HandoffRequest.status.metrics without reshaping.
    """
    wall_clock = round(time.monotonic() - start_time, 2)
    cost = _compute_cost_usd(
        model,
        input_tokens,
        output_tokens,
        cache_creation_input_tokens,
        cache_read_input_tokens,
    )
    return Metrics(
        model=model,
        turns_used=turns_used,
        wall_clock_seconds=wall_clock,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cost_usd=cost,
        tool_calls_summary=dict(sorted(tool_calls_summary.items())),
        termination=termination,
    )


def _build_degraded_result(
    *,
    reason: str,
    model: str,
    turns_used: int,
    start_time: float,
    total_input_tokens: int,
    total_output_tokens: int,
    total_cache_creation_tokens: int,
    total_cache_read_tokens: int,
    tool_calls_summary: dict[str, int],
    termination: str,
) -> AgentResult:
    """Construct the AgentResult for any degenerate termination path."""
    return AgentResult(
        findings=_degraded_findings(reason),
        metrics=_build_metrics(
            model=model,
            turns_used=turns_used,
            start_time=start_time,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cache_creation_input_tokens=total_cache_creation_tokens,
            cache_read_input_tokens=total_cache_read_tokens,
            tool_calls_summary=tool_calls_summary,
            termination=termination,
        ),
    )


def _build_error_result(
    *,
    reason: str,
    model: str,
    turns_used: int,
    start_time: float,
    total_input_tokens: int,
    total_output_tokens: int,
    total_cache_creation_tokens: int,
    total_cache_read_tokens: int,
    tool_calls_summary: dict[str, int],
    termination: str,
) -> AgentResult:
    """Construct the AgentResult for an Anthropic API error.

    Same shape as `_build_degraded_result` — error vs degraded is just a
    semantic distinction for `termination`. The caller treats both as
    investigation failures and surfaces them to the operator.
    """
    return _build_degraded_result(
        reason=reason,
        model=model,
        turns_used=turns_used,
        start_time=start_time,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cache_creation_tokens=total_cache_creation_tokens,
        total_cache_read_tokens=total_cache_read_tokens,
        tool_calls_summary=tool_calls_summary,
        termination=termination,
    )
