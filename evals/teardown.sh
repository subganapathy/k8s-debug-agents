#!/usr/bin/env bash
# evals/teardown.sh
#
# Deletes the Kind cluster created by setup.sh.
# Invoked by `make cluster-down`.

set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-debug-agent}"

if ! command -v kind >/dev/null 2>&1; then
  echo "ERROR: kind not in PATH"; exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  echo "==> Deleting Kind cluster '${CLUSTER_NAME}'"
  kind delete cluster --name "${CLUSTER_NAME}"
else
  echo "==> Kind cluster '${CLUSTER_NAME}' does not exist — nothing to do"
fi
