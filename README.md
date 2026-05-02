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

## Quick start (after Step 2)

```bash
# 1. Bring up Kind cluster + Istio + chart (credential-authz pods come up NotReady)
make cluster-up

# 2. Build the credential-authz image and load it into Kind
make build-credential-authz-image

# 3. Re-deploy the chart so it uses the just-built image
make app-upgrade

# 4. Create the Anthropic Secret separately (chart never sees the key)
kubectl create secret generic anthropic-api-key \
  --namespace=agent-system \
  --from-literal=api-key="${ANTHROPIC_API_KEY:-sk-ant-stub-step-02-do-not-use}"

# 5. Verify end-to-end: file watcher picks up Secret, gRPC contract returns x-api-key, rotation works without restart
make test-credential-authz

# Tear down
make cluster-down
```

Requirements: `kind`, `kubectl`, `helm` (v3+), `docker`, `go` (1.23+), `grpcurl`.

## Security guardrails (set up once)

API keys leaking into git can cause runaway costs. We use defense in depth:

**Layer 1 — `.gitignore` patterns** (already configured in this repo).

**Layer 2 — pre-commit hook running [gitleaks](https://github.com/gitleaks/gitleaks)** (one-time setup, then automatic):

```bash
brew install pre-commit gitleaks    # macOS; equivalent on other OSes
make install-pre-commit              # activates the hook
```

After this, every `git commit` scans staged changes and rejects secrets before they enter local history. Also: `make security-scan` runs gitleaks against the full working tree on demand.

**Layer 3 — GitHub push protection** (one-time, ~2 min, server-side safety net):

1. Open https://github.com/subganapathy/k8s-debug-agents/settings/security_analysis
2. **Enable** "Secret scanning"
3. **Enable** "Push protection"
4. (Recommended) **Enable** "Require justification when bypassing push protection"

When triggered, GitHub server-side rejects pushes containing recognized secret patterns (Anthropic, OpenAI, AWS, etc.).

**Layer 4 — Anthropic dashboard limits** (one-time, ~5 min, blast-radius reduction):

1. Console → Settings → Workspaces → create `dev-k8s-debug-agents`
2. Workspace → Limits → set monthly cap (e.g. **$50** for dev) + email alerts at 25/50/75%
3. API Keys → create dev-only key, scope to that workspace, save in a password manager
4. Rotation runbook: revoke → create new → `kubectl create secret … --dry-run=client -o yaml | kubectl apply -f -` → file watcher picks up the new key without pod restart

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
