"""CLI subcommand registration for the pod_launch_task variant.

Two surfaces consume this:
    1. `agent_core.cli` (user-facing, Job-spawning) — needs to know
       the target namespace + pod name + args to forward to the
       in-Pod runner. Uses `spawn_handler`.
    2. `agent_core.runner` (in-Pod entrypoint) — needs to actually
       execute the agent loop and return AgentResult. Uses
       `run_handler`.

Both surfaces share the SAME argparse subparser definition (subcommand
name, --namespace, --pod). Only the handler attached via set_defaults
differs. This keeps variants from declaring their flags twice.

When a new variant lands (e.g., intra_cluster_traffic_task), it
follows this same shape: one register_cli, two handler functions, both
attached via set_defaults.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from agent_core.schemas import AgentResult
from pod_launch_task.agent import run_agent


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    """Register the pod-launch subcommand with BOTH handlers attached.

    The caller (agent_core.cli or agent_core.runner) picks which handler
    to invoke based on which surface it is.
    """
    parser = subparsers.add_parser(
        "pod-launch",
        help="Diagnose why a pod is stuck during launch.",
        description=(
            "Run the pod-launch diagnostic agent against a specific pod. "
            "When invoked via the `agent` CLI, spawns a K8s Job. When invoked "
            "via `agent-runner` inside that Job's container, executes the "
            "agent loop in-process."
        ),
    )
    parser.add_argument(
        "--namespace",
        "-n",
        required=True,
        help="Namespace of the pod to diagnose (e.g., eval-insufficient-cpu).",
    )
    parser.add_argument(
        "--pod",
        "-p",
        required=True,
        help="Name of the pod to diagnose (e.g., needs-massive-cpu).",
    )
    parser.set_defaults(
        spawn_handler=_spawn_handler,
        run_handler=_run_handler,
        # Back-compat alias — some surfaces may use `handler` generically.
        # Defaults to the spawn handler since that's what the user CLI uses.
        handler=_spawn_handler,
    )


def _spawn_handler(args: argparse.Namespace) -> dict[str, Any]:
    """Used by `agent_core.cli`. Returns metadata for Job construction.

    Includes the args list that should be passed to `agent-runner` inside
    the Job's container. Keeping the mapping local to the variant means
    agent_core doesn't have to know variant flag shapes.
    """
    return {
        "target_namespace": args.namespace,
        "pod_name": args.pod,
        "runner_args": ["--namespace", args.namespace, "--pod", args.pod],
    }


def _run_handler(args: argparse.Namespace) -> AgentResult:
    """Used by `agent_core.runner` (inside the Job's container).

    Actually runs the agent loop. Returns AgentResult; the runner writes
    it to HR.status before exiting.
    """
    if not args.quiet:
        sys.stderr.write(
            f">>> Running pod-launch agent on {args.namespace}/{args.pod}\n"
        )
    return run_agent(args.namespace, args.pod, verbose=not args.quiet)
