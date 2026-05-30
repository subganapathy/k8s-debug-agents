# Eval harness

Automated regression gate for the pod-launch agent. For each scenario fixture under `evals/scenarios/pod-launch/`, the harness applies the K8s fixture, runs the agent, asserts the paired `.expected.yaml` spec against the agent's typed `AgentResult` output, and cleans up. Exits non-zero if any assertion fails or the agent crashes.

## Usage

```bash
# Set ANTHROPIC_API_KEY first (see agent-task/README.md for keychain recipe).
make eval                       # Run all scenarios, human-readable output.
make eval SCENARIO=insufficient-cpu  # Run just one scenario.
make eval JSON=1                # Emit machine-readable JSON.
make eval SCENARIO=foo SKIP_APPLY=1 SKIP_CLEAN=1   # Iteration mode.
```

Exit code: `0` if all scenarios pass, `1` if any failed.

## What the verifier checks

Each `.expected.yaml` spec is a small DSL the verifier reads:

```yaml
target:
  scenarioName: <name>            # documentation; not asserted
  namespace: eval-<name>          # passed to the agent as --namespace
  podName: <pod>                  # passed to the agent as --pod

expected:
  outputKind: Findings            # agent emitted a Findings (not a crash result)

  confidenceAtLeast: medium       # findings.confidence ≥ medium (ordinal: low < medium < high)

  problemMustMention:
    anyOf: [...]                  # at least ONE substring (case-insensitive) appears in problem

  problemMustNotMention: [...]    # NONE of these substrings appear (catches hallucinated wrong-phase diagnoses)

  remediationsMustInclude:
    anyOf: [...]                  # checked against concatenated remediation.action strings

  improvementsMustIncludeCategory:
    anyOf: [...]                  # at least one improvement.category matches (case-sensitive)

  alsoCheckMustInclude:
    nonEmpty: true                # findings.alsoCheck has at least one entry
```

All fields are optional — omit any check you don't want to assert. Empty `anyOf` lists are vacuously true (signal "no constraint" by omitting, not emptying).

## How it works

The harness is a single Python file (`evals/run_evals.py`) with two classes:

1. **`Verifier`** — pure function. Given an `AgentResult` and a parsed spec, returns a `VerificationReport` with per-check pass/fail. No cluster, no subprocess. Unit-testable against cached agent outputs.
2. **`Harness`** — orchestrator. For each scenario:
   - `subprocess.run(["make", "scenario-apply", "SCENARIO=...", ...])` (unless `--skip-apply`)
   - `subprocess.run([sys.executable, "-m", "agent_core", "--quiet", "pod-launch", "--namespace", ..., "--pod", ...])`
   - `AgentResult.model_validate_json(stdout)` — Pydantic surfaces any malformed agent output as a clean error
   - `verifier.verify(...)` against the spec
   - `subprocess.run(["make", "scenario-clean", "SCENARIO=...", ...])` in `finally` — runs even on crash

Crashes in any phase (subprocess failure, agent output didn't parse, etc.) become a failed `VerificationReport` with `crash_reason` set. The sweep continues across remaining scenarios.

## Running individual checks during dev

When iterating on the agent or a scenario:

```bash
# Apply the fixture once, then iterate the agent + verifier without re-applying:
make scenario-apply SCENARIO=foo
make eval SCENARIO=foo SKIP_APPLY=1 SKIP_CLEAN=1
make eval SCENARIO=foo SKIP_APPLY=1 SKIP_CLEAN=1   # iterate on prompt/code without paying re-apply cost
make scenario-clean SCENARIO=foo                    # done
```

## Cost reporting

The summary line shows total parent-agent cost across all scenarios. Sub-agent costs are tracked per-tool in `metrics.tools` and don't appear in the summary (a full-cost rollup is future work — for now sub-agent costs surface in the per-scenario `--json` output as `tools.<tool_name>.sub_agent_cost_usd`).

## Adding a new scenario

1. Write `evals/scenarios/pod-launch/<name>.yaml` — namespace + broken pod (PSA-restricted).
2. Write `evals/scenarios/pod-launch/<name>.expected.yaml` — structural spec (see above).
3. `make scenario-apply SCENARIO=<name>` — verify the fixture reaches its intended stuck state.
4. `make eval SCENARIO=<name>` — verify the agent's output matches the spec.
5. Commit and open a PR.

Background design: `current_state.md` + `design_pr9_plan.md` in the project memory.
