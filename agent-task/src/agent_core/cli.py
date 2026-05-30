"""User-facing CLI — spawns a K8s Job per invocation and watches its HR.

This is what humans type:
    agent pod-launch --namespace <ns> --pod <name>
    agent --quiet pod-launch --namespace <ns> --pod <name>

What it does:
    1. Reads the kubeconfig (dev) or in-cluster config (production).
    2. Creates a HandoffRequest CR in the target namespace.
    3. Builds an empty Job (we need its UID for owner-references).
    4. Provisions per-Job RBAC (SA + Role + RoleBinding + ClusterRoleBinding)
       owned by the Job.
    5. Submits the Job.
    6. Watches HR.status.phase until Completed or Failed.
    7. Prints the resulting Findings + Metrics JSON.
    8. Explicitly deletes the per-Job ClusterRoleBinding (the one piece
       that K8s GC can't cascade because of cluster→namespace scope rules).
    9. Exits. HR persists with findings populated; the Job + namespace-
       scoped RBAC get GC'd by K8s within ~5min via Job's ttlSecondsAfterFinished.

The dispatcher controller in PR-16 will perform the same sequence,
triggered by a DiagnosisRequest reconcile rather than a human invocation.
Both consume the same `agent_core.job_runner` + `agent_core.rbac_provisioner`
primitives.

Subcommand registration: each variant (pod_launch_task today,
intra_cluster_traffic_task next) exposes `register_cli(subparsers)` in
its own `cli.py`. Same registry as `agent_core.runner` (the in-Pod
runner) — both surfaces grow together when variants land.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

from kubernetes import client, config  # type: ignore[import-untyped]

from agent_core import job_runner, rbac_provisioner
from agent_core.schemas import AgentResult


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent",
        description=(
            "Spawn a diagnostic agent-task variant as a K8s Job. Creates a "
            "HandoffRequest CR, watches it to completion, prints the "
            "structured Findings document as JSON."
        ),
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress per-step status updates; print only the final JSON.",
    )
    parser.add_argument(
        "--image",
        default="agent-task:dev",
        help="Container image to use for the variant Job (default: agent-task:dev — Kind dev convention).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="How long to wait (seconds) for the HR to reach Completed/Failed.",
    )

    subparsers = parser.add_subparsers(
        dest="variant",
        required=True,
        metavar="<variant>",
        help="Which agent variant to run.",
    )
    _register_variants(subparsers)

    args = parser.parse_args(argv)

    # Auth: in-cluster if pod, kubeconfig if dev laptop. Same dual-mode
    # detection as hr_writer.
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    return _run_variant(args)


def _register_variants(subparsers: argparse._SubParsersAction) -> None:
    """Variant registry — kept in sync with agent_core.runner."""
    from pod_launch_task.cli import register_cli as register_pod_launch

    register_pod_launch(subparsers)


def _run_variant(args: argparse.Namespace) -> int:
    """The Job-spawning pipeline. Returns process exit code."""
    # The variant's spawn_handler tells us *what* to ask the agent to do
    # (target namespace, pod, args). We don't actually call run_agent
    # here — the Job's container does that via agent_core.runner.
    job_request = args.spawn_handler(args)
    target_namespace: str = job_request["target_namespace"]
    pod_name: str = job_request["pod_name"]
    cli_args_for_runner: list[str] = job_request["runner_args"]
    variant: str = args.variant

    # uuid in name: human-readable + collision-free. K8s allows up to 63
    # chars for most names; "agent-job-" + 8 uuid chars + "-..." keeps us
    # well under.
    run_uid = uuid.uuid4().hex[:8]
    hr_name = f"agent-{variant}-{run_uid}"
    job_name = f"agent-{variant}-{run_uid}"

    common_labels = {
        "app.kubernetes.io/name": "k8s-debug-agents",
        "app.kubernetes.io/managed-by": "agent-cli",
        "agent-task/created-by": "cli",
        "agent-task/variant": variant,
        "agent-task/run-uid": run_uid,
    }

    def log(msg: str) -> None:
        if not args.quiet:
            sys.stderr.write(f"[agent-cli] {msg}\n")
            sys.stderr.flush()

    log(f"creating HandoffRequest {target_namespace}/{hr_name}")
    hr_obj = job_runner.create_handoff_request(
        variant=variant,
        target_namespace=target_namespace,
        pod_name=pod_name,
        hr_name=hr_name,
        common_labels=common_labels,
    )
    hr_uid = hr_obj["metadata"]["uid"]

    log(f"building Job {target_namespace}/{job_name} (image={args.image})")
    # Build the Job spec *without* SA name resolved yet. We compute the SA
    # name from hr_uid (which RBAC provisioner uses too) so both sides
    # agree without a circular dependency.
    sa_name = f"agent-job-{hr_uid[:6]}"

    hr_owner = job_runner.hr_owner_ref(hr_obj)
    job_spec = job_runner.build_variant_job(
        variant=variant,
        cli_args=cli_args_for_runner,
        image=args.image,
        sa_name=sa_name,
        hr_uid=hr_uid,
        hr_name=hr_name,
        hr_namespace=target_namespace,
        target_namespace=target_namespace,
        common_labels=common_labels,
        hr_owner_ref=hr_owner,
        job_name=job_name,
    )

    # Provision RBAC first (so the SA exists when the Job spec references
    # it), then submit the Job. Per-Job RBAC is owned by the Job so it
    # cascades on Job TTL — but the Job has to exist first to be the
    # owner. We resolve this circularity by creating the Job, getting its
    # UID, then creating RBAC with the Job as owner. The Job's pods stay
    # Pending until the SA exists, then proceed.
    log("submitting Job (will be Pending until RBAC is provisioned)")
    submitted_job = job_runner.submit_job(job_spec, target_namespace)
    job_owner = job_runner.job_owner_ref(submitted_job)

    log("provisioning per-Job RBAC (SA + Role + RoleBinding + ClusterRoleBinding)")
    bundle = rbac_provisioner.provision(
        hr_uid=hr_uid,
        hr_name=hr_name,
        target_namespace=target_namespace,
        job_owner_ref=job_owner,
        common_labels=common_labels,
    )

    log(f"watching HR.status (timeout {args.timeout}s)")
    try:
        final_hr = job_runner.watch_hr_status(
            hr_name=hr_name,
            target_namespace=target_namespace,
            timeout_seconds=args.timeout,
        )
    except TimeoutError as e:
        log(f"TIMEOUT: {e}")
        log(f"the HR remains; investigate with: kubectl describe hr {hr_name} -n {target_namespace}")
        log(f"cleaning up CRB {bundle.clusterrolebinding_name}")
        rbac_provisioner.cleanup_clusterrolebinding(bundle.clusterrolebinding_name)
        return 124  # conventional timeout exit code

    phase = (final_hr.get("status") or {}).get("phase")
    log(f"HR phase={phase}; cleaning up CRB {bundle.clusterrolebinding_name}")
    rbac_provisioner.cleanup_clusterrolebinding(bundle.clusterrolebinding_name)

    # Reconstitute the AgentResult from HR.status and print it. This is
    # the same JSON shape the in-process CLI used to produce, so the eval
    # harness's parser doesn't need to change.
    result = _agent_result_from_hr_status(final_hr)
    if not args.quiet:
        sys.stderr.write("\n=== FINAL FINDINGS ===\n")
    print(result.model_dump_json(indent=2))

    return 0 if phase == "Completed" else 1


def _agent_result_from_hr_status(hr_obj: dict[str, Any]) -> AgentResult:
    """Parse HR.status -> AgentResult.

    Pydantic does the heavy lifting: both AgentResult and the HR.status
    fields are defined by the same Findings/Metrics classes, so the JSON
    round-trips cleanly. If status is missing fields the validator surfaces
    a clean error rather than us papering over the gap.
    """
    status = hr_obj.get("status") or {}
    return AgentResult.model_validate(
        {
            "findings": status.get("findings"),
            "metrics": status.get("metrics") or {"wall_clock_seconds": 0.0},
        }
    )


if __name__ == "__main__":
    sys.exit(main())
