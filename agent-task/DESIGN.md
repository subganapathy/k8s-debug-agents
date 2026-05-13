# agent-task — design log

This document is the **source of truth** for agent-task design decisions. Code derives from it, not the reverse. Each decision below was made by a human with full understanding of the tradeoffs, not chosen by default.

The first variant designed concretely is `pod-launch`. The `intra-cluster-traffic-not-flowing` variant is derived by analogy in a later step.

## Decision register

| # | Group | Decision | Choice | Rationale |
|---|---|---|---|---|
| 1 | Identity | Role | **LOCKED** (see below) | See "Decision #1 — Identity & Role" |
| 2 | Identity | Primary model | `claude-sonnet-4-6` | Diagnosis quality is the entire point; Sonnet is the production sweet spot for tool-use-heavy reasoning in 2026 |
| 3 | Identity | Output contract | `Diagnosis` OR `NeedsNodeAgent` (structured) | Locked from `design_output_contract.md`; 5-dim grading rubric maps to fields |
| 4 | Identity | Termination criterion | Two-clause "definition of done" in system prompt | Gives model an explicit meta-reasoning checkpoint |
| 5 | Tools | K8s tool catalog + HandoffContext schema | **NEXT** | Pulled forward from #13 — they're two halves of the same interface |
| — | Architecture | Dispatcher controller language | **Python + kopf** (LOCKED 2026-05-11) | Stack consistency; controller eventually IS an agent (Step 6 adds LLM judgment in reconcile) |
| 6 | Tools | Node-agent tool shape | **LOCKED 2026-05-10** | DaemonSet exposing narrow read-only RPC API over hostPath UDS. LLM (Specialist Job) is unprivileged; only mounts the UDS. See `~/.claude/.../memory/design_node_agent_architecture.md` |
| 7 | Tools | Tool error format | _next_ | |
| 8 | Tools | Tool-result truncation | _next_ | |
| 9 | Tools | Tool timeouts | _next_ | |
| 10 | Architecture | Pattern A (sub-agent-as-tool) | **LOCKED** | Single triage agent, specialist invoked via `consult_runtime_specialist` tool. Specialist deferred to Step 5. |
| 11 | Architecture | Specialist model + handoff strategy | _Step 5_ | Three options surface when we get there |
| 12 | Architecture | Specialist invocation timing | _Step 5_ | |
| 13 | Architecture | HandoffContext shape | **MERGED into #5** | Cannot design tool surface without designing what flows to specialist |
| 14 | Bounding | max_turns | **10 (initial)** | Re-tuned in Step 8 from measured eval data |
| 15 | Bounding | Token budget | **input=50K cumulative, output=8K/turn (initial)** | |
| 16 | Bounding | Wall-clock | **120s (initial)** | Hung Job blocks slot but burns no API |
| 17 | Bounding | Cost cap | **$0.50 hard, $0.10 alarm (initial)** | Fail-safe for runaway loops |
| 18 | Context | Growth strategy | _later_ | At max_turns=10 we likely don't need compaction |
| 19 | Context | Compaction policy | _later_ | |
| 20 | Context | Cache breakpoints | _later_ | After tools land |
| 21 | Output | Structured-output mechanism | _later_ | Three options: tool_use forced / JSON mode / prompt+parse |
| 22 | Output | Validation + retry | _later_ | |
| 23 | Output | Failure-to-diagnose policy | **LOCKED** (in role) | Low-confidence Diagnosis with `also_check` populated when budget exhausts |
| 24 | Obs | Per-run metrics | _later_ | |
| 25 | Obs | Tracing | _later_ | |
| 26 | Eval | Scenario harness | **LOCKED** (in Step 4) | Eyeball-first calibration → model-as-judge; 5–7 scenarios |
| 27 | Eval | Grading dimensions | locked | 5-dim from `design_output_contract.md` |
| 28 | Errors | Model error policy | _later_ | |
| 29 | Errors | Tool error policy | _later_ | |
| 30 | Security | Agent SA RBAC | _later_ | Read-only K8s; namespace-scoped where possible |
| 31 | Security | Output sanitization | _later_ | |

---

## Decision #1 — Identity & Role (LOCKED)

### Mission (system-prompt mission statement)

> **The pod-launch agent-task diagnoses why a specific named pod is slow to launch, stuck launching, or failing to launch. It runs as a Kubernetes Job pinned to the affected pod's node, receiving `(namespace, pod_name)` and pre-fetched context as input. Using read-only K8s API tools and a Sonnet 4.6 reasoning loop, it forms a hypothesis from control-plane evidence and produces a structured `Diagnosis` (root cause + ranked remediations with tradeoffs + preventive improvements + also_check). When the K8s API surface is insufficient and runtime evidence is required, it returns a structured `NeedsNodeAgent` handoff rather than guessing — Step 5's specialist sub-agent will consume that handoff. The agent does not search for pods, does not interpret free-text alerts, does not take destructive actions, and does not investigate other nodes; each is deferred to a later step.**

### Structured surfaces

| Surface | Locked value |
|---|---|
| **Trigger** | `dispatcher diagnose pod-launch <namespace> <pod>` (CLI, deterministic) |
| **Input contract** | `(namespace, pod_name)` + dispatcher pre-fetches `(filtered_pod_spec, pod_status, recent_events, immediate_owner_ref, node_name, node_labels)` baked into system prompt as initial context |
| **Pre-flight filtering** | Drop `metadata.annotations`, `metadata.managedFields`, deep owner-chain, env literal values. Truncate labels >256 chars and messages >2KB. Reduces context size ~40-60% and shrinks prompt-injection surface to near-zero. (Spec'd in Decision #5.) |
| **Pod-deleted edge case** | Agent-task re-fetches pod at startup (deterministic, before any LLM call). If 404, emits a "pod-deleted" `Diagnosis` (`confidence: high`, `problem: "pod was deleted between dispatcher pre-flight and Job scheduling"`) and exits. Zero LLM calls. |
| **Placement** | Job pinned to `pod.spec.nodeName` via nodeAffinity (deterministic) |
| **Primary model** | `claude-sonnet-4-6` |
| **Tool surface** | Read-only K8s API tools (Decision #5). **NO write actions** by architectural invariant. |
| **Output contract** | `Diagnosis(problem, evidence, confidence, remediations, improvements, also_check)` OR `NeedsNodeAgent(stage, reason, handoff_ctx)`. Both structured; locked from `design_output_contract.md`. |
| **Bounds (initial)** | `max_turns=10`, `input_tokens=50K`, `output_tokens=8K/turn`, `wall_clock=120s`, `cost_cap=$0.50` (alarm at $0.10). Re-tuned in Step 8 from measured data. |
| **Definition of done** | See system-prompt clauses below. |
| **Loop policy** | Hypothesize → tools → confirm/refute → if confidence ≥ medium emit Diagnosis; if K8s exhausted + runtime hypothesis emit NeedsNodeAgent; if budget exhausted emit low-confidence Diagnosis with populated `also_check`. |
| **Failure modes** | (1) pre-flight 404 → handled; (2) malformed model output → Decision #22 retry; (3) model loops → `max_turns`; (4) model timeout → `wall_clock`; (5) model 5xx → Decision #28 retry policy. |

### System prompt structure

Single agent, **phase-aware sections**. The pre-flight context tells the agent which section applies; the model can also synthesize across sections (e.g., CrashLoopBackOff with `reason: ImagePullBackOff` matches both post-start and runtime-creation playbooks).

```
## Diagnostic playbooks (use the section that matches initial pod state)

### If status.phase == "Pending" and containerStatuses is empty:
   You are in the SCHEDULING phase. Investigate: node allocatable, taints/tolerations,
   nodeSelector/nodeAffinity, PodDisruptionBudgets, ResourceQuota, scheduling events.

### If status.phase == "Pending" with containerStatuses[].state.waiting.reason in
    [ContainerCreating, PodInitializing]:
   You are in the RUNTIME-CREATION phase. Investigate: image pull state, image pull secrets,
   PVC binding, ConfigMap/Secret references, init container progress.

### If init containers are running and not yet complete:
   You are in the INIT phase. Investigate: init container logs, expected init duration,
   init container resource constraints.

### If main containers are running but Ready=False:
   You are in the READINESS phase. Investigate: readiness probe config + recent results,
   probe endpoint health, startup probe configuration.

### If any container shows state.waiting.reason in [CrashLoopBackOff, Error]:
   You are in the POST-START-FAILURE phase. Investigate: container logs (current + previous),
   exit codes, lastState.terminated.message, OOM signals, application config.
```

### Definition of done (system prompt clauses)

> **You are done when EITHER:**
>
> **(a)** You have evidence sufficient to identify a single root cause with at least *medium* confidence, AND you have ranked at least one remediation with explicit tradeoffs, AND you have populated the `also_check` and `improvements` fields. → Emit `Diagnosis`.
>
> **(b)** You have exhausted what the K8s API can tell you, AND your hypothesis points to a runtime/CRI/CNI/CSI layer that requires node-agent inspection. → Emit `NeedsNodeAgent` with full `handoff_ctx`.
>
> **You are NOT done if:** you have a hypothesis but no confirming evidence, you have evidence but no clear root cause, you have a root cause but no ranked remediation. Keep investigating, but never exceed your budget.

### What this agent does NOT do (out-of-scope, with destinations)

| Excluded | Lands in |
|---|---|
| Pod discovery from alerts | Step 6 (dispatcher-as-agent) |
| Free-text alert interpretation | Step 6 |
| HPA-latency / autoscaling-derived alerts | Step 6 (alert-triage layer) |
| Multi-pod parallel diagnosis | Step 6+ |
| Specialist sub-agent (actual runtime investigation) | Step 5 |
| node-agent (the daemon the specialist talks to) | Step 5 |
| Cross-node investigation | Step 5 (`delegate_to_node` tool, UDS-only architecture) |
| `intra-cluster-traffic-not-flowing` variant | Step 7 |
| Write tools (mutating K8s actions) | Out of scope for the entire pod-launch problem class |

### Step 4 sub-steps (eval-driven)

```
4a. Write 5–7 scenarios + STRUCTURAL spec (cheap rule-based assertions)
4b. Build agent loop + K8s tools + dispatcher CLI
4c. Run agent on scenarios; structural assertions auto-pass/fail
4d. Eyeball-grade 3–5 outputs per scenario on the 5 quality dims (RC/RS/SE/AC/IQ)
4e. Build model-as-judge USING calibration data from 4d
4f. Validate judge agrees with eyeball on ≥80% of dim scores; refine rubric until aligned
4g. CI integration; subsequent prompt iterations use calibrated judge
```

### Initial scenario set (Decision #26 detail)

| Scenario | Phase tested | Expected output | Notes |
|---|---|---|---|
| `insufficient-cpu` | Scheduling | Diagnosis: CPU/scheduling | Pod requests CPU > all node allocatable |
| `image-pull-secret-missing` | Runtime-creation | Diagnosis: pull-secret/registry | Pod refs private image; namespace has no `imagePullSecrets` |
| `init-crashloop` | Init | Diagnosis: init container failure | Init container exits non-zero |
| `readiness-probe-timeout` | Readiness | Diagnosis: probe/endpoint | Main container Ready=False due to slow probe target |
| `pull-image-slow` | Runtime-creation, escalates | `NeedsNodeAgent` | Image is huge; K8s API doesn't show pull progress; specialist needed |
| `pdb-blocks-eviction` (stretch) | Scheduling | Diagnosis: PDB | Replacement pod can't schedule because PDB blocks eviction of incumbent |
| `oom-killed-loop` (stretch) | Post-start-failure | Diagnosis: OOM/limits | Container killed by kernel OOM, restart loop |

---
