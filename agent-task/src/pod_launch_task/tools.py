"""Tool definitions + executor + bootstrap helpers.

Tools are the agent's interface to the world. Each tool definition has:
- name: short identifier; the LLM emits this in its tool_use blocks.
- description: tells the model WHEN to use it (not just what it does),
  and enumerates the supported kinds.
- input_schema: JSON Schema for the tool's parameters.

Design: two parametric resource-access tools (kubectl_read, kubectl_list)
plus a logs tool and the emit_findings sentinel. Each parametric tool
takes a `kind` enum and dispatches in code; unsupported kinds are
rejected at the executor with a clear error. Restricting kinds to the
ones this task actually needs keeps the LLM's tool choice constrained
and predictable, while still leaving room to extend per task variant
(a future intra_cluster_traffic_task can add NetworkPolicy to the
kubectl_list enum without touching anything else).

K8s access uses the Python `kubernetes` client (CoreV1Api for built-ins,
dynamic client for everything else). Single code path for in-cluster
(load_incluster_config) and laptop (load_kube_config).

Four tools:
- `kubectl_read`: fetch a single named resource. Kinds: Pod, Deployment,
  ReplicaSet, StatefulSet, DaemonSet, Job, PersistentVolumeClaim,
  ConfigMap, Secret (.data stripped), ServiceAccount, Service,
  PodDisruptionBudget, ResourceQuota, LimitRange.
- `kubectl_list`: list resources of a given kind, with kind-specific
  filters. Kinds: Pod (filters: namespace/node_name/label_selector/phase),
  Node (no filters), Event (filters: namespace/involved_object_name/
  involved_object_kind), PodDisruptionBudget (filter: pod_name for
  server-side selector match against the pod's labels; without pod_name,
  lists all in the namespace), ResourceQuota (namespace only),
  LimitRange (namespace only).
- `kubectl_get_container_logs`: stdout/stderr from a container,
  `previous=true` for crashloop analysis. Distinct from kubectl_read
  because logs are a subresource, not the resource itself.
- `emit_findings`: pseudo-tool. When the model calls this, the agent loop
  terminates and the tool's input becomes the final Findings. This is the
  "forced structured output" pattern — the model can't end the
  investigation without producing a structured Findings.

Implementation note: the "kubectl_" prefix is the LLM contract (familiar
concept); the implementation uses the Python K8s API client, not the
kubectl subprocess.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config, dynamic
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import (
    NotFoundError as DynNotFoundError,
    ResourceNotFoundError,
)

from pod_launch_task.findings import Findings

# ─── Supported kinds + apiVersion map ──────────────────────────────────────────

_READ_KINDS = [
    "Pod",
    "Deployment",
    "ReplicaSet",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "PersistentVolumeClaim",
    "ConfigMap",
    "Secret",
    "ServiceAccount",
    "Service",
    "PodDisruptionBudget",
    "ResourceQuota",
    "LimitRange",
]

_LIST_KINDS = [
    "Pod",
    "Node",
    "Event",
    "PodDisruptionBudget",
    "ResourceQuota",
    "LimitRange",
]

_KIND_TO_API_VERSION = {
    "Pod": "v1",
    "Deployment": "apps/v1",
    "ReplicaSet": "apps/v1",
    "StatefulSet": "apps/v1",
    "DaemonSet": "apps/v1",
    "Job": "batch/v1",
    "PersistentVolumeClaim": "v1",
    "ConfigMap": "v1",
    "Secret": "v1",
    "ServiceAccount": "v1",
    "Service": "v1",
    "PodDisruptionBudget": "policy/v1",
    "ResourceQuota": "v1",
    "LimitRange": "v1",
}

# ─── Tool definitions (consumed by Anthropic's `tools` parameter) ──────────────

KUBECTL_READ_TOOL = {
    "name": "kubectl_read",
    "description": (
        "Read a single named Kubernetes resource by kind + namespace + name. "
        "Use after you have identified a specific resource via an event, pod "
        "spec reference, owner reference, or a prior kubectl_list call.\n\n"
        "Default filtering (applied to EVERY kind): `metadata.annotations` "
        "and `metadata.managedFields` are stripped to reduce prompt-injection "
        "surface and noise. Per-kind additional filtering noted below.\n\n"
        "Supported kinds (enforced at the executor — unsupported kinds are "
        "rejected with an error):\n"
        " - Pod: additionally drops deep owner chains (keeps only the "
        "immediate parent in `ownerReferences`).\n"
        " - Deployment, ReplicaSet, StatefulSet, DaemonSet, Job: workload "
        "controllers. Fetch when investigating ADMISSION-REJECTED "
        "('the ReplicaSet can't create pods'), rollout state, or owner "
        "references.\n"
        " - PersistentVolumeClaim: storage binding state (`.status.phase`, "
        "`.spec.storageClassName`, etc.).\n"
        " - ConfigMap: full configuration data (note: `metadata.annotations` "
        "is filtered, but `.data` is NOT — that's the actual config payload).\n"
        " - Secret: metadata + type only — `.data` and `.stringData` are "
        "ALSO stripped (in addition to annotations / managedFields) for "
        "security. You can confirm existence and type "
        "(kubernetes.io/dockerconfigjson, Opaque, etc.) but not values.\n"
        " - ServiceAccount: identity used by pods.\n"
        " - Service: in-cluster networking config.\n"
        " - PodDisruptionBudget, ResourceQuota, LimitRange: when you "
        "already know the resource's name. To DISCOVER which PDB / quota / "
        "LimitRange applies to a pod or namespace, use `kubectl_list` "
        "with the appropriate kind."
    ),
    "input_schema": {
        "type": "object",
        "required": ["kind", "namespace", "name"],
        "properties": {
            "kind": {"type": "string", "enum": _READ_KINDS},
            "namespace": {"type": "string"},
            "name": {"type": "string"},
        },
    },
}

KUBECTL_LIST_TOOL = {
    "name": "kubectl_list",
    "description": (
        "List Kubernetes resources of a given kind. Each kind below "
        "documents its applicable filter fields; filters that don't apply "
        "to the chosen kind are ignored by the executor.\n\n"
        "Supported kinds:\n"
        " - Pod: list pods, optionally filtered by `namespace`, `node_name` "
        "(`spec.nodeName` — useful for 'who's consuming this node'), "
        "`label_selector` (K8s syntax, e.g. 'app=hog'), or `phase`. "
        "Returns compact summaries (name, ns, node, phase, owner, labels, "
        "container CPU/memory requests). Critical for diagnosing PDB-"
        "induced scheduling pressure: list pods on a full node, see which "
        "workloads they belong to, then list PodDisruptionBudget for those "
        "workloads.\n"
        " - Node (cluster-scoped — no namespace needed): list all nodes "
        "with labels, taints, allocatable, capacity, conditions. Essential "
        "for affinity / toleration / resource-shortage diagnoses where you "
        "need to name specific nodes.\n"
        " - Event: list events in a namespace, optionally filtered by "
        "`involved_object_name` + `involved_object_kind`. For ADMISSION-"
        "REJECTED diagnoses, use `involved_object_kind`=Deployment or "
        "ReplicaSet to find 'FailedCreate' events (admission blocking pod "
        "creation). For SCHEDULING failures, use kind=Pod to find "
        "FailedScheduling events. Events are sorted oldest → newest.\n"
        " - PodDisruptionBudget: lists PDBs in the namespace. When "
        "`pod_name` is provided, the executor server-side label-matches "
        "each PDB's `.spec.selector` against the pod's labels and returns "
        "ONLY the matching ones, with `.spec.minAvailable`/`maxUnavailable` "
        "and `.status` (currentHealthy, desiredHealthy, disruptionsAllowed). "
        "Use this to DISCOVER which PDB (if any) restricts a workload's "
        "eviction — PDB names are not predictable from pod names. Without "
        "`pod_name`, lists all PDBs in the namespace.\n"
        " - ResourceQuota: lists all ResourceQuotas in a namespace with "
        "`.status.used` vs `.spec.hard`. Confirms whether the namespace is "
        "at quota and which dimensions (cpu / memory / pods / etc.) are "
        "exhausted.\n"
        " - LimitRange: lists all LimitRanges in a namespace with "
        "defaults / defaultRequests / min / max per resource. Reveals "
        "whether default-injected requests are silently consuming quota."
    ),
    "input_schema": {
        "type": "object",
        "required": ["kind"],
        "properties": {
            "kind": {"type": "string", "enum": _LIST_KINDS},
            "namespace": {
                "type": "string",
                "description": "Namespace. Required for all kinds except Node. Omit for Node.",
            },
            "node_name": {
                "type": "string",
                "description": "kind=Pod only: filter by spec.nodeName.",
            },
            "label_selector": {
                "type": "string",
                "description": "kind=Pod only: K8s label selector, e.g. 'app=hog,tier=backend'.",
            },
            "phase": {
                "type": "string",
                "enum": ["Pending", "Running", "Succeeded", "Failed", "Unknown"],
                "description": "kind=Pod only: filter by status.phase.",
            },
            "involved_object_name": {
                "type": "string",
                "description": "kind=Event only: filter by involvedObject.name.",
            },
            "involved_object_kind": {
                "type": "string",
                "description": "kind=Event only: filter by involvedObject.kind (Pod, Deployment, ReplicaSet, …). Recommended for precision.",
            },
            "pod_name": {
                "type": "string",
                "description": "kind=PodDisruptionBudget only: when provided, server-side selector-match PDBs against this pod's labels and return only matches.",
            },
        },
    },
}

KUBECTL_GET_LOGS_TOOL = {
    "name": "kubectl_get_container_logs",
    "description": (
        "Fetch recent stdout/stderr log lines from a specific container in "
        "a specific pod. Use when the diagnostic phase is POST-START "
        "(CrashLoopBackOff, Error, OOMKilled, or any case where the "
        "container has produced output that could explain the failure). "
        "Set `previous=true` to fetch logs from the PREVIOUS instance of "
        "the container (essential for crashloop analysis — the current "
        "instance may not have started yet). For INIT phase issues, pass "
        "the init container's name. Returns the last `lines` log lines "
        "(default 200). Empty output means the container produced no logs "
        "in that period (or doesn't exist; the API will say so)."
    ),
    "input_schema": {
        "type": "object",
        "required": ["namespace", "pod", "container"],
        "properties": {
            "namespace": {"type": "string"},
            "pod": {"type": "string"},
            "container": {
                "type": "string",
                "description": "Container name within the pod. For init containers, use the init container's name.",
            },
            "lines": {
                "type": "integer",
                "default": 200,
                "description": "Number of recent log lines to return.",
            },
            "previous": {
                "type": "boolean",
                "default": False,
                "description": "Fetch logs from the PREVIOUS instance of the container (use for CrashLoopBackOff to see what caused the last crash).",
            },
        },
    },
}

EMIT_FINDINGS_TOOL = {
    "name": "emit_findings",
    "description": (
        "Submit your final Findings and terminate the investigation. Call this "
        "when you have a confident root cause supported by evidence, OR when "
        "you have exhausted what the kubectl tools can tell you and the "
        "hypothesis points to a runtime layer this minimal Phase 1 agent "
        "cannot inspect. The input you provide becomes the structured Findings "
        "output of this investigation."
    ),
    # Generated from the Pydantic model — single source of truth for the
    # output contract. When the model emits emit_findings, agent.py validates
    # the input via `Findings.model_validate(...)`, so the schema enforcement
    # is both client-side (via the LLM seeing the schema) AND server-side
    # (via Pydantic parsing). Any drift between description and validation
    # is impossible because both come from the same class definition.
    "input_schema": Findings.model_json_schema(),
}

TOOLS = [
    KUBECTL_READ_TOOL,
    KUBECTL_LIST_TOOL,
    KUBECTL_GET_LOGS_TOOL,
    EMIT_FINDINGS_TOOL,
]

# ─── K8s client setup (module-load; cached afterwards) ─────────────────────────

REQUEST_TIMEOUT_SECONDS = 10


def _load_k8s_config() -> None:
    """Load K8s credentials. In-cluster first; kubeconfig fallback.

    This single function works in BOTH contexts:
    - Inside a pod: load_incluster_config() reads the projected SA token at
      /var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt} and the
      KUBERNETES_SERVICE_HOST env var.
    - On a dev laptop: load_kube_config() reads ~/.kube/config (or the path
      in $KUBECONFIG).

    When Step 4.5 lands and this code runs inside an agent-task pod with
    a projected SA token and Istio restricting outbound to apiserver +
    Anthropic, no code change here is required. The K8s client just works.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


_load_k8s_config()
_core_v1 = client.CoreV1Api()
_api_client = client.ApiClient()
_dynamic_client = dynamic.DynamicClient(_api_client)


# ─── Filter helpers ────────────────────────────────────────────────────────────


def _to_filtered_pod_json(pod_obj: Any) -> str:
    """Serialize a V1Pod to JSON and drop high-prompt-injection-risk fields."""
    pod_dict = _api_client.sanitize_for_serialization(pod_obj)
    metadata = pod_dict.get("metadata", {})
    metadata.pop("annotations", None)
    metadata.pop("managedFields", None)
    if "ownerReferences" in metadata:
        metadata["ownerReferences"] = metadata["ownerReferences"][:1]
    return json.dumps(pod_dict, indent=2)


def _filter_node(node_dict: dict[str, Any]) -> dict[str, Any]:
    """Drop scheduling-irrelevant + noisy fields from a single node dict."""
    metadata = node_dict.get("metadata", {})
    metadata.pop("annotations", None)
    metadata.pop("managedFields", None)

    spec = node_dict.get("spec", {})
    spec.pop("podCIDR", None)
    spec.pop("podCIDRs", None)
    spec.pop("providerID", None)
    spec.pop("configSource", None)

    status = node_dict.get("status", {})
    status.pop("images", None)
    status.pop("daemonEndpoints", None)
    status.pop("volumesInUse", None)
    status.pop("volumesAttached", None)
    node_info = status.get("nodeInfo", {})
    if node_info:
        status["nodeInfo"] = {
            "kubeletVersion": node_info.get("kubeletVersion"),
            "osImage": node_info.get("osImage"),
            "architecture": node_info.get("architecture"),
        }

    return node_dict


def _nodes_to_filtered_json(node_list_obj: Any) -> str:
    """Serialize a V1NodeList to JSON, applying _filter_node to each item."""
    nodes_dict = _api_client.sanitize_for_serialization(node_list_obj)
    for node in nodes_dict.get("items", []):
        _filter_node(node)
    return json.dumps(nodes_dict, indent=2)


def _filter_generic_resource(d: dict[str, Any], kind: str) -> dict[str, Any]:
    """Drop noisy fields + sensitive data from any K8s resource dict."""
    metadata = d.get("metadata", {})
    metadata.pop("annotations", None)
    metadata.pop("managedFields", None)

    if kind == "Secret":
        d.pop("data", None)
        d.pop("stringData", None)

    return d


def _pod_summary(pod_dict: dict[str, Any]) -> dict[str, Any]:
    """Compact pod summary for list-views."""
    metadata = pod_dict.get("metadata", {})
    spec = pod_dict.get("spec", {})
    status = pod_dict.get("status", {})

    containers_summary = []
    for c in spec.get("containers", []) or []:
        req = (c.get("resources", {}) or {}).get("requests", {}) or {}
        cpu = req.get("cpu", "0")
        mem = req.get("memory", "0")
        containers_summary.append({"name": c.get("name"), "cpu_request": cpu, "memory_request": mem})

    owners = metadata.get("ownerReferences") or []
    primary_owner = None
    if owners:
        o = owners[0]
        primary_owner = {"kind": o.get("kind"), "name": o.get("name")}

    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "owner": primary_owner,
        "labels": metadata.get("labels"),
        "containers": containers_summary,
    }


def _pods_to_summary_json(pod_list_obj: Any) -> str:
    """Serialize a V1PodList to compact summary JSON."""
    pods_dict = _api_client.sanitize_for_serialization(pod_list_obj)
    summaries = [_pod_summary(p) for p in pods_dict.get("items", [])]
    return json.dumps({"items": summaries}, indent=2)


def _events_to_sorted_json(event_list_obj: Any) -> str:
    """Serialize a V1EventList to JSON, sorted by lastTimestamp ascending."""
    events_dict = _api_client.sanitize_for_serialization(event_list_obj)
    items = events_dict.get("items", [])
    items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
    events_dict["items"] = items
    return json.dumps(events_dict, indent=2)


def _selector_matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    """True iff a K8s label selector matches the given labels.

    Supports both .matchLabels (key=value AND) and .matchExpressions
    (In, NotIn, Exists, DoesNotExist operators). An empty selector
    (no matchLabels and no matchExpressions) matches everything per
    K8s semantics. Unknown matchExpressions operators conservatively
    do NOT match.
    """
    if not selector:
        return True
    labels = labels or {}

    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False

    for expr in selector.get("matchExpressions") or []:
        key = expr.get("key")
        op = expr.get("operator")
        values = expr.get("values") or []
        actual = labels.get(key)
        if op == "In":
            if actual not in values:
                return False
        elif op == "NotIn":
            if actual in values:
                return False
        elif op == "Exists":
            if key not in labels:
                return False
        elif op == "DoesNotExist":
            if key in labels:
                return False
        else:
            return False

    return True


# ─── Tool execution ────────────────────────────────────────────────────────────


def execute_tool(name: str, args: dict[str, Any]) -> str:
    """Execute a tool by name and return the result as a string.

    Returns:
        The tool's output as a JSON string, or an error message formatted as a
        string. The agent surfaces this back to the LLM as the tool_result
        content. Even errors are formatted as strings — we want the LLM to be
        able to reason about a NotFound or PermissionDenied just like a normal
        response.
    """
    if name == "kubectl_read":
        return _exec_read(args)
    elif name == "kubectl_list":
        return _exec_list(args)
    elif name == "kubectl_get_container_logs":
        return _exec_logs(args)
    elif name == "emit_findings":
        return "emit_findings is handled by the agent loop and should not be executed as a regular tool"
    else:
        return f"unknown tool: {name}"


def _exec_read(args: dict[str, Any]) -> str:
    kind = args.get("kind")
    if kind not in _READ_KINDS:
        return (
            f"Error: kind '{kind}' is not supported by kubectl_read. "
            f"Supported kinds: {_READ_KINDS}."
        )
    namespace = args.get("namespace") or ""
    resource_name = args.get("name")
    if not resource_name:
        return "Error: 'name' is required for kubectl_read."
    if not namespace:
        return f"Error: 'namespace' is required for kubectl_read (kind={kind})."

    try:
        if kind == "Pod":
            pod = _core_v1.read_namespaced_pod(
                name=resource_name,
                namespace=namespace,
                _request_timeout=REQUEST_TIMEOUT_SECONDS,
            )
            return _to_filtered_pod_json(pod)

        api_version = _KIND_TO_API_VERSION[kind]
        resource_api = _dynamic_client.resources.get(
            api_version=api_version, kind=kind
        )
        obj = resource_api.get(name=resource_name, namespace=namespace)
        d = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
        d = _filter_generic_resource(d, kind)
        return json.dumps(d, indent=2)
    except ResourceNotFoundError:
        return (
            f"K8s API error: resource type '{_KIND_TO_API_VERSION.get(kind, '?')}/{kind}' "
            "is not registered in this cluster."
        )
    except DynNotFoundError:
        return f"K8s API error: {kind}/{resource_name} not found in namespace '{namespace}'."
    except ApiException as e:
        return f"K8s API error reading {kind}/{resource_name}: status={e.status} reason={e.reason}"
    except Exception as e:
        return f"K8s API error reading {kind}/{resource_name}: {type(e).__name__}: {e}"


def _exec_list(args: dict[str, Any]) -> str:
    kind = args.get("kind")
    if kind not in _LIST_KINDS:
        return (
            f"Error: kind '{kind}' is not supported by kubectl_list. "
            f"Supported kinds: {_LIST_KINDS}."
        )
    namespace = args.get("namespace") or None

    try:
        if kind == "Pod":
            return _list_pods(args, namespace)
        elif kind == "Node":
            nodes = _core_v1.list_node(_request_timeout=REQUEST_TIMEOUT_SECONDS)
            return _nodes_to_filtered_json(nodes)
        elif kind == "Event":
            if not namespace:
                return "Error: 'namespace' is required for kubectl_list kind=Event."
            return _list_events(args, namespace)
        elif kind == "PodDisruptionBudget":
            if not namespace:
                return "Error: 'namespace' is required for kubectl_list kind=PodDisruptionBudget."
            return _list_pdbs(args, namespace)
        elif kind == "ResourceQuota":
            if not namespace:
                return "Error: 'namespace' is required for kubectl_list kind=ResourceQuota."
            quotas = _core_v1.list_namespaced_resource_quota(
                namespace=namespace, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
            quotas_dict = _api_client.sanitize_for_serialization(quotas)
            for q in quotas_dict.get("items", []) or []:
                _filter_generic_resource(q, "ResourceQuota")
            return json.dumps(quotas_dict, indent=2)
        elif kind == "LimitRange":
            if not namespace:
                return "Error: 'namespace' is required for kubectl_list kind=LimitRange."
            lrs = _core_v1.list_namespaced_limit_range(
                namespace=namespace, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
            lrs_dict = _api_client.sanitize_for_serialization(lrs)
            for lr in lrs_dict.get("items", []) or []:
                _filter_generic_resource(lr, "LimitRange")
            return json.dumps(lrs_dict, indent=2)
        # unreachable — enum is checked above
        return f"Error: unhandled kind '{kind}'."
    except ResourceNotFoundError:
        return f"K8s API error: resource type for kind={kind} not registered in this cluster."
    except ApiException as e:
        return f"K8s API error listing {kind}: status={e.status} reason={e.reason}"
    except Exception as e:
        return f"K8s API error listing {kind}: {type(e).__name__}: {e}"


def _list_pods(args: dict[str, Any], namespace: str | None) -> str:
    field_selectors = []
    if args.get("node_name"):
        field_selectors.append(f"spec.nodeName={args['node_name']}")
    if args.get("phase"):
        field_selectors.append(f"status.phase={args['phase']}")
    field_selector = ",".join(field_selectors) if field_selectors else None
    label_selector = args.get("label_selector") or None

    list_kwargs: dict[str, Any] = {"_request_timeout": REQUEST_TIMEOUT_SECONDS}
    if field_selector:
        list_kwargs["field_selector"] = field_selector
    if label_selector:
        list_kwargs["label_selector"] = label_selector

    if namespace:
        pods = _core_v1.list_namespaced_pod(namespace=namespace, **list_kwargs)
    else:
        pods = _core_v1.list_pod_for_all_namespaces(**list_kwargs)
    return _pods_to_summary_json(pods)


def _list_events(args: dict[str, Any], namespace: str) -> str:
    selectors = []
    if args.get("involved_object_name"):
        selectors.append(f"involvedObject.name={args['involved_object_name']}")
    if args.get("involved_object_kind"):
        selectors.append(f"involvedObject.kind={args['involved_object_kind']}")
    list_kwargs: dict[str, Any] = {"_request_timeout": REQUEST_TIMEOUT_SECONDS}
    if selectors:
        list_kwargs["field_selector"] = ",".join(selectors)
    events = _core_v1.list_namespaced_event(namespace=namespace, **list_kwargs)
    return _events_to_sorted_json(events)


def _list_pdbs(args: dict[str, Any], namespace: str) -> str:
    pdb_api = _dynamic_client.resources.get(
        api_version="policy/v1", kind="PodDisruptionBudget"
    )
    pdbs = pdb_api.get(namespace=namespace)
    pdbs_dict = pdbs.to_dict() if hasattr(pdbs, "to_dict") else dict(pdbs)
    items = pdbs_dict.get("items", []) or []

    pod_name = args.get("pod_name")
    if not pod_name:
        return json.dumps(
            {"items": [_filter_generic_resource(p, "PodDisruptionBudget") for p in items]},
            indent=2,
        )

    try:
        pod = _core_v1.read_namespaced_pod(
            name=pod_name,
            namespace=namespace,
            _request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except ApiException as e:
        return (
            f"K8s API error reading pod '{pod_name}' for PDB selector match: "
            f"status={e.status} reason={e.reason}"
        )
    pod_labels = (pod.metadata.labels or {}) if pod.metadata else {}

    matched = []
    for pdb in items:
        selector = (pdb.get("spec") or {}).get("selector") or {}
        if _selector_matches(selector, pod_labels):
            matched.append(_filter_generic_resource(pdb, "PodDisruptionBudget"))

    return json.dumps(
        {
            "pod_name": pod_name,
            "pod_labels": pod_labels,
            "matched_pdb_count": len(matched),
            "matched_pdbs": matched,
        },
        indent=2,
    )


def _exec_logs(args: dict[str, Any]) -> str:
    try:
        logs = _core_v1.read_namespaced_pod_log(
            name=args["pod"],
            namespace=args["namespace"],
            container=args["container"],
            tail_lines=args.get("lines", 200),
            previous=args.get("previous", False),
            _request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not logs:
            return "(no log output)"
        return logs
    except ApiException as e:
        return f"K8s API error reading logs: status={e.status} reason={e.reason}"
    except Exception as e:
        return f"K8s API error reading logs: {type(e).__name__}: {e}"


# ─── Bootstrap (deterministic pre-flight before the LLM loop starts) ──────────


def bootstrap_pod_context(namespace: str, pod_name: str) -> dict[str, Any]:
    """Fetch initial pod state for inclusion in the first user message.

    See ARCHITECTURE.md §15 (Decision: Bootstrap-in-task). This is the
    deterministic phase before the LLM loop starts — we want the agent to
    have ground truth context from turn 1, not spend its first turn calling
    kubectl_read.

    Returns:
        A dict with the filtered pod JSON and a fetched-at ISO timestamp.
    """
    pod_text = execute_tool(
        "kubectl_read",
        {"kind": "Pod", "namespace": namespace, "name": pod_name},
    )
    return {
        "pod_state_filtered": pod_text,
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
    }
