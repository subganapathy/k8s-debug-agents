# k8s-debug-agents

Kubernetes-native debugging agents that classify alerts and dispatch fine-grained
agent-tasks to diagnose infrastructure issues. Built with the Anthropic Agent SDK.

## What this is

An ops tool that, when given an alert (pod slow to launch, traffic not flowing, etc.),
produces a `Diagnosis` — root cause + evidence + ranked remediations with tradeoffs +
pointers for deeper investigation. Agents run as Kubernetes Jobs in the target cluster,
use the K8s API directly for control-plane signals, and delegate runtime / node-level
inspection to a privileged node-agent DaemonSet.

## Status

**Phase 1, Step 1.** Kind cluster + Istio scaffolding only. See `CLAUDE.md` for the
full build plan and architecture pointers.

## Repo layout

```
charts/k8s-debug-agents/   # single umbrella Helm chart (component subdirectories under templates/)
evals/                     # Kind cluster config, setup scripts, smoke-test pod, future scenarios
docker/                    # one subdirectory per image (filled in as components are built)
docs/                      # API specs, runbooks (filled in later steps)
Makefile                   # cluster-up / cluster-down / istio-install / app-install / ...
```

Empty component directories (`node-agent/`, `credential-authz/`, `dispatcher/`, `agent-task/`)
will be populated in subsequent stacked PRs — see the build order in `CLAUDE.md`.

## Quick start (after Step 1)

```bash
make cluster-up      # creates Kind cluster, installs Istio, applies our chart, runs smoke test
make smoke-test      # re-runs the sidecar-injection verification
make cluster-down    # deletes the Kind cluster
```

Requirements: `kind`, `kubectl`, `helm` (v3+), `docker`.

## Install flow (dev and prod, same pattern)

Three Helm releases, orchestrated by Makefile targets. Kind and prod differ only in
values.yaml. See `CLAUDE.md` for rationale and `docs/` (later) for the prod runbook.

```
istio-base    (Istio CRDs + cluster resources, in istio-system)
istiod        (Istio control plane,            in istio-system)
k8s-debug-agents  (our app,                    in agent-system)
```

## Contributing

Every change is a stacked PR reviewed before merge. See `.github/pull_request_template.md`
for the required PR description discipline.
