# pod-launch scenarios

Fixtures for evaluating the `pod-launch` task's diagnostic quality. Each scenario is a deterministic broken-pod manifest paired with a structural expected-output spec.

These are the inputs the Step 4 eval framework consumes — first via eyeball grading (Step 4d), then via the calibrated model-as-judge (Step 4e onwards). Same fixtures are reused for CI gating in Step 4g and the formal eval suite in Step 7.

## Why structural specs (not exact-match)

LLM outputs vary. The same diagnosis can be phrased ten different ways. Exact-match assertions are brittle (one synonym change → fail) and shallow (catches phrasing variance, not diagnostic correctness).

Structural specs assert what we actually care about:

- **`outputKind`** — the agent produced the right structure (catches catastrophic failures: errors, malformed JSON, etc.)
- **`confidenceAtLeast`** — the agent isn't underconfident on cases that have clear signal
- **`problemMustMention.anyOf`** — the root-cause statement names at least one of the right concepts
- **`problemMustNotMention`** — the root-cause statement DOESN'T name a wrong diagnostic (catches "the agent hallucinated an image pull problem")
- **`remediationsMustInclude.anyOf`** — the top remediation references a real fix
- **`improvementsMustIncludeCategory.anyOf`** — preventive guidance is in the right category
- **`alsoCheckMustInclude.nonEmpty`** — soft constraint, just verifies the field is populated

The `anyOf` lists are synonym sets. Resilient to LLM phrasing variance; still catches "the agent never mentioned the concept."

## Scenarios

| Scenario | What it tests | Output kind | Implemented? |
|---|---|---|---|
| `insufficient-cpu` | Pod stuck Pending with `FailedScheduling`; CPU request > all node allocatable | Findings | ✅ |
| `image-pull-secret-missing` | Pod stuck ContainerCreating; namespace lacks `imagePullSecrets` for private registry | Findings | _planned_ |
| `init-crashloop` | Init container exits non-zero; main containers blocked | Findings | _planned_ |
| `readiness-probe-timeout` | Main container running but `Ready=false` because probe endpoint slow | Findings | _planned_ |
| `pull-image-slow` | Image pull genuinely in progress (network slow); K8s API insufficient to conclude | Findings (low-confidence, "K8s API exhausted, runtime evidence needed") | _planned_ |
| `pdb-blocks-eviction` (stretch) | Replacement pod can't schedule because PDB blocks evicting incumbent | Findings | _planned_ |
| `oom-killed-loop` (stretch) | Container killed by kernel OOM in restart loop | Findings | _planned_ |

## How to apply / clean up a scenario

```bash
# Apply (creates the namespace + broken pod):
make scenario-apply SCENARIO=insufficient-cpu

# Inspect the broken state:
kubectl get pods -n eval-insufficient-cpu
kubectl get events -n eval-insufficient-cpu --sort-by='.lastTimestamp'

# Clean up (deletes the namespace + everything in it):
make scenario-clean SCENARIO=insufficient-cpu
```

The `scenario-apply` target also waits for the pod to reach its expected stuck state before returning, so you know the fixture has reproduced. For `insufficient-cpu`, "stuck state" = `phase=Pending` with a `FailedScheduling` event.

## Adding a new scenario

1. Write `<name>.yaml` — namespace `eval-<name>` (PSA-restricted labels) + broken pod (PSA-compliant securityContext).
2. Write `<name>.expected.yaml` — structural spec following the schema above.
3. Add a row to the scenarios table in this README.
4. `make scenario-apply SCENARIO=<name>` and verify the pod reaches the intended stuck state.
5. `make scenario-clean SCENARIO=<name>` to tear down.

In a later PR, the `make eval` target will iterate over all scenarios automatically.

## Why one-namespace-per-scenario

- Cleanup is `kubectl delete namespace eval-<name>` — atomic, fast, can't leak resources.
- Parallel scenarios don't collide (different namespaces).
- Each fixture is reviewable in isolation; no shared state between scenarios.
- PSA labels apply per namespace; we can vary policy per scenario if needed (currently all are `restricted`).

## What's NOT in this directory

- Agent code (lands in `agent-task/` in a later PR)
- The harness that runs the agent against a scenario (later PR)
- The model-as-judge grader (later PR)
- Eyeball-grading workflow (later PR)

This directory is the *spec*: what scenarios exist and what good outputs look like. Everything else builds on top.
