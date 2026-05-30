"""Enable `python -m agent_core <variant> ...` invocation.

Used by the eval harness (which spawns `sys.executable -m agent_core
pod-launch ...`) and by anyone who prefers `python -m` over the
installed `agent` console script.
"""
import sys

from agent_core.cli import main

if __name__ == "__main__":
    sys.exit(main())
