# k8s-debug-agents — Makefile
#
# Convention:
#   Kind-specific wrappers : cluster-up, cluster-down, smoke-test
#   Generic (any cluster)  : istio-install, istio-upgrade, istio-uninstall,
#                            app-install, app-upgrade, app-uninstall
#
# Prod uses the same generic targets (driven by GitOps in reality, but Makefile
# is the reference implementation + escape hatch).

SHELL := /usr/bin/env bash
.ONESHELL:
.DEFAULT_GOAL := help

# ─── Configurable ──────────────────────────────────────────────────────────────
CLUSTER_NAME       ?= debug-agent
K8S_VERSION        ?= v1.33.1
ISTIO_VERSION      ?= 1.24.2

# Namespace topology — see design_namespace_topology.md
PLATFORM_NAMESPACE ?= agent-platform
TASKS_NAMESPACE    ?= agent-tasks
ISTIO_NAMESPACE    ?= istio-system

CHART_DIR          ?= charts/k8s-debug-agents
KIND_CONFIG        ?= evals/kind-config.yaml
SMOKE_POD          ?= evals/smoke-test/pod.yaml
CREDFLOW_DIR       ?= evals/credential-flow
ISTIO_REPO_URL     := https://istio-release.storage.googleapis.com/charts

# Image config — local Kind dev. Empty REGISTRY means images are loaded via
# `kind load docker-image` rather than pushed to a remote registry.
REGISTRY           ?=
IMAGE_TAG          ?= 0.1.0
CREDENTIAL_AUTHZ_IMAGE := k8s-debug-agents/credential-authz:$(IMAGE_TAG)

# ─── Help ──────────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help
	@echo "k8s-debug-agents — make targets"
	@echo ""
	@echo "Kind dev loop:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E '^(cluster-up|cluster-down|smoke-test|build-[a-z-]+|test-[a-z-]+):' | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Generic (any cluster):"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E '^(istio-|app-)' | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Scenario evals (Step 4):"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E '^scenario-' | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Agent (Step 4):"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E '^agent-' | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Security guardrails:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E '^(install-pre-commit|security-scan):' | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Variables (override on command line):"
	@echo "  CLUSTER_NAME=$(CLUSTER_NAME)"
	@echo "  K8S_VERSION=$(K8S_VERSION)"
	@echo "  ISTIO_VERSION=$(ISTIO_VERSION)"
	@echo "  PLATFORM_NAMESPACE=$(PLATFORM_NAMESPACE)"
	@echo "  TASKS_NAMESPACE=$(TASKS_NAMESPACE)"
	@echo "  ISTIO_NAMESPACE=$(ISTIO_NAMESPACE)"

# ─── Kind dev loop ─────────────────────────────────────────────────────────────
.PHONY: cluster-up
cluster-up: ## Create Kind cluster, install Istio + our app, run smoke test
	@./evals/setup.sh

.PHONY: cluster-down
cluster-down: ## Delete the Kind cluster
	@./evals/teardown.sh

.PHONY: smoke-test
smoke-test: ## Re-run the sidecar-injection smoke test against the current cluster
	@echo ">> Running smoke test: deploy pod to $(TASKS_NAMESPACE), verify 2 containers (app + istio-proxy)"
	kubectl apply -f $(SMOKE_POD)
	kubectl wait --for=condition=Ready pod/smoke-test -n $(TASKS_NAMESPACE) --timeout=60s
	@CONTAINERS=$$(kubectl get pod smoke-test -n $(TASKS_NAMESPACE) -o jsonpath='{.spec.containers[*].name}'); \
	COUNT=$$(echo "$$CONTAINERS" | wc -w | tr -d ' '); \
	if [[ "$$COUNT" != "2" ]]; then \
	  echo "FAIL: expected 2 containers, got $$COUNT: $$CONTAINERS"; exit 1; \
	fi; \
	echo "OK: sidecar injected (containers: $$CONTAINERS)"

# ─── Istio (generic — Kind and prod use the same targets) ──────────────────────
# THREE Helm releases, in order: istio-base → istio-cni → istiod.
#
# Why istio-cni: it moves the iptables setup that the per-pod istio-init
# container would normally do into a node-level CNI plugin DaemonSet. This
# eliminates the privileged init container from user pods (which would
# otherwise require CAP_NET_ADMIN + CAP_NET_RAW + runAsUser=0 — capabilities
# forbidden by Pod Security Admission `baseline` and `restricted` profiles).
#
# Without istio-cni, every pod with sidecar injection cannot satisfy hardened
# PSA. With istio-cni, user pods are clean and we can enforce `restricted`
# in agent-system. The privileged work consolidates to one DaemonSet pod per
# node in istio-system.
#
# istiod is configured with `pilot.cni.enabled=true` so it knows the CNI
# plugin will handle iptables setup and skips injecting the istio-init
# container into pods.

.PHONY: istio-install
istio-install: ## Install Istio (istio-base + istio-cni + istiod) via Helm — three separate releases
	@echo ">> Adding Istio Helm repo"
	helm repo add istio $(ISTIO_REPO_URL) --force-update
	helm repo update istio
	@echo ">> Installing istio-base ($(ISTIO_VERSION)) into $(ISTIO_NAMESPACE)"
	helm upgrade --install istio-base istio/base \
	  --version $(ISTIO_VERSION) \
	  --namespace $(ISTIO_NAMESPACE) --create-namespace \
	  --wait
	@echo ">> Installing istio-cni ($(ISTIO_VERSION)) into $(ISTIO_NAMESPACE)"
	@echo ">> (eliminates the privileged istio-init container in user pods)"
	helm upgrade --install istio-cni istio/cni \
	  --version $(ISTIO_VERSION) \
	  --namespace $(ISTIO_NAMESPACE) \
	  --wait
	@echo ">> Installing istiod ($(ISTIO_VERSION)) into $(ISTIO_NAMESPACE)"
	@echo ">> (pilot.cni.enabled=true tells istiod to skip injecting istio-init)"
	helm upgrade --install istiod istio/istiod \
	  --version $(ISTIO_VERSION) \
	  --namespace $(ISTIO_NAMESPACE) \
	  --set pilot.cni.enabled=true \
	  --wait

.PHONY: istio-upgrade
istio-upgrade: ## Upgrade Istio — requires VERSION=x.y.z. Uses SSA on new CRDs before helm upgrade.
	@test -n "$(VERSION)" || (echo "VERSION=x.y.z required (current installed: $(ISTIO_VERSION))"; exit 1)
	@echo ">> Server-Side Applying CRDs for Istio $(VERSION)"
	@echo ">> (Helm 3 does not upgrade CRDs in crds/ on helm upgrade — SSA is the canonical recipe)"
	kubectl apply --server-side -f \
	  https://raw.githubusercontent.com/istio/istio/$(VERSION)/manifests/charts/base/crds/crd-all.gen.yaml
	@echo ">> Bumping istio-base to $(VERSION)"
	helm repo update istio
	helm upgrade istio-base istio/base \
	  --version $(VERSION) \
	  --namespace $(ISTIO_NAMESPACE) \
	  --wait
	@echo ">> Bumping istio-cni to $(VERSION)"
	helm upgrade istio-cni istio/cni \
	  --version $(VERSION) \
	  --namespace $(ISTIO_NAMESPACE) \
	  --wait
	@echo ">> Bumping istiod to $(VERSION)"
	helm upgrade istiod istio/istiod \
	  --version $(VERSION) \
	  --namespace $(ISTIO_NAMESPACE) \
	  --set pilot.cni.enabled=true \
	  --wait

.PHONY: istio-uninstall
istio-uninstall: ## Uninstall Istio (istiod first, then istio-cni, then istio-base — reverse install order)
	@echo ">> Uninstalling istiod"
	-helm uninstall istiod --namespace $(ISTIO_NAMESPACE)
	@echo ">> Uninstalling istio-cni"
	-helm uninstall istio-cni --namespace $(ISTIO_NAMESPACE)
	@echo ">> Uninstalling istio-base"
	-helm uninstall istio-base --namespace $(ISTIO_NAMESPACE)
	@echo ">> Note: CRDs are intentionally NOT deleted — would cascade-delete all Istio CRs cluster-wide."
	@echo ">>       To remove CRDs manually: kubectl delete crd -l release=istio-base"

# ─── Our app (generic — Kind and prod differ only in values.yaml) ──────────────
VALUES_FILE ?= $(CHART_DIR)/values-kind.yaml

.PHONY: app-install
app-install: ## Install k8s-debug-agents chart — override VALUES_FILE for prod
	@echo ">> Installing k8s-debug-agents (values: $(VALUES_FILE))"
	@echo ">> Helm release lives in $(PLATFORM_NAMESPACE); chart also creates $(TASKS_NAMESPACE) for runtime workloads"
	helm upgrade --install k8s-debug-agents $(CHART_DIR) \
	  -f $(VALUES_FILE) \
	  --namespace $(PLATFORM_NAMESPACE) --create-namespace \
	  --wait

.PHONY: app-upgrade
app-upgrade: ## Upgrade k8s-debug-agents chart — override VALUES_FILE for prod
	@echo ">> Upgrading k8s-debug-agents (values: $(VALUES_FILE))"
	helm upgrade k8s-debug-agents $(CHART_DIR) \
	  -f $(VALUES_FILE) \
	  --namespace $(PLATFORM_NAMESPACE) \
	  --wait

.PHONY: app-uninstall
app-uninstall: ## Uninstall k8s-debug-agents chart
	-helm uninstall k8s-debug-agents --namespace $(PLATFORM_NAMESPACE)
	@echo ">> Namespaces $(PLATFORM_NAMESPACE) and $(TASKS_NAMESPACE) intentionally NOT deleted."
	@echo ">>       To remove: kubectl delete namespace $(PLATFORM_NAMESPACE) $(TASKS_NAMESPACE)"

# ─── Image build (Kind dev loop) ───────────────────────────────────────────────

.PHONY: build-images
build-images: build-credential-authz-image ## Build all component images and load into Kind

.PHONY: build-credential-authz-image
build-credential-authz-image: ## Build credential-authz image and kind-load into the dev cluster
	@echo ">> Building $(CREDENTIAL_AUTHZ_IMAGE)"
	docker build \
	  --file docker/credential-authz/Dockerfile \
	  --tag $(CREDENTIAL_AUTHZ_IMAGE) \
	  .
	@echo ">> Loading $(CREDENTIAL_AUTHZ_IMAGE) into Kind cluster $(CLUSTER_NAME)"
	kind load docker-image $(CREDENTIAL_AUTHZ_IMAGE) --name $(CLUSTER_NAME)

# ─── Step 2 verification ───────────────────────────────────────────────────────

.PHONY: test-credential-authz
test-credential-authz: ## Direct gRPC test: hit credential-authz ext_authz endpoint, assert x-api-key in response
	@./evals/credential-authz/check.sh

.PHONY: test-credential-flow
test-credential-flow: ## End-to-end Step 3: a pod in agent-tasks calls Anthropic via Istio + ext_authz; verify the header was injected
	@./evals/credential-flow/check.sh

# ─── Scenario evals (Step 4) ───────────────────────────────────────────────────
# Each scenario is a deterministic broken-pod fixture in evals/scenarios/pod-launch/.
# The fixture YAML creates a dedicated PSA-restricted namespace (eval-<SCENARIO>)
# and a pod that reaches a known stuck state. The paired .expected.yaml file
# specifies the structural assertions a correct agent diagnosis must satisfy.
#
# This is the input to Step 4's eyeball-first → model-as-judge eval loop.
# The agent + harness land in subsequent PRs; this PR ships only the fixtures.

SCENARIO_DIR := evals/scenarios/pod-launch

.PHONY: scenario-apply
scenario-apply: ## Apply a scenario fixture. Usage: make scenario-apply SCENARIO=insufficient-cpu
	@test -n "$(SCENARIO)" || { echo "ERROR: SCENARIO=<name> required (e.g. SCENARIO=insufficient-cpu)"; exit 1; }
	@test -f $(SCENARIO_DIR)/$(SCENARIO).yaml || { echo "ERROR: $(SCENARIO_DIR)/$(SCENARIO).yaml not found"; exit 1; }
	@echo ">> Applying scenario fixture: $(SCENARIO)"
	kubectl apply -f $(SCENARIO_DIR)/$(SCENARIO).yaml
	@echo ">> Waiting up to 60s for pod to reach stuck state in namespace eval-$(SCENARIO)..."
	@for i in $$(seq 1 30); do \
	  PHASE=$$(kubectl get pods -n eval-$(SCENARIO) -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true); \
	  EVENT_COUNT=$$(kubectl get events -n eval-$(SCENARIO) --field-selector reason=FailedScheduling 2>/dev/null | wc -l | tr -d ' '); \
	  if [[ "$$PHASE" == "Pending" && "$$EVENT_COUNT" -gt "1" ]]; then \
	    echo ">> Reached stuck state: phase=Pending with FailedScheduling events"; \
	    break; \
	  fi; \
	  if [[ "$$PHASE" == "Running" ]]; then \
	    echo "WARN: pod is Running — fixture did not reproduce intended stuck state"; \
	    break; \
	  fi; \
	  sleep 2; \
	done
	@echo ""
	@echo ">> Pod state:"
	@kubectl get pods -n eval-$(SCENARIO) -o wide
	@echo ""
	@echo ">> Recent events:"
	@kubectl get events -n eval-$(SCENARIO) --sort-by='.lastTimestamp' | tail -10
	@echo ""
	@echo ">> Expected output spec: $(SCENARIO_DIR)/$(SCENARIO).expected.yaml"
	@echo ">> To clean up: make scenario-clean SCENARIO=$(SCENARIO)"

.PHONY: scenario-clean
scenario-clean: ## Clean up a scenario fixture. Usage: make scenario-clean SCENARIO=insufficient-cpu
	@test -n "$(SCENARIO)" || { echo "ERROR: SCENARIO=<name> required"; exit 1; }
	@echo ">> Deleting namespace eval-$(SCENARIO) and all its resources"
	-kubectl delete namespace eval-$(SCENARIO) --wait=false
	@echo ">> Done (namespace deletion is async; resources will GC shortly)"

.PHONY: scenario-list
scenario-list: ## List available scenario fixtures
	@echo "Available scenarios in $(SCENARIO_DIR):"
	@ls -1 $(SCENARIO_DIR)/*.yaml 2>/dev/null | grep -v '.expected.yaml' | sed 's|$(SCENARIO_DIR)/||; s|\.yaml$$||; s|^|  |'

# ─── Agent (Step 4) ────────────────────────────────────────────────────────────
# Standalone agent runner. Requires ANTHROPIC_API_KEY in env and an active
# kubectl context. See agent-task/README.md for setup details.

AGENT_DIR := agent-task

.PHONY: agent-setup
agent-setup: ## Create venv and install agent-task in editable mode (one-time setup)
	@echo ">> Creating venv at $(AGENT_DIR)/.venv"
	cd $(AGENT_DIR) && python3 -m venv .venv
	@echo ">> Installing agent-task in editable mode"
	cd $(AGENT_DIR) && .venv/bin/pip install -e .
	@echo ""
	@echo "Setup complete. To run the agent, you must export ANTHROPIC_API_KEY first."
	@echo "If your key is in macOS Keychain:"
	@echo "  export ANTHROPIC_API_KEY=\$$(security find-generic-password -a \"\$$USER\" -s \"anthropic-api-key\" -w | tr -d '\\n\\r')"

.PHONY: agent-run
agent-run: ## Run the pod-launch agent. Usage: make agent-run NAMESPACE=foo POD=bar
	@test -n "$(NAMESPACE)" || { echo "ERROR: NAMESPACE=<ns> required"; exit 1; }
	@test -n "$(POD)" || { echo "ERROR: POD=<pod_name> required"; exit 1; }
	@test -n "$$ANTHROPIC_API_KEY" || { \
	  echo "ERROR: ANTHROPIC_API_KEY not set. See agent-task/README.md for setup."; \
	  exit 2; \
	}
	@test -d $(AGENT_DIR)/.venv || { \
	  echo "ERROR: venv not found at $(AGENT_DIR)/.venv. Run 'make agent-setup' first."; \
	  exit 1; \
	}
	@cd $(AGENT_DIR) && .venv/bin/python -m pod_launch_task --namespace $(NAMESPACE) --pod $(POD)

# ─── Security guardrails ───────────────────────────────────────────────────────
# Layer 2 of accidental-secret-checkin defense (Layer 1 = .gitignore patterns,
# Layer 3 = GitHub push protection, Layer 4 = Anthropic dashboard limits).

.PHONY: install-pre-commit
install-pre-commit: ## Install pre-commit framework + activate hooks defined in .pre-commit-config.yaml
	@command -v pre-commit >/dev/null || { \
	  echo ">> pre-commit not installed. Install with: brew install pre-commit (macOS) or pip install pre-commit"; \
	  exit 1; \
	}
	pre-commit install
	@echo ""
	@echo "Hooks activated. Every commit will now scan for secrets via gitleaks."
	@echo "To run hooks against the entire repo (not just staged): pre-commit run --all-files"

.PHONY: security-scan
security-scan: ## Scan the entire working tree for committed secrets (gitleaks)
	@command -v gitleaks >/dev/null || { \
	  echo ">> gitleaks not installed. Install with: brew install gitleaks"; \
	  exit 1; \
	}
	gitleaks detect --source=. --verbose --redact
