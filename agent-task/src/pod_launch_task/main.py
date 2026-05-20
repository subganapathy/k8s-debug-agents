"""CLI entry point for the standalone pod-launch agent.

Usage:
    python -m pod_launch_task --namespace <ns> --pod <name>

Requires ANTHROPIC_API_KEY in the environment. For local dev with the
key in macOS Keychain:

    export ANTHROPIC_API_KEY=$(security find-generic-password \\
        -a "$USER" -s "anthropic-api-key" -w | tr -d '\\n\\r')
"""
from __future__ import annotations

import argparse
import os
import sys

from pod_launch_task.agent import run_agent


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the pod-launch diagnostic agent against a specific pod. "
            "Phase 1 standalone — no CRDs, no orchestrator, no Job. Just the "
            "agent loop against your active kubectl context."
        )
    )
    parser.add_argument(
        "--namespace",
        "-n",
        required=True,
        help="Namespace of the pod to diagnose (e.g., eval-insufficient-cpu)",
    )
    parser.add_argument(
        "--pod",
        "-p",
        required=True,
        help="Name of the pod to diagnose (e.g., needs-massive-cpu)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress per-turn trajectory output; print only the final Findings JSON",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.stderr.write(
            "ERROR: ANTHROPIC_API_KEY is not set in the environment.\n"
            "\n"
            "If your key is in macOS Keychain (recommended for dev):\n"
            "  export ANTHROPIC_API_KEY=$(security find-generic-password "
            '-a "$USER" -s "anthropic-api-key" -w | tr -d \'\\n\\r\')\n'
            "\n"
            "Then re-run.\n"
        )
        return 2

    if not args.quiet:
        sys.stderr.write(
            f">>> Running pod-launch agent on {args.namespace}/{args.pod}\n"
        )

    result = run_agent(args.namespace, args.pod, verbose=not args.quiet)

    if not args.quiet:
        sys.stderr.write("\n=== FINAL FINDINGS ===\n")
    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
