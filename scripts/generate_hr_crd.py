#!/usr/bin/env python3
"""Generate the HandoffRequest CRD's openAPIV3Schema from Pydantic models.

This script writes `charts/k8s-debug-agents/templates/crds/handoffrequest.yaml`
with the openAPIV3Schema embedded from `agent_core.schemas`. Run whenever
the Pydantic models change; the output is committed alongside the source
so chart consumers don't need a Python toolchain.

Usage:
    cd k8s-debug-agents
    PYTHONPATH=agent-task/src python scripts/generate_hr_crd.py
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml  # noqa: F401  (used implicitly via pyyaml; we hand-render YAML for clarity)

from agent_core.schemas import HandoffRequestSpec, HandoffRequestStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "charts/k8s-debug-agents/templates/crds/handoffrequest.yaml"

HEADER = """{{/*
HandoffRequest CRD — per-invocation diagnostic record.

GENERATED FROM agent_core.schemas.HandoffRequestSpec + HandoffRequestStatus.
DO NOT EDIT BY HAND. Regenerate with:
    PYTHONPATH=agent-task/src python scripts/generate_hr_crd.py

One HR represents one diagnostic agent invocation. Created by the CLI
(today) or the dispatcher controller (PR-16). Owns the Job + per-Job RBAC
(SA, Role, RoleBinding) so deleting the HR cascades cleanup. The agent
writes findings + metrics to .status at termination via a subresource
PATCH. Watchers (CLI / dispatcher) poll/watch .status.phase until
Completed or Failed.

Lives in the same namespace as the pod being diagnosed (per-target model
locked by design_istio_selective_injection.md + design memos for PR-10).

Field naming: spec uses camelCase (K8s convention); status carries the
agent's Findings + Metrics shapes unchanged — those models are also the
source of truth for emit_findings tool input_schema and AgentResult
stdout, so JSON output stays byte-compatible across the chain.
*/}}
"""


def _strip_pydantic_metadata(schema: dict) -> dict:
    """Pydantic's model_json_schema emits constructs (const, $defs, $ref,
    title, anyOf-with-null) that are valid JSON Schema but rejected by
    Kubernetes' OpenAPI v3 validator.

    Transformations:
      - inline $ref references
      - convert const → enum (K8s OpenAPI doesn't support const)
      - drop title fields (cosmetic; K8s rejects unknown fields)
      - convert anyOf: [{type: T}, {type: null}] → nullable=true (K8s style)
    """
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                name = ref.split("/")[-1]
                resolved = defs.get(name, {})
                return inline(resolved)
            # anyOf nullable shorthand: [{type: T}, {type: null}] →
            # remove the nullable variant and add nullable: true
            if "anyOf" in node and isinstance(node["anyOf"], list):
                variants = node["anyOf"]
                non_null = [v for v in variants if v != {"type": "null"}]
                has_null = len(non_null) < len(variants)
                if has_null and len(non_null) == 1:
                    merged = {**node, **non_null[0]}
                    merged.pop("anyOf", None)
                    merged["nullable"] = True
                    return inline(merged)
            result = {}
            for k, v in node.items():
                if k == "title":
                    continue
                if k == "const":
                    # K8s OpenAPI: const → enum with single value
                    result["enum"] = [v]
                    continue
                result[k] = inline(v)
            return result
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    return inline(schema)


def main() -> None:
    spec_schema = _strip_pydantic_metadata(HandoffRequestSpec.model_json_schema())
    status_schema = _strip_pydantic_metadata(HandoffRequestStatus.model_json_schema())

    # Compose the openAPIV3Schema. Kubernetes requires the root be an
    # object with properties for spec and status.
    open_api_schema = {
        "type": "object",
        "properties": {
            "spec": spec_schema,
            "status": status_schema,
        },
        "required": ["spec"],
    }

    crd = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {
            "name": "handoffrequests.k8s-debug-agents.io",
            # Labels added via Helm template's tpl include below.
        },
        "spec": {
            "group": "k8s-debug-agents.io",
            "scope": "Namespaced",
            "names": {
                "plural": "handoffrequests",
                "singular": "handoffrequest",
                "kind": "HandoffRequest",
                "shortNames": ["hr"],
                "categories": ["k8s-debug-agents"],
            },
            "versions": [
                {
                    "name": "v1",
                    "served": True,
                    "storage": True,
                    "subresources": {"status": {}},
                    "additionalPrinterColumns": [
                        {"name": "Variant", "type": "string", "jsonPath": ".spec.variant"},
                        {"name": "Target", "type": "string", "jsonPath": ".spec.target.podName"},
                        {"name": "Phase", "type": "string", "jsonPath": ".status.phase"},
                        {"name": "Age", "type": "date", "jsonPath": ".metadata.creationTimestamp"},
                    ],
                    "schema": {"openAPIV3Schema": open_api_schema},
                }
            ],
        },
    }

    # Emit deterministic JSON (sorted keys, 2-space indent) so diffs are
    # readable; Helm parses both JSON and YAML in template files.
    body = json.dumps(crd, indent=2, sort_keys=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(HEADER + body + "\n")
    print(f"Wrote {OUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
