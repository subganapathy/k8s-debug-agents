#!/usr/bin/env bash
# evals/setup.sh
#
# Brings up a Kind cluster + Istio + k8s-debug-agents chart + runs the smoke test.
# Idempotent: safe to run against an existing cluster (skips what's already there).
#
# Invoked by `make cluster-up`.

set -euo pipefail

# ─── Config (inherits from Makefile, with sane defaults if run standalone) ─────
CLUSTER_NAME="${CLUSTER_NAME:-debug-agent}"
ISTIO_VERSION="${ISTIO_VERSION:-1.24.2}"
PLATFORM_NAMESPACE="${PLATFORM_NAMESPACE:-agent-platform}"
TASKS_NAMESPACE="${TASKS_NAMESPACE:-agent-tasks}"
ISTIO_NAMESPACE="${ISTIO_NAMESPACE:-istio-system}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KIND_CONFIG="${SCRIPT_DIR}/kind-config.yaml"
CHART_DIR="${REPO_ROOT}/charts/k8s-debug-agents"
VALUES_FILE="${CHART_DIR}/values-kind.yaml"

echo "==> k8s-debug-agents dev bring-up"
echo "    cluster: ${CLUSTER_NAME}"
echo "    istio:   ${ISTIO_VERSION}"
echo ""

# ─── Preflight: check required tools ───────────────────────────────────────────
require() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not in PATH"; exit 1; }
}
require kind
require kubectl
require helm
require docker

# ─── 1. Create Kind cluster (idempotent) ───────────────────────────────────────
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  echo "==> Kind cluster '${CLUSTER_NAME}' already exists, skipping create"
else
  echo "==> Creating Kind cluster '${CLUSTER_NAME}'"
  kind create cluster --name "${CLUSTER_NAME}" --config "${KIND_CONFIG}"
fi

# Ensure kubectl context is pointed at our cluster (kind sets this, but be
# explicit so we fail loud if someone has a different context set by hand).
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null

# ─── 2. Install Istio via Helm (THREE separate releases) ──────────────────────
# Order matters: istio-base (CRDs) → istio-cni (CNI plugin DaemonSet) → istiod
# (control plane, configured to skip istio-init container injection).
#
# Why three not two: istio-cni eliminates the privileged per-pod istio-init
# container so user namespaces can enforce hardened Pod Security Admission
# (restricted profile). Without it, every pod with sidecar injection requires
# CAP_NET_ADMIN + CAP_NET_RAW + runAsUser=0 in its init container — capabilities
# that PSA `baseline` and `restricted` profiles forbid.
#
# pilot.cni.enabled=true on istiod tells the sidecar injector to skip
# istio-init since the CNI plugin handles iptables setup at the node level.
echo "==> Adding Istio Helm repo"
helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update >/dev/null
helm repo update istio >/dev/null

echo "==> Installing istio-base ${ISTIO_VERSION} into ${ISTIO_NAMESPACE}"
helm upgrade --install istio-base istio/base \
  --version "${ISTIO_VERSION}" \
  --namespace "${ISTIO_NAMESPACE}" --create-namespace \
  --wait

echo "==> Installing istio-cni ${ISTIO_VERSION} into ${ISTIO_NAMESPACE}"
helm upgrade --install istio-cni istio/cni \
  --version "${ISTIO_VERSION}" \
  --namespace "${ISTIO_NAMESPACE}" \
  --wait

echo "==> Installing istiod ${ISTIO_VERSION} into ${ISTIO_NAMESPACE}"
helm upgrade --install istiod istio/istiod \
  --version "${ISTIO_VERSION}" \
  --namespace "${ISTIO_NAMESPACE}" \
  --set pilot.cni.enabled=true \
  --wait

# ─── 3. Install our chart (creates agent-platform + agent-tasks namespaces) ────
# Helm release lives in agent-platform; the chart's tasks-namespace.yaml template
# creates agent-tasks alongside it. Both namespaces get PSA restricted + Istio
# injection labels.
echo "==> Installing k8s-debug-agents chart (Helm release in ${PLATFORM_NAMESPACE})"
helm upgrade --install k8s-debug-agents "${CHART_DIR}" \
  -f "${VALUES_FILE}" \
  --namespace "${PLATFORM_NAMESPACE}" --create-namespace \
  --wait

# ─── 4. Smoke test: deploy a pod into agent-tasks, verify sidecar injection ────
echo "==> Smoke test: verifying sidecar injection works in ${TASKS_NAMESPACE}"

# Clean up any stale smoke pod (idempotency)
kubectl delete pod smoke-test -n "${TASKS_NAMESPACE}" --ignore-not-found --wait=true >/dev/null

kubectl apply -f "${SCRIPT_DIR}/smoke-test/pod.yaml"
kubectl wait --for=condition=Ready pod/smoke-test \
  -n "${TASKS_NAMESPACE}" --timeout=60s

CONTAINERS=$(kubectl get pod smoke-test -n "${TASKS_NAMESPACE}" \
  -o jsonpath='{.spec.containers[*].name}')
COUNT=$(echo "${CONTAINERS}" | wc -w | tr -d ' ')

if [[ "${COUNT}" != "2" ]]; then
  echo ""
  echo "FAIL: expected 2 containers (app + istio-proxy), got ${COUNT}"
  echo "      containers: ${CONTAINERS}"
  echo ""
  echo "      Common cause: namespace missing 'istio-injection=enabled' label"
  echo "      Check:       kubectl get ns ${TASKS_NAMESPACE} --show-labels"
  exit 1
fi

echo ""
echo "OK: sidecar injected — containers: ${CONTAINERS}"
echo ""
echo "==> Done."
echo ""
echo "Next:"
echo "  kubectl get pod smoke-test -n ${TASKS_NAMESPACE}"
echo "  make cluster-down    # when you're done"
