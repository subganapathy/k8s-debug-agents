"""Programmatic per-invocation RBAC provisioning.

For each diagnostic invocation the CLI (today) or dispatcher (PR-16)
creates a fresh ephemeral RBAC bundle scoped tightly to the target
namespace plus a narrow cluster-scope binding for the one resource
kind that can't be expressed namespace-scoped (Node).

Created objects, all owned by the Job (so they cascade-delete when
the Job hits its ttlSecondsAfterFinished and K8s GC walks the
ownership graph):

    ServiceAccount   agent-job-<uid>            (in target namespace)
    Role             agent-job-<uid>-reader     (mirrors agent-task-reader,
                                                 namespace-scoped)
    RoleBinding      agent-job-<uid>-binding    (binds SA → Role)

Plus one cluster-scoped object that cannot be owner-ref'd to a
namespaced Job (K8s GC restricts cluster-scoped → namespaced ownership):

    ClusterRoleBinding agent-job-<uid>-node-reader   (binds SA → agent-node-reader)

The CRB is explicitly deleted by the CLI/dispatcher when the HR
transitions to Completed/Failed. Labels carry the HR uid so orphan
cleanup is straightforward (`make eval-cleanup` greps by label).

Design memos this implements:
    - design_namespace_topology.md      (per-target Jobs, not centralized)
    - design_kyverno_pr16_scope.md      (naming convention agent-job-<uid>-*,
                                         content matches agent-task-reader;
                                         Kyverno enforces both in PR-16)

The agent-task-reader ClusterRole + agent-node-reader ClusterRole are
Helm-installed (policy definition is platform-managed; per-invocation
bindings are programmatic). The Role this module creates is a
namespace-scoped *copy* of agent-task-reader's rules — same shape,
narrower scope. Plus one rule that grants update on the specific HR's
/status subresource (so the agent can write findings).
"""
from __future__ import annotations

from dataclasses import dataclass

from kubernetes import client  # type: ignore[import-untyped]

# Rules mirrored from the Helm-installed `agent-task-reader` ClusterRole.
# Keep this in sync with charts/k8s-debug-agents/templates/agent-task/
# clusterroles.yaml. In PR-16, Kyverno will enforce that any Role created
# by the dispatcher has rules byte-equal to the canonical ClusterRole —
# this hardcoded list is the local-side echo of the same contract.
#
# K8s Python client uses snake_case (api_groups, not apiGroups) when
# constructing V1PolicyRule. The dicts below match that convention.
AGENT_TASK_READER_RULES: list[dict] = [
    {
        "api_groups": [""],
        "resources": [
            "pods",
            "events",
            "services",
            "configmaps",
            "serviceaccounts",
            "resourcequotas",
            "limitranges",
            "pods/log",
        ],
        "verbs": ["get", "list"],
    },
    {
        "api_groups": ["apps"],
        "resources": ["deployments", "replicasets", "statefulsets", "daemonsets"],
        "verbs": ["get", "list"],
    },
    {
        "api_groups": ["batch"],
        "resources": ["jobs"],
        "verbs": ["get", "list"],
    },
    {
        "api_groups": ["policy"],
        "resources": ["poddisruptionbudgets"],
        "verbs": ["get", "list"],
    },
]


@dataclass
class RBACBundle:
    """References to the K8s objects provisioned for one invocation.

    The CLI / dispatcher carries this around until cleanup. SA, Role,
    RoleBinding are GC'd via Job ownership; only the ClusterRoleBinding
    needs explicit delete.
    """

    sa_name: str
    role_name: str
    rolebinding_name: str
    clusterrolebinding_name: str
    target_namespace: str


def provision(
    *,
    hr_uid: str,
    hr_name: str,
    target_namespace: str,
    job_owner_ref: client.V1OwnerReference,
    common_labels: dict[str, str],
) -> RBACBundle:
    """Create the four RBAC objects for one invocation.

    `hr_uid` provides the short suffix in object names (first 6 hex chars
    of HR's metadata.uid). `hr_name` is referenced in the Role's HR-status
    rule (so the SA can only update its own HR's status, not any other).
    `job_owner_ref` is an OwnerReference pointing at the Job — applied
    to SA/Role/RoleBinding so K8s GC cascades them on Job deletion. The
    CRB is created WITHOUT this owner ref (cluster-scoped restriction).

    `common_labels` should include `agent-task/created-by: cli` (or
    `dispatcher` in PR-16), `agent-task/hr-uid: <uid>`, and any other
    discoverability labels.

    Returns the bundle the caller uses for cleanup tracking.
    """
    short_uid = hr_uid[:6]
    bundle = RBACBundle(
        sa_name=f"agent-job-{short_uid}",
        role_name=f"agent-job-{short_uid}-reader",
        rolebinding_name=f"agent-job-{short_uid}-binding",
        clusterrolebinding_name=f"agent-job-{short_uid}-node-reader",
        target_namespace=target_namespace,
    )

    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()

    # 1. ServiceAccount — Job pod's runtime identity.
    sa = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name=bundle.sa_name,
            namespace=target_namespace,
            labels=common_labels,
            owner_references=[job_owner_ref],
        ),
        # Default automounting is fine — the projected SA token is what
        # hr_writer uses to authenticate to the apiserver for status writes.
        automount_service_account_token=True,
    )
    core.create_namespaced_service_account(namespace=target_namespace, body=sa)

    # 2. Role — namespace-scoped mirror of agent-task-reader, plus the
    # narrow HR-status update rule.
    role_rules = list(AGENT_TASK_READER_RULES) + [
        {
            "api_groups": ["k8s-debug-agents.io"],
            "resources": ["handoffrequests/status"],
            "verbs": ["update", "patch"],
            # CRITICAL: scope to JUST this HR. The SA cannot touch any
            # other HandoffRequest's status, even one in the same namespace.
            "resource_names": [hr_name],
        },
    ]
    role = client.V1Role(
        metadata=client.V1ObjectMeta(
            name=bundle.role_name,
            namespace=target_namespace,
            labels=common_labels,
            owner_references=[job_owner_ref],
        ),
        rules=[client.V1PolicyRule(**r) for r in role_rules],
    )
    rbac.create_namespaced_role(namespace=target_namespace, body=role)

    # 3. RoleBinding — bind the SA to the Role.
    binding = client.V1RoleBinding(
        metadata=client.V1ObjectMeta(
            name=bundle.rolebinding_name,
            namespace=target_namespace,
            labels=common_labels,
            owner_references=[job_owner_ref],
        ),
        subjects=[
            client.RbacV1Subject(
                kind="ServiceAccount",
                name=bundle.sa_name,
                namespace=target_namespace,
            ),
        ],
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="Role",
            name=bundle.role_name,
        ),
    )
    rbac.create_namespaced_role_binding(namespace=target_namespace, body=binding)

    # 4. ClusterRoleBinding — bind SA to the standing agent-node-reader
    # ClusterRole. Cluster-scoped, cannot owner-ref a namespaced Job;
    # the CLI/dispatcher must explicitly delete it on HR completion.
    crb = client.V1ClusterRoleBinding(
        metadata=client.V1ObjectMeta(
            name=bundle.clusterrolebinding_name,
            labels=common_labels,
            # No owner_references — K8s GC would refuse them.
        ),
        subjects=[
            client.RbacV1Subject(
                kind="ServiceAccount",
                name=bundle.sa_name,
                namespace=target_namespace,
            ),
        ],
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="ClusterRole",
            name="agent-node-reader",
        ),
    )
    rbac.create_cluster_role_binding(body=crb)

    return bundle


def cleanup_clusterrolebinding(name: str) -> None:
    """Delete a per-invocation ClusterRoleBinding. Idempotent (404 OK).

    Called by the CLI / dispatcher when the HR transitions to a terminal
    phase. The other three RBAC objects (SA, Role, RoleBinding) get
    cleaned up automatically by K8s GC when the Job's
    ttlSecondsAfterFinished fires; this is the one cleanup that can't
    cascade via ownership.
    """
    rbac = client.RbacAuthorizationV1Api()
    try:
        rbac.delete_cluster_role_binding(name=name)
    except client.ApiException as e:
        if e.status != 404:
            raise


def cleanup_orphan_crbs(label_selector: str = "agent-task/created-by") -> int:
    """Best-effort cleanup of orphaned per-Job ClusterRoleBindings.

    Belt-and-suspenders for when the CLI crashes before its explicit
    cleanup ran. Used by `make eval-cleanup`. Returns count of CRBs
    deleted.
    """
    rbac = client.RbacAuthorizationV1Api()
    crbs = rbac.list_cluster_role_binding(label_selector=label_selector)
    count = 0
    for crb in crbs.items:
        if not (crb.metadata.name or "").startswith("agent-job-"):
            continue
        try:
            rbac.delete_cluster_role_binding(name=crb.metadata.name)
            count += 1
        except client.ApiException as e:
            if e.status != 404:
                raise
    return count
