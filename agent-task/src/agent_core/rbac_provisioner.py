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

# Per-Job SA name prefix. Each diagnostic invocation gets its own SA
# named `agent-job-<short-uid>` in the target namespace, fully ephemeral
# (owned by the Job, cascade-deleted on Job TTL). No state shared across
# Jobs — concurrent diagnoses in the same namespace each get their own
# identity, Role, RoleBinding, and ClusterRoleBinding.
#
# Tight runtime access to credential-authz is enforced via:
#   - NetworkPolicy on credential-authz pods, restricting ingress to
#     pods labeled `app.kubernetes.io/component: agent-task` +
#     `agent-task/created-by: cli|dispatcher` (see chart's
#     credential-authz/network-policy.yaml).
#   - Istio AuthorizationPolicy (light L7 backstop, presence-match).
#   - Kyverno (PR-16) enforcing label conventions at admission time.
#
# This 4-layer model lets per-Job SA names stay variable without losing
# tight access control — see design_credential_patterns.md (PR-10
# revision) for the rationale.
AGENT_TASK_SA_NAME_PREFIX = "agent-job-"

# Read permissions are NOT mirrored into per-Job Roles anymore. Instead,
# a single shared RoleBinding (`SHARED_READER_ROLEBINDING_NAME`) binds
# the shared SA to the Helm-installed `agent-task-reader` ClusterRole.
# This avoids per-Job re-creation of identical rule sets and lets the
# tool surface evolve in ONE place (chart) without code churn here.
#
# In PR-16, Kyverno enforces that the dispatcher can only create
# RoleBindings referencing this specific ClusterRole — preventing a
# compromised dispatcher from binding `cluster-admin` or any other CR.


@dataclass
class RBACBundle:
    """References to the K8s objects provisioned for one invocation.

    All namespaced objects (SA, Role, RoleBinding, NetworkPolicy, Sidecar)
    are owned by the Job → cascade-deleted by K8s GC on Job TTL.
    ClusterRoleBinding is cluster-scoped (can't be owner-ref'd to a
    namespaced Job) → CLI/dispatcher explicit-deletes on HR completion.
    CRB cleanup uses the unique name; orphan reaping uses the
    agent-task labels.
    """

    sa_name: str
    role_name: str
    rolebinding_name: str
    clusterrolebinding_name: str
    egress_networkpolicy_name: str
    egress_sidecar_name: str
    target_namespace: str


def provision(
    *,
    hr_uid: str,
    hr_name: str,
    target_namespace: str,
    job_owner_ref: client.V1OwnerReference,
    common_labels: dict[str, str],
) -> RBACBundle:
    """Create the four per-Job RBAC objects for one diagnostic invocation.

    Per-Job everything: shared-nothing model. The SA INSTANCE is unique
    per Job (named `agent-job-<short-uid>`); SA + Role + RoleBinding are
    owned by the Job for TTL cascade cleanup; CRB is explicitly cleaned
    up by the CLI/dispatcher on HR completion.

    The Role contains BOTH the read surface (mirroring the Helm-installed
    `agent-task-reader` ClusterRole's rules) AND the narrow HR-status
    update permission scoped via `resourceNames: [hr_name]`. Combining
    both into one per-Job Role keeps the bundle simple and avoids the
    "shared reader-binding" idempotency pattern we briefly tried.

    Multiple concurrent Jobs in the same namespace get distinct SA
    names (`agent-job-<uid1>`, `agent-job-<uid2>`, …) — no name conflict.

    Runtime access to credential-authz is gated by NetworkPolicy +
    Istio AuthorizationPolicy (presence-match) + Kyverno labels — see
    design_credential_patterns.md (PR-10 revision).
    """
    short_uid = hr_uid[:6]
    bundle = RBACBundle(
        sa_name=f"{AGENT_TASK_SA_NAME_PREFIX}{short_uid}",
        role_name=f"agent-job-{short_uid}-role",
        rolebinding_name=f"agent-job-{short_uid}-binding",
        clusterrolebinding_name=f"agent-job-{short_uid}-node-reader",
        egress_networkpolicy_name=f"agent-job-{short_uid}-egress",
        egress_sidecar_name=f"agent-job-{short_uid}-sidecar-egress",
        target_namespace=target_namespace,
    )

    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()
    networking = client.NetworkingV1Api()
    # Istio CRDs aren't in the kubernetes-client/python typed API; use the
    # CustomObjectsApi to create them.
    custom = client.CustomObjectsApi()

    # 1. ServiceAccount — Job pod's runtime identity (K8s API auth + mTLS).
    sa = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name=bundle.sa_name,
            namespace=target_namespace,
            labels=common_labels,
            owner_references=[job_owner_ref],
        ),
        automount_service_account_token=True,
    )
    core.create_namespaced_service_account(namespace=target_namespace, body=sa)

    # 2. Role — combines the read surface (mirrors agent-task-reader
    # ClusterRole) + the narrow HR-status update. resourceNames scopes
    # /status writes to JUST this HR — the SA cannot touch any other HR
    # in the namespace.
    role = client.V1Role(
        metadata=client.V1ObjectMeta(
            name=bundle.role_name,
            namespace=target_namespace,
            labels=common_labels,
            owner_references=[job_owner_ref],
        ),
        rules=[
            client.V1PolicyRule(
                api_groups=[""],
                resources=[
                    "pods",
                    "events",
                    "services",
                    "configmaps",
                    "serviceaccounts",
                    "resourcequotas",
                    "limitranges",
                    "pods/log",
                ],
                verbs=["get", "list"],
            ),
            client.V1PolicyRule(
                api_groups=["apps"],
                resources=["deployments", "replicasets", "statefulsets", "daemonsets"],
                verbs=["get", "list"],
            ),
            client.V1PolicyRule(
                api_groups=["batch"],
                resources=["jobs"],
                verbs=["get", "list"],
            ),
            client.V1PolicyRule(
                api_groups=["policy"],
                resources=["poddisruptionbudgets"],
                verbs=["get", "list"],
            ),
            # Narrow: only this HR's /status, only update/patch.
            client.V1PolicyRule(
                api_groups=["k8s-debug-agents.io"],
                resources=["handoffrequests/status"],
                verbs=["update", "patch"],
                resource_names=[hr_name],
            ),
        ],
    )
    rbac.create_namespaced_role(namespace=target_namespace, body=role)

    # 3. RoleBinding — bind the per-Job SA to the per-Job Role.
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

    # 4. ClusterRoleBinding — narrow Node read. Cluster-scoped, can't be
    # owner-ref'd to a namespaced Job; CLI/dispatcher explicit-deletes
    # on HR completion.
    crb = client.V1ClusterRoleBinding(
        metadata=client.V1ObjectMeta(
            name=bundle.clusterrolebinding_name,
            labels=common_labels,
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

    # 5. PER-JOB egress NetworkPolicy — restricts the agent Job's
    # outbound traffic to: DNS, K8s apiserver, credential-authz, istiod,
    # and external HTTPS (for Anthropic). Everything else is blocked.
    #
    # Selects only THIS Job's pod via the run-uid label (so multiple
    # concurrent Jobs in the same namespace get distinct policies that
    # don't overlap).
    #
    # Owned by the Job → cascade-deleted on TTL alongside SA/Role/RoleBinding.
    run_uid_label = common_labels.get("agent-task/run-uid", short_uid)
    netpol = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(
            name=bundle.egress_networkpolicy_name,
            namespace=target_namespace,
            labels=common_labels,
            owner_references=[job_owner_ref],
        ),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(
                match_labels={"agent-task/run-uid": run_uid_label},
            ),
            policy_types=["Egress"],
            egress=[
                # DNS (kube-dns / coredns lives in kube-system).
                client.V1NetworkPolicyEgressRule(
                    to=[
                        client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={
                                    "kubernetes.io/metadata.name": "kube-system",
                                },
                            ),
                        ),
                    ],
                    ports=[
                        client.V1NetworkPolicyPort(protocol="UDP", port=53),
                        client.V1NetworkPolicyPort(protocol="TCP", port=53),
                    ],
                ),
                # credential-authz (for the Anthropic call path via ext_authz).
                client.V1NetworkPolicyEgressRule(
                    to=[
                        client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={
                                    "kubernetes.io/metadata.name": "agent-platform",
                                },
                            ),
                            pod_selector=client.V1LabelSelector(
                                match_labels={
                                    "app.kubernetes.io/component": "credential-authz",
                                },
                            ),
                        ),
                    ],
                    ports=[
                        client.V1NetworkPolicyPort(protocol="TCP", port=9001),
                    ],
                ),
                # istiod (sidecar needs xDS + workload cert refresh).
                client.V1NetworkPolicyEgressRule(
                    to=[
                        client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={
                                    "kubernetes.io/metadata.name": "istio-system",
                                },
                            ),
                        ),
                    ],
                    ports=[
                        client.V1NetworkPolicyPort(protocol="TCP", port=15012),
                        client.V1NetworkPolicyPort(protocol="TCP", port=15010),
                        client.V1NetworkPolicyPort(protocol="TCP", port=15014),
                    ],
                ),
                # K8s apiserver + external HTTPS (Anthropic).
                #
                # Why combined into one rule with no `to`: NetworkPolicy
                # ipBlock matching evaluates against the POST-DNAT
                # destination (for clusterIP services, that's the actual
                # backend pod IP — which for kube-apiserver is the host-
                # network node IP, not the cluster service CIDR). Across
                # CNIs this varies; some evaluate pre-DNAT. Pinning to
                # specific IPs isn't portable across cluster setups
                # (Kind, EKS, GKE all use different node/service CIDRs).
                #
                # The pragmatic answer: allow egress to ANY destination
                # on the standard HTTPS ports (443, 6443). The TIGHT
                # enforcement for apiserver access is K8s RBAC (per-Job
                # SA's Role only grants read on a specific tool surface).
                # NetworkPolicy still blocks every other port — agent
                # cannot reach random services or alternative outbound
                # ports.
                #
                # 6443 is included because some apiserver setups expose
                # on 6443 directly (post-DNAT for in-cluster Service).
                #
                # Wave-A hardening: chartify with `apiserverEndpoints`
                # value so production can pin specific IPs.
                client.V1NetworkPolicyEgressRule(
                    ports=[
                        client.V1NetworkPolicyPort(protocol="TCP", port=443),
                        client.V1NetworkPolicyPort(protocol="TCP", port=6443),
                    ],
                ),
                # External plaintext HTTP (port 80) — for the in-pod
                # plaintext request to api.anthropic.com that the Istio
                # sidecar then TLS-originates to 443. The application
                # uses HTTP base_url; iptables redirects to istio-proxy
                # which makes the actual outbound HTTPS connection. From
                # NetworkPolicy's perspective, the pod's outbound port-80
                # traffic targets api.anthropic.com's IP (which is public).
                # Restrict to public IPs only.
                client.V1NetworkPolicyEgressRule(
                    to=[
                        client.V1NetworkPolicyPeer(
                            ip_block=client.V1IPBlock(
                                cidr="0.0.0.0/0",
                                _except=[
                                    "10.0.0.0/8",
                                    "172.16.0.0/12",
                                    "192.168.0.0/16",
                                ],
                            ),
                        ),
                    ],
                    ports=[
                        client.V1NetworkPolicyPort(protocol="TCP", port=80),
                    ],
                ),
            ],
        ),
    )
    networking.create_namespaced_network_policy(
        namespace=target_namespace, body=netpol
    )

    # 6. PER-JOB Istio Sidecar resource — PRIMARY egress restriction at
    # the mesh layer. Lists the destinations Envoy will route to;
    # everything else gets dropped (UpstreamFailure / UF). Service-name
    # based so it works across cluster setups without IP hardcoding.
    #
    # Why both Sidecar AND NetworkPolicy: belt-and-suspenders.
    #   - Sidecar (L7, mesh): tight by service name, primary gate
    #   - NetworkPolicy (L3/L4, CNI): kernel-level backstop, catches
    #     anything that somehow bypassed the sidecar
    #
    # Hosts the agent legitimately needs:
    #   - apiserver (kubernetes.default.svc) — kubectl_* tools + hr_writer
    #   - credential-authz — ext_authz substitution (the ext_authz call
    #     is made by Istio sidecar internally, not the app — but the
    #     sidecar still needs the cluster known via Sidecar resource)
    #   - istiod / istio-system — sidecar xDS + cert refresh (Istio also
    #     allows control-plane traffic automatically, but explicit doesn't
    #     hurt)
    #   - kube-dns — DNS resolution
    #   - api.anthropic.com — Anthropic API (via the ServiceEntry in
    #     istio-system, see chart/.../istio-mesh/service-entry.yaml)
    sidecar_cr = {
        "apiVersion": "networking.istio.io/v1",
        "kind": "Sidecar",
        "metadata": {
            "name": bundle.egress_sidecar_name,
            "namespace": target_namespace,
            "labels": common_labels,
            "ownerReferences": [
                {
                    "apiVersion": job_owner_ref.api_version,
                    "kind": job_owner_ref.kind,
                    "name": job_owner_ref.name,
                    "uid": job_owner_ref.uid,
                    "controller": True,
                    "blockOwnerDeletion": True,
                },
            ],
        },
        "spec": {
            "workloadSelector": {
                "labels": {"agent-task/run-uid": run_uid_label},
            },
            "egress": [
                {
                    "hosts": [
                        # K8s apiserver
                        "default/kubernetes.default.svc.cluster.local",
                        # ext_authz target service
                        "agent-platform/credential-authz.agent-platform.svc.cluster.local",
                        # Istio control plane (xDS + workload cert SDS)
                        "istio-system/*",
                        # DNS resolver
                        "kube-system/kube-dns.kube-system.svc.cluster.local",
                        # External Anthropic (registered as ServiceEntry
                        # in istio-system per PR-10 chart move)
                        "istio-system/api.anthropic.com",
                    ],
                },
            ],
        },
    }
    custom.create_namespaced_custom_object(
        group="networking.istio.io",
        version="v1",
        namespace=target_namespace,
        plural="sidecars",
        body=sidecar_cr,
    )

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
