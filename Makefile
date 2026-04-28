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
AGENT_NAMESPACE    ?= agent-system
ISTIO_NAMESPACE    ?= istio-system
CHART_DIR          ?= charts/k8s-debug-agents
KIND_CONFIG        ?= evals/kind-config.yaml
SMOKE_POD          ?= evals/smoke-test/pod.yaml
ISTIO_REPO_URL     := https://istio-release.storage.googleapis.com/charts

# ─── Help ──────────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help
	@echo "k8s-debug-agents — make targets"
	@echo ""
	@echo "Kind dev loop:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E '^(cluster-up|cluster-down|smoke-test):' | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Generic (any cluster):"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | grep -E '^(istio-|app-)' | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Variables (override on command line):"
	@echo "  CLUSTER_NAME=$(CLUSTER_NAME)"
	@echo "  K8S_VERSION=$(K8S_VERSION)"
	@echo "  ISTIO_VERSION=$(ISTIO_VERSION)"
	@echo "  AGENT_NAMESPACE=$(AGENT_NAMESPACE)"
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
	@echo ">> Running smoke test: deploy pod to $(AGENT_NAMESPACE), verify 2 containers (app + istio-proxy)"
	kubectl apply -f $(SMOKE_POD)
	kubectl wait --for=condition=Ready pod/smoke-test -n $(AGENT_NAMESPACE) --timeout=60s
	@CONTAINERS=$$(kubectl get pod smoke-test -n $(AGENT_NAMESPACE) -o jsonpath='{.spec.containers[*].name}'); \
	COUNT=$$(echo "$$CONTAINERS" | wc -w | tr -d ' '); \
	if [[ "$$COUNT" != "2" ]]; then \
	  echo "FAIL: expected 2 containers, got $$COUNT: $$CONTAINERS"; exit 1; \
	fi; \
	echo "OK: sidecar injected (containers: $$CONTAINERS)"

# ─── Istio (generic — Kind and prod use the same targets) ──────────────────────
.PHONY: istio-install
istio-install: ## Install Istio (istio-base + istiod) via Helm — two separate releases
	@echo ">> Adding Istio Helm repo"
	helm repo add istio $(ISTIO_REPO_URL) --force-update
	helm repo update istio
	@echo ">> Installing istio-base ($(ISTIO_VERSION)) into $(ISTIO_NAMESPACE)"
	helm upgrade --install istio-base istio/base \
	  --version $(ISTIO_VERSION) \
	  --namespace $(ISTIO_NAMESPACE) --create-namespace \
	  --wait
	@echo ">> Installing istiod ($(ISTIO_VERSION)) into $(ISTIO_NAMESPACE)"
	helm upgrade --install istiod istio/istiod \
	  --version $(ISTIO_VERSION) \
	  --namespace $(ISTIO_NAMESPACE) \
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
	@echo ">> Bumping istiod to $(VERSION)"
	helm upgrade istiod istio/istiod \
	  --version $(VERSION) \
	  --namespace $(ISTIO_NAMESPACE) \
	  --wait

.PHONY: istio-uninstall
istio-uninstall: ## Uninstall Istio (istiod first, then istio-base)
	@echo ">> Uninstalling istiod"
	-helm uninstall istiod --namespace $(ISTIO_NAMESPACE)
	@echo ">> Uninstalling istio-base"
	-helm uninstall istio-base --namespace $(ISTIO_NAMESPACE)
	@echo ">> Note: CRDs are intentionally NOT deleted — would cascade-delete all Istio CRs cluster-wide."
	@echo ">>       To remove CRDs manually: kubectl delete crd -l release=istio-base"

# ─── Our app (generic — Kind and prod differ only in values.yaml) ──────────────
VALUES_FILE ?= $(CHART_DIR)/values-kind.yaml

.PHONY: app-install
app-install: ## Install k8s-debug-agents chart — override VALUES_FILE for prod
	@echo ">> Installing k8s-debug-agents (values: $(VALUES_FILE))"
	helm upgrade --install k8s-debug-agents $(CHART_DIR) \
	  -f $(VALUES_FILE) \
	  --namespace $(AGENT_NAMESPACE) --create-namespace \
	  --wait

.PHONY: app-upgrade
app-upgrade: ## Upgrade k8s-debug-agents chart — override VALUES_FILE for prod
	@echo ">> Upgrading k8s-debug-agents (values: $(VALUES_FILE))"
	helm upgrade k8s-debug-agents $(CHART_DIR) \
	  -f $(VALUES_FILE) \
	  --namespace $(AGENT_NAMESPACE) \
	  --wait

.PHONY: app-uninstall
app-uninstall: ## Uninstall k8s-debug-agents chart
	-helm uninstall k8s-debug-agents --namespace $(AGENT_NAMESPACE)
	@echo ">> Namespace $(AGENT_NAMESPACE) intentionally NOT deleted — would remove any unrelated workloads."
	@echo ">>       To remove: kubectl delete namespace $(AGENT_NAMESPACE)"
