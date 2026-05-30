"""CLI subcommand registration for the pod_launch_task variant.

The top-level dispatcher in `agent_core.cli` calls `register_cli` during
argparse setup. We declare only what's pod_launch-specific: the
subcommand name (`pod-launch`), the variant-specific flags
(`--namespace`, `--pod`), and a handler that invokes the agent loop.

Everything variant-agnostic (API-key check, AgentResult JSON printing,
the shared `--quiet` flag, the exit-code contract) lives in
`agent_core.cli` — variants don't reinvent it.
"""
from __future__ import annotations

import argparse
import sys

from agent_core.schemas import AgentResult
from pod_launch_task.agent import run_agent


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "pod-launch",
        help="Diagnose why a pod is stuck during launch.",
        description=(
            "Run the pod-launch diagnostic agent against a specific pod. "
            "Phase 1 standalone — no CRDs, no orchestrator, no Job. Just "
            "the agent loop against your active kubectl context."
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
    parser.set_defaults(handler=_handle)


def _handle(args: argparse.Namespace) -> AgentResult:
    """Variant handler. Returns the AgentResult; the dispatcher prints it."""
    if not args.quiet:
        sys.stderr.write(
            f">>> Running pod-launch agent on {args.namespace}/{args.pod}\n"
        )
    return run_agent(args.namespace, args.pod, verbose=not args.quiet)
