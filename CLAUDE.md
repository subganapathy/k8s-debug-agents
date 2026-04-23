# k8s-debug-agents — Claude Code Context

Repo: https://github.com/subganapathy/k8s-debug-agents
Architecture document (source of truth, lives in a sibling repo):
  `~/code/small-step-giant-leap/pod-launch-lifecycle/ARCHITECTURE.md`

Memory system for this project (auto-loaded):
  `~/.claude/projects/-Users-subramanianganapathy-code-small-step-giant-leap-pod-launch-lifecycle/memory/`

## What this is

A Kubernetes-native debugging agent system. Classifies alerts, dispatches fine-grained
agent-tasks that use the Anthropic SDK to diagnose infrastructure issues.

## 5 components

1. **Dispatcher** (Python + HTTP API) — receives alerts, classifies them, launches
   agent-task Jobs with scoped RBAC. Exposes `POST /v1/diagnose` as the system's
   primary interface. Slack / Prometheus / etc. are thin adapters on top.
2. **Agent-Task** (Python, ephemeral Job) — runs the Anthropic SDK tool-use loop.
   Structured as Pattern A: triage (K8s API) with an escape-hatch tool that hands
   off to specialists (CRI/CNI/volume) wired in Phase 2 as sub-agents-as-tools
   running in the same pod.
3. **Node-Agent** (Go DaemonSet) — privileged, one per node. Exposes CRI/CNI/CSI
   state over UDS with SO_PEERCRED auth.
4. **Credential-Authz** (Go Deployment) — holds the Anthropic API key, implements
   Envoy ext_authz gRPC. Injects `x-api-key` into outbound requests from the sidecar.
5. **Istio** — sidecar injection + mTLS + ext_authz filter for credential flow.

## Agent output contract

Every agent-task produces a `Diagnosis`: `problem`, `evidence`, `confidence`,
`remediations` (ranked, with tradeoffs + risk), `also_check` (pointers for deeper
investigation — text in Phase 1, specialist invocations in Phase 2+).

Evals grade on four dimensions: root cause correctness, remediation specificity,
side-effects / risk accuracy, also_check relevance.

## Install flow

Three Helm releases: `istio-base`, `istiod`, `k8s-debug-agents`. Kind and prod use
the same releases; only values.yaml differs. Makefile targets (`istio-install`,
`istio-upgrade`, `app-install`, etc.) are the dev interface and the reference
implementation for what GitOps (ArgoCD) will do in prod.

**`istio-upgrade` uses Server-Side Apply** on new CRDs before `helm upgrade`, because
Helm 3 doesn't upgrade CRDs in `crds/` on `helm upgrade`. See the Makefile recipe.

## Credential flow

1. Agent-task calls Anthropic SDK with `api_key="placeholder"`, `base_url="http://..."`
2. Istio sidecar iptables-redirects outbound to `api.anthropic.com`
3. Sidecar's ext_authz filter sends gRPC CheckRequest to `credential-authz`
4. `credential-authz` reads API key from K8s Secret mount, returns it as header
5. Sidecar injects `x-api-key`, originates TLS, forwards to Anthropic
6. All hops mTLS-encrypted (Istio) or TLS-encrypted (to Anthropic)

Agent-task pod carries zero static secrets. Rotation: update Secret, propagates in
~6 minutes, no pod restart.

## Dispatcher API (primary interface)

```
POST /v1/diagnose               # trigger diagnosis; sync (wait=true) or async (202 + ID)
GET  /v1/diagnoses/{id}         # fetch status / result
GET  /healthz /readyz

CLI wrapper: bin/k8s-debug-agents diagnose --alert-type X --namespace Y --pod Z
Makefile:    make diagnose POD=... NS=... ALERT=...
```

Slack / Alertmanager adapters come later as thin translators to this API.

## Phase 1 scope (current)

Two agent-task variants, K8s API only:
- `pod-slow-to-launch` (Stages 1-7, T1→T5)
- `intra-cluster-traffic-not-flowing` (Path A — ClusterIP/Service/EndpointSlice/CNI)

Both are triage agents with `report_needs_node_agent(stage, handoff_ctx)` escape hatch
that becomes `invoke_{cri,cni,volume}_specialist` in Phase 2. Handoff context schema
designed now so specialists plug in without rewriting.

AWS LBC / external-traffic variant is **Phase 2 (next agent of this project)**.
Metric-specialist (Prometheus/CloudWatch historical queries) is **Phase 3**.

## Build order

1. **Kind cluster + Istio** ← current step
2. Credential-authz service (Go, ext_authz)
3. Istio config (EnvoyFilter, ServiceEntry, DestinationRule, PeerAuthentication)
4. Dispatcher + two triage variants (K8s API only, Pattern A shape)
5. Evals (K8s API only, four-dimension grading)
6. Node-agent scaffold (DaemonSet, UDS listener)
7. Tier 1 CRI client + cri-specialist
8. Tier 2 containerd deep inspector
9. CNI interface + cni-specialist (Cilium first)
10. SO_PEERCRED auth (verify caller is a kubepods cgroup member)
11. RBAC cleanup CronJob

## Chart structure

Single umbrella chart at `charts/k8s-debug-agents/` with component subdirectories
under `templates/` (node-agent/, credential-authz/, dispatcher/). One shared agent-task
Docker image with a `VARIANT` env var (prompts and variant-specific config mounted
via ConfigMap). Agent-tasks are **runtime artifacts** (created by the dispatcher per
alert), not install-time chart resources.

## Deployment

- **Dev (Kind)**: `make cluster-up`. That's it.
- **Prod**: deferred until post-e2e (after Step 6). Expected shape is ArgoCD
  ApplicationSet + cluster-label selector — same mechanism for install, backfill,
  and upgrade. BYO Istio (fleet-managed separately). See `design_argocd_primer.md`
  in memory.

## Workflow rules (from memory)

- **Stacked PRs only.** Every change is its own reviewable PR.
- **NO PR MERGED WITHOUT USER REVIEW** — absolute rule; learning mechanism.
- **Vibe-code + review every artifact** — Claude writes, user reviews deeply.
  Review is the primary learning mechanism, not a rubber stamp.
- **Restart resilience**: decisions land in durable files immediately. PR
  descriptions are self-contained narratives. `current_state.md` tracks build-order
  progress. On restart, read `MEMORY.md` first, then follow pointers.

## Gotchas

- **containerd namespace**: always use `"k8s.io"` — `ctx = namespaces.WithNamespace(ctx, "k8s.io")`
- **Istio base_url must be HTTP, not HTTPS** — so Envoy sidecar can do L7 ext_authz inspection; sidecar then originates TLS outbound
- **Events age out at 1h** by default — triage agent prefers `pod.status` fields
  (conditions, containerStatuses, waiting.reason, lastState.terminated) over events
- **No PR merges without review** — repeat of the workflow rule because it matters
