"""Job-creation primitive — shared by the CLI today and the dispatcher controller tomorrow.

Functions here build + submit + watch + clean up the K8s Job that runs
one variant invocation. The caller (CLI in PR-10, dispatcher in PR-16)
differs only in *trigger*: human invocation vs CR reconcile. Below the
trigger, the lifecycle is identical.

Public surface:
    create_handoff_request(spec, target_ns, common_labels) -> dict
        Creates the HR CR in the target namespace. Returns the created
        object (including .metadata.uid the caller needs for naming).

    build_variant_job(variant, cli_args, image, sa_name, hr_uid, hr_name,
                       hr_namespace, target_ns, common_labels, hr_owner_ref)
                       -> V1Job
        Builds (does NOT submit) the Job spec. Pure construction; lets
        the caller inspect the spec before submission for debugging.

    submit_job(job, target_ns) -> V1Job
        POSTs to BatchV1Api. Returns the server-assigned Job (with uid).

    watch_hr_status(hr_name, target_ns, timeout_seconds) -> dict
        Blocks until HR.status.phase is Completed or Failed (or timeout).
        Returns the final HR object.

    delete_hr(hr_name, target_ns)
        Optional. CLI does NOT call this — HR persists as history. The
        dispatcher controller in PR-16 may call it as part of DR cleanup.

Init container: every variant Pod includes a `rbac-wait` init container
that sleeps 2s to let the apiserver's RBAC authorizer cache propagate
the per-Job Role/RoleBinding. Upgradable to `kubectl auth can-i` polling
later; sleep is the simplest workable v0 per the design discussion.

The Job spec carries restricted-PSA-compliant securityContext on every
container (init + agent), so it admits in any namespace regardless of
PSA enforce level.
"""
from __future__ import annotations

import time
from typing import Any

from kubernetes import client, watch  # type: ignore[import-untyped]

CRD_GROUP = "k8s-debug-agents.io"
CRD_VERSION = "v1"
CRD_PLURAL = "handoffrequests"

# Init container parameters. busybox is tiny, ships with sleep; PSA-restricted
# acceptable because we set securityContext explicitly.
INIT_IMAGE = "busybox:1.36"
RBAC_PROPAGATION_SLEEP_SECONDS = 2

# Job spec defaults. ttl_seconds_after_finished gives a window for the
# CLI/dispatcher to read final status; afterwards K8s GC cascades through
# to the SA/Role/RoleBinding owned by the Job.
JOB_TTL_SECONDS_AFTER_FINISHED = 300

# Resource ask. Generous enough for the agent loop's spikes (Anthropic SDK
# does HTTP/2 mux + JSON serdes); tight enough to fit into modest namespace
# quotas. Spike-tolerant via limit > request.
AGENT_CPU_REQUEST = "100m"
AGENT_CPU_LIMIT = "1"
AGENT_MEM_REQUEST = "256Mi"
AGENT_MEM_LIMIT = "1Gi"


def create_handoff_request(
    *,
    variant: str,
    target_namespace: str,
    pod_name: str,
    hr_name: str,
    common_labels: dict[str, str],
) -> dict[str, Any]:
    """Create the HandoffRequest CR. Returns the server-assigned object
    so the caller can extract .metadata.uid for naming the rest of the
    per-invocation resources.

    HR lives in the same namespace as the pod being diagnosed (per-target
    model). The CLI/dispatcher specifies the name explicitly (typically
    derived from a uuid) so observability tooling can correlate logs +
    HRs without watching for generateName collisions.
    """
    api = client.CustomObjectsApi()
    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "HandoffRequest",
        "metadata": {
            "name": hr_name,
            "namespace": target_namespace,
            "labels": common_labels,
        },
        "spec": {
            "variant": variant,
            "target": {
                "namespace": target_namespace,
                "podName": pod_name,
            },
        },
    }
    return api.create_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=target_namespace,
        plural=CRD_PLURAL,
        body=body,
    )


def build_variant_job(
    *,
    variant: str,
    cli_args: list[str],
    image: str,
    sa_name: str,
    hr_uid: str,
    hr_name: str,
    hr_namespace: str,
    target_namespace: str,
    common_labels: dict[str, str],
    hr_owner_ref: client.V1OwnerReference,
    job_name: str,
) -> client.V1Job:
    """Construct the Job spec without submitting.

    Pod labels MUST include:
      - `sidecar.istio.io/inject: "true"`     (Pattern B Istio selective injection)
      - `app.kubernetes.io/component: agent-task`  (EnvoyFilter workloadSelector)
      - `agent-task/variant: <variant>`       (observability + future Kyverno match)

    Pod has NO env vars for credentials — the Anthropic SDK ships the
    placeholder x-api-key header; Istio ext_authz substitutes the real
    key per the Step-3 credential pipeline.

    Pod has TWO containers:
      - initContainer `rbac-wait`: sleeps 2s for RBAC propagation
      - container `agent`: runs `agent-runner <variant> <args>`

    Both containers carry restricted-PSA-compliant securityContext so
    the Pod admits in any namespace's PSA level.
    """
    # Common securityContext for both containers — meets PSA `restricted`.
    restricted_security_context = client.V1SecurityContext(
        run_as_non_root=True,
        run_as_user=65532,
        allow_privilege_escalation=False,
        read_only_root_filesystem=True,
        capabilities=client.V1Capabilities(drop=["ALL"]),
        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
    )

    pod_labels = {
        **common_labels,
        "sidecar.istio.io/inject": "true",
        "app.kubernetes.io/component": "agent-task",
        "agent-task/variant": variant,
    }

    init_container = client.V1Container(
        name="rbac-wait",
        image=INIT_IMAGE,
        command=["sleep", str(RBAC_PROPAGATION_SLEEP_SECONDS)],
        resources=client.V1ResourceRequirements(
            requests={"cpu": "10m", "memory": "16Mi"},
            limits={"cpu": "100m", "memory": "32Mi"},
        ),
        security_context=restricted_security_context,
    )

    agent_container = client.V1Container(
        name="agent",
        image=image,
        command=["agent-runner"],
        # --quiet is a top-level flag on agent-runner (git-style); it MUST
        # appear before the subcommand. argparse rejects the reverse order.
        args=["--quiet", variant] + cli_args,
        env=[
            # HR identity — used by hr_writer to PATCH the right object.
            client.V1EnvVar(name="HR_NAME", value=hr_name),
            client.V1EnvVar(name="HR_NAMESPACE", value=hr_namespace),
            # Anthropic SDK reads ANTHROPIC_API_KEY — we set the placeholder
            # explicitly so the SDK puts it in the x-api-key header. Istio
            # ext_authz substitutes the real key before the request leaves
            # the pod.
            #
            # The placeholder string here exactly matches what the Step-3
            # test-pod uses + what gitleaks's allowlist whitelists in
            # .gitleaks.toml. Kyverno (PR-16) will require this exact value
            # and reject any other env so a compromised dispatcher can't
            # smuggle in a real key.
            client.V1EnvVar(name="ANTHROPIC_API_KEY", value="PLACEHOLDER-DO-NOT-USE"),
            # ANTHROPIC_BASE_URL: force HTTP so the request stays plaintext
            # inside the Pod's netns until Envoy intercepts. Envoy's
            # ext_authz filter can only L7-inspect/modify plaintext HTTP —
            # if the SDK uses HTTPS direct, the credential substitution
            # path is bypassed and the request fails (real key never gets
            # injected). The Step-3 DestinationRule then originates TLS to
            # api.anthropic.com:443. See istio-mesh/destination-rule.yaml.
            client.V1EnvVar(name="ANTHROPIC_BASE_URL", value="http://api.anthropic.com"),
        ],
        resources=client.V1ResourceRequirements(
            requests={"cpu": AGENT_CPU_REQUEST, "memory": AGENT_MEM_REQUEST},
            limits={"cpu": AGENT_CPU_LIMIT, "memory": AGENT_MEM_LIMIT},
        ),
        security_context=restricted_security_context,
        # /tmp is the one writable directory the agent needs (Python's
        # __pycache__, anthropic SDK's caching, etc.). readOnlyRootFilesystem
        # requires us to mount an emptyDir for any writable path.
        volume_mounts=[
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
        ],
    )

    pod_spec = client.V1PodSpec(
        service_account_name=sa_name,
        restart_policy="Never",
        host_network=False,
        host_pid=False,
        host_ipc=False,
        init_containers=[init_container],
        containers=[agent_container],
        volumes=[
            client.V1Volume(name="tmp", empty_dir=client.V1EmptyDirVolumeSource()),
        ],
        security_context=client.V1PodSecurityContext(
            run_as_non_root=True,
            run_as_user=65532,
            run_as_group=65532,
            fs_group=65532,
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        ),
    )

    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=target_namespace,
            labels=common_labels,
            owner_references=[hr_owner_ref],
        ),
        spec=client.V1JobSpec(
            backoff_limit=0,  # all-or-nothing per design_variant_retry_semantics
            ttl_seconds_after_finished=JOB_TTL_SECONDS_AFTER_FINISHED,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=pod_labels),
                spec=pod_spec,
            ),
        ),
    )
    return job


def submit_job(job: client.V1Job, target_namespace: str) -> client.V1Job:
    """POST the Job to the apiserver. Returns the server-assigned object."""
    batch = client.BatchV1Api()
    return batch.create_namespaced_job(namespace=target_namespace, body=job)


def watch_hr_status(
    *,
    hr_name: str,
    target_namespace: str,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Block until HR.status.phase reaches a terminal state.

    Uses the K8s watch API with a per-iteration timeout to avoid the
    "watch connection closed by intermediate proxy" failure mode. Restarts
    the watch from the last observed resourceVersion on reconnect.

    Returns the final HR object. Raises TimeoutError if the cap elapses
    before a terminal phase is observed.
    """
    api = client.CustomObjectsApi()
    deadline = time.monotonic() + timeout_seconds

    # First fetch — handles the case where the agent has already written
    # status before the watch starts (rare but possible for fast paths).
    current = api.get_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=target_namespace,
        plural=CRD_PLURAL,
        name=hr_name,
    )
    phase = (current.get("status") or {}).get("phase")
    if phase in ("Completed", "Failed"):
        return current

    resource_version = current["metadata"]["resourceVersion"]

    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        w = watch.Watch()
        try:
            for event in w.stream(
                api.list_namespaced_custom_object,
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=target_namespace,
                plural=CRD_PLURAL,
                field_selector=f"metadata.name={hr_name}",
                resource_version=resource_version,
                timeout_seconds=remaining,
            ):
                obj = event["object"]
                resource_version = obj["metadata"]["resourceVersion"]
                phase = (obj.get("status") or {}).get("phase")
                if phase in ("Completed", "Failed"):
                    w.stop()
                    return obj
        except client.ApiException as e:
            if e.status == 410:
                # resourceVersion too old — refetch and reset.
                current = api.get_namespaced_custom_object(
                    group=CRD_GROUP,
                    version=CRD_VERSION,
                    namespace=target_namespace,
                    plural=CRD_PLURAL,
                    name=hr_name,
                )
                resource_version = current["metadata"]["resourceVersion"]
                phase = (current.get("status") or {}).get("phase")
                if phase in ("Completed", "Failed"):
                    return current
                continue
            raise

    raise TimeoutError(
        f"HandoffRequest {target_namespace}/{hr_name} did not reach Completed/Failed "
        f"within {timeout_seconds}s; last observed phase={phase!r}"
    )


def get_hr(hr_name: str, target_namespace: str) -> dict[str, Any]:
    """Fetch HR by name. Thin wrapper for readability at call sites."""
    api = client.CustomObjectsApi()
    return api.get_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=target_namespace,
        plural=CRD_PLURAL,
        name=hr_name,
    )


def hr_owner_ref(hr_object: dict[str, Any]) -> client.V1OwnerReference:
    """Build a V1OwnerReference pointing at the given HR.

    Used as the owner_references entry on the Job spec so Job (+ its
    cascade-children: Pod, SA, Role, RoleBinding) all delete when the HR
    is deleted.
    """
    return client.V1OwnerReference(
        api_version=f"{CRD_GROUP}/{CRD_VERSION}",
        kind="HandoffRequest",
        name=hr_object["metadata"]["name"],
        uid=hr_object["metadata"]["uid"],
        # block_owner_deletion: K8s won't delete the HR until its
        # dependents are deleted first (foreground cascade). controller:
        # marks this as the controlling owner.
        block_owner_deletion=True,
        controller=True,
    )


def job_owner_ref(job_object: client.V1Job) -> client.V1OwnerReference:
    """Build a V1OwnerReference pointing at the given Job.

    Used on SA/Role/RoleBinding so K8s GC cascades them when the Job's
    TTL fires.
    """
    return client.V1OwnerReference(
        api_version="batch/v1",
        kind="Job",
        name=job_object.metadata.name,
        uid=job_object.metadata.uid,
        block_owner_deletion=True,
        controller=True,
    )
