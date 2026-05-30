"""In-process variant runner — the container's entrypoint inside the Job pod.

The user-facing CLI (`agent`, defined in `agent_core.cli`) is the
Job-spawning entrypoint: it builds a Job spec and submits it. Inside
that Job's container, the entrypoint is `agent-runner` — THIS module —
which actually runs the agent loop in-process and writes findings to
HandoffRequest.status before exiting.

Two scripts, one image; the user types `agent`, the Job's container
runs `agent-runner`. See `design_istio_selective_injection.md` +
`current_state.md` (PR-10 scope) for the full picture.

Usage (inside a Job pod — never invoked by humans directly):
    agent-runner pod-launch --namespace <ns> --pod <name>
    python -m agent_core.runner pod-launch --namespace <ns> --pod <name>

Environment variables expected inside the Job container:
    HR_NAME       Name of the HandoffRequest this Job belongs to
    HR_NAMESPACE  Namespace of the HandoffRequest
    (ANTHROPIC_API_KEY intentionally NOT set — credentials arrive via
    Istio ext_authz substitution per the Step-3 credential pipeline.)

Behaviour:
    1. Parse args via the same subparser registry the user-facing CLI uses.
    2. Run the variant's handler (calls `run_agent(...)`).
    3. Write findings + metrics to HR.status via the K8s API (subresource
       PATCH). Phase becomes Completed (success) or Failed (exception).
    4. Print the AgentResult JSON to stdout as a debug fallback.

The in-process loop is identical to what the CLI used to do directly;
the difference is the surrounding context (cluster pod, no API-key env
var, Status-writing).
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

from agent_core.schemas import AgentResult


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-runner",
        description=(
            "Container entrypoint for variant Jobs. Runs the agent loop "
            "in-process and writes findings to HandoffRequest.status."
        ),
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help=(
            "Suppress per-turn trajectory output. Status-write still "
            "happens; only the human-readable stderr trail is silenced."
        ),
    )

    subparsers = parser.add_subparsers(
        dest="variant",
        required=True,
        metavar="<variant>",
        help="Which agent variant to run.",
    )
    _register_variants(subparsers)

    args = parser.parse_args(argv)

    # HR_NAME / HR_NAMESPACE are required when this runner is invoked
    # by a real Job pod. Allow them to be missing for local dev (running
    # `agent-runner` directly without a Job), in which case we skip the
    # status write and just print JSON.
    hr_name = os.environ.get("HR_NAME")
    hr_namespace = os.environ.get("HR_NAMESPACE")

    try:
        # run_handler is the in-process executor (calls run_agent). Set
        # by the variant's register_cli alongside spawn_handler. Both
        # surfaces (cli.py + runner.py) share the same parser; only the
        # selected handler differs.
        result: AgentResult = args.run_handler(args)
    except Exception as e:  # noqa: BLE001
        # Agent loop blew up unexpectedly. Synthesize a degenerate result
        # so we can still write SOMETHING to HR.status; otherwise the CLI
        # would block forever waiting for completion.
        sys.stderr.write(
            f"FATAL: agent loop raised: {type(e).__name__}: {e}\n"
            f"{traceback.format_exc()}\n"
        )
        if hr_name and hr_namespace:
            _write_failure_status(hr_name, hr_namespace, reason=str(e))
        return 1

    # Success path: write findings to HR.status, then print stdout JSON.
    if hr_name and hr_namespace:
        from agent_core.hr_writer import write_findings

        write_findings(
            hr_name=hr_name,
            hr_namespace=hr_namespace,
            result=result,
            phase="Completed",
        )

    if not args.quiet:
        sys.stderr.write("\n=== FINAL FINDINGS ===\n")
    print(result.model_dump_json(indent=2))
    return 0


def _write_failure_status(hr_name: str, hr_namespace: str, reason: str) -> None:
    """Best-effort failure status write. Swallows secondary exceptions so
    the original error is what reaches the operator via container exit code."""
    try:
        from agent_core.hr_writer import write_failure

        write_failure(hr_name=hr_name, hr_namespace=hr_namespace, reason=reason)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(
            f"WARN: status-write failed: {type(e).__name__}: {e}\n"
            "(original agent error is the real failure; container will exit 1)\n"
        )


def _register_variants(subparsers: argparse._SubParsersAction) -> None:
    """Explicit variant registration — same registry as the user CLI uses.

    Mirrored in agent_core.cli._register_variants so both surfaces grow
    together when new variants land. Single import per variant.
    """
    from pod_launch_task.cli import register_cli as register_pod_launch

    register_pod_launch(subparsers)


if __name__ == "__main__":
    sys.exit(main())
