"""Top-level CLI dispatcher for all agent-task variants.

Pattern: git-style subcommands. Each variant registers its own subparser
via `register_cli(subparsers)`. This module owns the variant-agnostic
plumbing — API-key check, `--quiet`, `AgentResult` serialization — so
variants only declare their own arguments and a handler.

Usage:
    agent pod-launch --namespace <ns> --pod <name>
    agent --quiet pod-launch --namespace <ns> --pod <name>
    python -m agent_core pod-launch --namespace <ns> --pod <name>

(The `agent` console-script entry comes from `[project.scripts]` in
pyproject.toml. `python -m agent_core` works because of __main__.py.)

Adding a new variant:
    1. Write `register_cli(subparsers)` in your variant package (see
       `pod_launch_task/cli.py` for the pattern).
    2. Import + call it in `_register_variants()` below.
    3. That's it — the variant inherits the API-key check, JSON
       output, verbose vs quiet trajectory, exit-code convention.

Shared flags (`--quiet`) live on the top-level parser, not the
subparsers — they must appear before the subcommand:
    agent --quiet pod-launch ...    ✓
    agent pod-launch --quiet ...    ✗ (argparse rejects)
This matches kubectl/git/docker convention.
"""
from __future__ import annotations

import argparse
import os
import sys

from agent_core.schemas import AgentResult


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent",
        description=(
            "Run a diagnostic agent-task variant. Each variant emits a "
            "structured Findings document (as JSON) on stdout."
        ),
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress per-turn trajectory output; print only the final JSON.",
    )

    subparsers = parser.add_subparsers(
        dest="variant",
        required=True,
        metavar="<variant>",
        help="Which agent variant to run.",
    )
    _register_variants(subparsers)

    args = parser.parse_args(argv)

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

    # Variant handlers run the agent and return an AgentResult; the
    # dispatcher owns serialization so output format stays uniform.
    result: AgentResult = args.handler(args)

    if not args.quiet:
        sys.stderr.write("\n=== FINAL FINDINGS ===\n")
    print(result.model_dump_json(indent=2))
    return 0


def _register_variants(subparsers: argparse._SubParsersAction) -> None:
    """Explicit variant registration. Adding a variant = one import +
    one call here. We chose explicit registration over entry-points
    discovery to keep the variant list visible, ordered, and
    grep-able from this single file.
    """
    from pod_launch_task.cli import register_cli as register_pod_launch

    register_pod_launch(subparsers)


if __name__ == "__main__":
    sys.exit(main())
