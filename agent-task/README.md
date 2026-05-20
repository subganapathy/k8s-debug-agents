# agent-task

The agent-task family — diagnostic agents that produce structured Findings. Ships one variant today (`pod_launch_task`); future variants (`intra_cluster_traffic_task`, `hpa_latency_task`, …) will land as sibling Python packages under `src/`.

Each variant runs locally as a Python process; takes its variant-specific parameters and produces structured Findings + observability metrics.

This is **Phase 1 / PR #8** — the first variant (`pod_launch_task`):

- Standalone Python (no K8s Job, no CRDs, no orchestrator)
- Anthropic SDK + Sonnet 4.6
- Four tools: `kubectl_read` (parametric, by-name fetch), `kubectl_list` (parametric, kind-aware list), `kubectl_get_container_logs`, `emit_findings`. The two parametric tools enforce supported `kind` enums in code, so the LLM gets a single tool surface that scales across resource types instead of one tool per kind.
- Pre-flight bootstrap fetches initial pod state before the LLM loop starts
- Demonstrates the canonical agent loop patterns (tool-use protocol, stop_reason handling, forced structured output)

See `DESIGN.md` for the decision register and `../ARCHITECTURE.md` for the broader platform architecture.

## Prerequisites

- Python ≥3.11
- `kubectl` on `$PATH` with kubeconfig pointing at a cluster (Kind for dev)
- An Anthropic API key

## Setup

```bash
cd agent-task
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the package in editable mode so you can iterate on the code without reinstalling.

## Provide the API key

The agent reads `ANTHROPIC_API_KEY` from the environment. If you keep the key in macOS Keychain (recommended for dev — never enters shell history):

```bash
export ANTHROPIC_API_KEY=$(security find-generic-password \
    -a "$USER" -s "anthropic-api-key" -w | tr -d '\n\r')
```

The `tr -d '\n\r'` is critical — pasted keys often carry a trailing newline that fails Anthropic's validation (symptom: 401 "invalid x-api-key"). See `../README.md` "Security guardrails" for full context.

## Run against a scenario

The repo ships with an `insufficient-cpu` scenario fixture (PR #6). Apply it, then run the agent:

```bash
# From the repo root:
make scenario-apply SCENARIO=insufficient-cpu

# Then from agent-task/:
python -m pod_launch_task --namespace eval-insufficient-cpu --pod needs-massive-cpu
```

Expected output (truncated):

```
>>> Running pod-launch agent on eval-insufficient-cpu/needs-massive-cpu
[bootstrap] Fetching initial state for eval-insufficient-cpu/needs-massive-cpu...

[turn 1/10] Calling Anthropic...
[turn 1] stop_reason=tool_use in=... out=...
[turn 1] model says: The pod requests cpu=100 which is suspicious...
[turn 1] tool call: kubectl_list({"kind": "Event", "namespace": "eval-insufficient-cpu", "involved_object_kind": "Pod", "involved_object_name": "needs-massive-cpu"})
[turn 1] tool result: ... bytes, head: ...

[turn 2/10] Calling Anthropic...
[turn 2] stop_reason=tool_use in=... out=...
[turn 2] >>> emit_findings called; agent terminating

=== FINAL FINDINGS ===
{
  "findings": {
    "problem": "Pod eval-insufficient-cpu/needs-massive-cpu is stuck in Pending because...",
    "confidence": "high",
    "evidence": [...],
    "remediations": [...],
    "improvements": [...],
    "alsoCheck": [...]
  },
  "metrics": {
    "turns_used": 2,
    "input_tokens": ...,
    "output_tokens": ...
  }
}
```

When done, tear down the scenario:

```bash
make scenario-clean SCENARIO=insufficient-cpu
```

## How to read the code

Files, in order of dependency:

1. **`src/pod_launch_task/prompts.py`** — the system prompt. Phase-aware diagnostic playbooks + "data not instructions" framing + explicit definition-of-done.
2. **`src/pod_launch_task/tools.py`** — tool definitions (consumed by Anthropic's `tools` parameter), tool executor (kubectl subprocess), and the bootstrap helper (pre-LLM context fetch + filter).
3. **`src/pod_launch_task/agent.py`** — the agent loop itself. Handles every `stop_reason` explicitly. Recognizes `emit_findings` as the termination tool.
4. **`src/pod_launch_task/main.py`** — the CLI entry point. Parses args, checks for the API key, calls `run_agent`, prints the JSON result.

The loop in `agent.py` is the canonical pattern. Read it line-by-line — comments explain every non-obvious choice.

## What's intentionally simple

- **No structured logging.** Trajectory goes to stderr (left-to-right readable); final JSON goes to stdout (Unix convention for machine-parseable). No log files.
- **No async.** Synchronous everywhere. Simpler to reason about for first-time agent reading.
- **No model-as-judge.** A separate eval harness PR will add that.
- **No streaming.** We get full responses; no token-by-token streaming. Simplifies the loop; streaming pays off later when we want progress updates.

## What IS in the box (production-grade defaults)

- **Anthropic SDK retries on transient errors** (`max_retries=5`, `timeout=60s`). 5xx / 429 / connection errors retry with exponential backoff inside the SDK. When retries are exhausted, we surface a structured error result with `termination=api_retries_exhausted_*` instead of crashing.
- **Prompt caching on the system prompt.** The ~4300-char system prompt is marked `cache_control: ephemeral`. First turn pays cache-write cost (~1.25× input); turns 2+ pay ~0.10× input for the cache read. Roughly 30% input-cost reduction on a 3-turn investigation.
- **Observability in the `metrics` field** of every result: `model`, `turns_used`, `wall_clock_seconds`, `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `cost_usd` (computed from token usage × current Sonnet 4.6 pricing), `tool_calls_summary` (which tools the agent invoked, how many times). On degenerate paths, a `termination` field explains why.

These three are what makes the agent "orchestrator-ready" — when Step 4.5's controller wraps this, the metrics shape and error semantics are already what it needs.

## What this PR does NOT include

- The eval harness that runs the agent against scenarios and asserts the `.expected.yaml` spec
- The orchestrator controller + CRDs (Step 4.5)
- node-agent integration (Step 5)
- The Slack adapter (Step 5.5)

See `../current_state.md` (in memory; not committed) or the build phasing in `../ARCHITECTURE.md` §14 for the full sequence.
