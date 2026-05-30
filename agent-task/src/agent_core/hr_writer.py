"""Write findings + metrics to HandoffRequest.status from inside a variant Job.

The variant Job's container (via `agent_core.runner`) calls
`write_findings(...)` after the agent loop completes successfully, or
`write_failure(...)` if the loop raises before producing a result. Both
use the K8s API to PATCH the HandoffRequest's /status subresource —
which is the only field the Job's SA has write permission for (its
per-Job Role is scoped to `handoffrequests/status` with `resourceNames:
[the HR name]`, see `agent_core.rbac_provisioner`).

We use the K8s Python client's in-cluster config — kubelet projects the
Job's SA token into the pod, the client reads it via the standard
`/var/run/secrets/kubernetes.io/serviceaccount/...` path. Same code
works in any conformant K8s cluster; no kubeconfig involved.

Why pydantic serialization, not raw dicts: `AgentResult.model_dump(
mode='json')` produces exactly the shape declared in
`HandoffRequestStatus` (because the underlying types are shared —
Findings and Metrics are the same classes). The CRD's openAPIV3Schema
will validate any divergence at write time and reject the PATCH; we
prefer that loud failure over silently writing a malformed status.
"""
from __future__ import annotations

import os
import sys

from kubernetes import client, config  # type: ignore[import-untyped]

from agent_core.schemas import AgentResult, Findings, Metrics

CRD_GROUP = "k8s-debug-agents.io"
CRD_VERSION = "v1"
CRD_PLURAL = "handoffrequests"


def _load_config() -> None:
    """Pick the right K8s auth path.

    Inside a pod: load_incluster_config() reads the projected SA token.
    Local dev (running agent-runner directly without a pod, for debug):
    fall back to kubeconfig. This makes the runner debuggable on a laptop
    without needing to fully containerize first.
    """
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
    else:
        config.load_kube_config()


def write_findings(
    *,
    hr_name: str,
    hr_namespace: str,
    result: AgentResult,
    phase: str = "Completed",
) -> None:
    """PATCH HR.status with the variant's findings + metrics + phase.

    Called by `agent_core.runner` on the successful path. The patch is
    a strategic merge limited to the /status subresource, so it cannot
    touch HR.spec (the per-Job Role doesn't grant that anyway).

    Raises if the K8s API call fails. The caller (runner.py) propagates
    the exception, which the agent's exit code surfaces to the Job's
    .status.containerStatuses[*].state.terminated for the dispatcher /
    CLI watcher to observe.
    """
    _load_config()
    api = client.CustomObjectsApi()

    status_payload = {
        "phase": phase,
        # mode='json' ensures enums / Decimal / etc. are serialized to
        # JSON-native types — same encoding the CRD's openAPIV3Schema
        # was generated from.
        "findings": result.findings.model_dump(mode="json") if result.findings else None,
        "metrics": result.metrics.model_dump(mode="json"),
    }

    api.patch_namespaced_custom_object_status(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=hr_namespace,
        plural=CRD_PLURAL,
        name=hr_name,
        body={"status": status_payload},
    )
    sys.stderr.write(
        f"[hr_writer] PATCHed {hr_namespace}/{hr_name} status -> phase={phase}\n"
    )


def write_failure(*, hr_name: str, hr_namespace: str, reason: str) -> None:
    """PATCH HR.status with a Failed phase + minimal Findings/Metrics shell.

    Called by `agent_core.runner` when the agent loop raises before
    producing a result. We synthesize a low-confidence Findings carrying
    the failure reason so downstream consumers (eval verifier, dispatcher)
    have something to read rather than a null status.

    Metrics is a near-empty Metrics record — wall_clock=0.0 because we
    have no monotonic-clock context here. The dispatcher and eval
    harness already handle the case where metrics are mostly zero
    (deterministic variants emit the same shape).
    """
    failure_findings = Findings(
        problem=f"agent runner failed before producing findings: {reason}",
        confidence="low",
        evidence=[],
        remediations=[],
        improvements=[],
        alsoCheck=[
            "Inspect the Job's pod logs for the full traceback.",
            "Check whether the variant's required env vars (HR_NAME, HR_NAMESPACE) were set.",
        ],
    )
    failure_metrics = Metrics(wall_clock_seconds=0.0, termination="runner_exception")

    result = AgentResult(findings=failure_findings, metrics=failure_metrics)
    write_findings(
        hr_name=hr_name,
        hr_namespace=hr_namespace,
        result=result,
        phase="Failed",
    )
