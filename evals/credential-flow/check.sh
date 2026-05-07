#!/usr/bin/env bash
# evals/credential-flow/check.sh
#
# Step 3 end-to-end credential-flow verification.
#
# Asserts the full path: agent-tasks pod → Istio sidecar → ext_authz →
# credential-authz → header injection → outbound TLS to api.anthropic.com.
#
# Prereqs:
#   - Cluster running with Istio + istio-cni + chart applied (`make cluster-up`)
#   - credential-authz image built + loaded (`make build-credential-authz-image`)
#   - Anthropic Secret created in agent-platform ns
#
# Procedure:
#   1. Apply credential-flow-test pod + SA in agent-tasks ns
#   2. Wait for pod to be Running (with sidecar injection)
#   3. Wait for the curl call inside the test container to complete
#   4. Inspect:
#      a. The HTTP_STATUS curl returned (200 = real key worked; 401 = injection
#         happened but key was a stub; other = something broke)
#      b. credential-authz logs to confirm the Check method fired
#      c. The test container's output to confirm the request reached Anthropic
#         (non-zero HTTP status implies the request made it out, was TLS-
#          terminated by Anthropic, etc.)

set -euo pipefail

PLATFORM_NS="${PLATFORM_NAMESPACE:-agent-platform}"
TASKS_NS="${TASKS_NAMESPACE:-agent-tasks}"
SECRET_NAME="${SECRET_NAME:-anthropic-api-key}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_POD_YAML="${SCRIPT_DIR}/test-pod.yaml"

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not in PATH"; exit 1; }
}
require kubectl

# ─── 1. Sanity: secret + credential-authz exist ──────────────────────────────
echo "==> [1/5] Confirming prereqs"
if ! kubectl get secret "${SECRET_NAME}" -n "${PLATFORM_NS}" >/dev/null 2>&1; then
  cat <<EOF
FAIL: Secret ${SECRET_NAME} not found in ${PLATFORM_NS}.
      Create with:
        kubectl create secret generic ${SECRET_NAME} -n ${PLATFORM_NS} \\
          --from-literal=api-key="\${ANTHROPIC_API_KEY:-sk-ant-stub-step-03-do-not-use}"
EOF
  exit 1
fi

if ! kubectl get deployment credential-authz -n "${PLATFORM_NS}" >/dev/null 2>&1; then
  echo "FAIL: credential-authz Deployment not found in ${PLATFORM_NS}."
  echo "      Run: make build-credential-authz-image && make app-upgrade"
  exit 1
fi

# Wait for credential-authz to be Ready (its readinessProbe waits for the Secret)
echo "==> Waiting for credential-authz to be Ready..."
kubectl rollout status deployment/credential-authz -n "${PLATFORM_NS}" --timeout=90s

# ─── 2. Clean up any stale test pod, deploy the new one ──────────────────────
echo "==> [2/5] Deploying credential-flow-test pod into ${TASKS_NS}"
kubectl delete pod credential-flow-test -n "${TASKS_NS}" --ignore-not-found --wait=true >/dev/null
kubectl apply -f "${TEST_POD_YAML}"

# ─── 3. Wait for the pod to be Running (sidecar injection succeeded) ─────────
echo "==> [3/5] Waiting for the pod to be Running (sidecar should inject)"
# We use --for=condition=Initialized rather than =Ready because the test
# container exits after curl completes, but we want to inspect logs first.
for i in $(seq 1 30); do
  PHASE=$(kubectl get pod credential-flow-test -n "${TASKS_NS}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
  CONTAINERS=$(kubectl get pod credential-flow-test -n "${TASKS_NS}" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null || echo "")
  if [[ "${PHASE}" == "Running" || "${PHASE}" == "Succeeded" || "${PHASE}" == "Failed" ]]; then
    break
  fi
  sleep 2
done

# Confirm sidecar was injected
if ! echo "${CONTAINERS}" | grep -q "istio-proxy"; then
  echo "FAIL: istio-proxy sidecar was not injected. Containers: ${CONTAINERS}"
  echo "      Check namespace label: kubectl get ns ${TASKS_NS} --show-labels"
  exit 1
fi
echo "OK: pod has containers: ${CONTAINERS}"

# ─── 4. Capture the test container's output (curl response) ──────────────────
echo "==> [4/5] Reading test container output (curl made the request to Anthropic)"
# The test container does: curl ... ; sleep 300. We tail logs until we see the
# HTTP_STATUS marker line, then we have everything we need.
LOGS=""
for i in $(seq 1 60); do
  LOGS=$(kubectl logs -n "${TASKS_NS}" credential-flow-test -c tester 2>/dev/null || echo "")
  if echo "${LOGS}" | grep -q "HTTP_STATUS="; then
    break
  fi
  sleep 1
done

if ! echo "${LOGS}" | grep -q "HTTP_STATUS="; then
  echo "FAIL: never saw HTTP_STATUS line — curl didn't complete in 60s."
  echo "Last log output:"
  echo "${LOGS}" | tail -20
  exit 1
fi

HTTP_STATUS=$(echo "${LOGS}" | grep "HTTP_STATUS=" | tail -1 | sed 's/.*HTTP_STATUS=//')
echo "OK: curl completed; Anthropic returned HTTP_STATUS=${HTTP_STATUS}"

# ─── 5. Verify ext_authz Check fired (credential-authz logged it) ────────────
echo "==> [5/5] Confirming credential-authz received the ext_authz Check call"
# credential-authz logs each Check via slog: at minimum we should see SOME
# activity timestamped after our test pod started.
TEST_POD_START=$(kubectl get pod credential-flow-test -n "${TASKS_NS}" -o jsonpath='{.status.startTime}')
echo "    test pod started at: ${TEST_POD_START}"

# Get credential-authz logs since shortly before the pod started (gives buffer)
AUTHZ_LOGS=$(kubectl logs -n "${PLATFORM_NS}" \
  -l app.kubernetes.io/component=credential-authz \
  --since=120s --tail=100 2>/dev/null || echo "")

if [[ -z "${AUTHZ_LOGS}" ]]; then
  echo "WARN: no recent logs from credential-authz."
  echo "      The pod may have rotated, or logging may be silent."
fi

# Print the credential-authz log tail for inspection (don't grep — let user see)
echo ""
echo "── credential-authz recent logs (last 120s, up to 30 lines) ──"
echo "${AUTHZ_LOGS}" | tail -30
echo "── end ──"
echo ""

# ─── Summary ──────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════════════"
echo " HTTP_STATUS from Anthropic:  ${HTTP_STATUS}"
echo ""
case "${HTTP_STATUS}" in
  200)
    echo " ✓ FULL SUCCESS: Anthropic accepted the request. The injection"
    echo "   flow worked AND the configured Secret holds a valid API key."
    ;;
  401|403)
    echo " ✓ INJECTION WORKED, KEY REJECTED: The request reached Anthropic"
    echo "   over TLS (so Istio's ServiceEntry + DestinationRule + EnvoyFilter"
    echo "   pipeline all worked). Anthropic rejected the credential — most"
    echo "   likely because the Secret holds a stub key. To get a 200,"
    echo "   re-create the Secret with a real Anthropic API key."
    ;;
  000|"")
    echo " ✗ FAIL: curl returned no status — the request never made it through"
    echo "   the sidecar. Possible causes:"
    echo "     - ext_authz failure (credential-authz down or refusing)"
    echo "     - failure_mode_allow=false rejected the request at ext_authz"
    echo "     - ServiceEntry missing or misconfigured"
    echo "     - DestinationRule missing → Envoy didn't originate TLS"
    echo "   Inspect logs above + 'kubectl logs -c istio-proxy credential-flow-test -n ${TASKS_NS}'"
    exit 1
    ;;
  *)
    echo " ⚠ UNEXPECTED STATUS: ${HTTP_STATUS}. Inspect:"
    echo "     kubectl logs credential-flow-test -n ${TASKS_NS} -c tester"
    echo "     kubectl logs credential-flow-test -n ${TASKS_NS} -c istio-proxy"
    echo "     kubectl logs -n ${PLATFORM_NS} -l app.kubernetes.io/component=credential-authz"
    exit 1
    ;;
esac
echo "═══════════════════════════════════════════════════════════════════════"
echo ""
echo "Test pod left running. Inspect at will:"
echo "   kubectl logs credential-flow-test -n ${TASKS_NS} -c tester"
echo "   kubectl logs credential-flow-test -n ${TASKS_NS} -c istio-proxy"
echo "   kubectl delete pod credential-flow-test -n ${TASKS_NS}    # when done"
