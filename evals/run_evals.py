"""Eval harness + structural verifier for pod-launch scenarios.

For each scenario in `evals/scenarios/pod-launch/*.expected.yaml`:
  1. Apply the fixture (kubectl, via `make scenario-apply SCENARIO=<name>`)
  2. Run the agent (`python -m agent_core --quiet pod-launch --namespace <ns> --pod <name>`)
  3. Parse the agent's stdout into a typed `AgentResult`
  4. Apply the `.expected.yaml` spec's structural checks against the result
  5. Clean up (`make scenario-clean SCENARIO=<name>`)

Exits 0 if all scenarios pass; non-zero if any check failed or the agent
crashed.

Usage:
    python evals/run_evals.py                   # all scenarios, human output
    python evals/run_evals.py --scenario NAME   # one scenario
    python evals/run_evals.py --json            # machine-readable output
    python evals/run_evals.py --skip-apply      # assume fixture is already applied
    python evals/run_evals.py --skip-clean      # leave fixture for manual inspection

Invoked via `make eval` from repo root, which runs this through the
agent-task venv so `agent_core` and the variant packages are importable.

The .expected.yaml schema this verifier understands (all fields optional):
  target:
    scenarioName: <str>           # documentation; not asserted
    namespace: <str>              # passed to the agent
    podName: <str>                # passed to the agent
  expected:
    outputKind: Findings          # asserts agent emitted a Findings (not None)
    confidenceAtLeast: <low|medium|high>
    problemMustMention:
      anyOf: [<str>, ...]         # at least one substring (case-insensitive)
    problemMustNotMention: [<str>, ...]   # none of these substrings
    remediationsMustInclude:
      anyOf: [<str>, ...]         # checked against concatenated remediation.action
    improvementsMustIncludeCategory:
      anyOf: [<str>, ...]         # checked against improvement.category (case-sensitive)
    alsoCheckMustInclude:
      nonEmpty: true              # alsoCheck list has ≥1 entry
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from agent_core.schemas import AgentResult, Findings

# ─── Constants ────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = REPO_ROOT / "evals" / "scenarios" / "pod-launch"
AGENT_DIR = REPO_ROOT / "agent-task"

CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# How long to allow the agent to run before we give up. Most scenarios
# finish in 30-90s; latency-lens scenarios may push higher because the
# fixture itself takes ~50s to reach Ready. 5 min is generous.
AGENT_TIMEOUT_SECONDS = 300

# How long to wait for a `make scenario-apply` to reach a stuck state.
# The Makefile itself caps at 120s; this gives a small buffer.
APPLY_TIMEOUT_SECONDS = 180

# ─── Models ────────────────────────────────────────────────────────────────────


class CheckResult(BaseModel):
    """Result of one structural check against one scenario's spec."""

    name: str
    passed: bool
    detail: str = ""


class VerificationReport(BaseModel):
    """Per-scenario verification result. Aggregates all CheckResults plus
    a small set of runtime statistics from the agent's metrics."""

    scenario: str
    overall_passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    agent_cost_usd: float = 0.0
    agent_turns: int | None = None
    agent_wall_clock_seconds: float = 0.0
    crash_reason: str | None = None  # populated if the harness itself failed


# ─── Verifier ─────────────────────────────────────────────────────────────────


class Verifier:
    """Applies the structural checks in a `.expected.yaml` spec against an
    AgentResult. Pure function — no cluster, no subprocess. Designed to be
    unit-testable against curated AgentResult JSON without applying any
    fixture.
    """

    def verify(
        self,
        scenario: str,
        result: AgentResult,
        spec: dict[str, Any],
    ) -> VerificationReport:
        expected = spec.get("expected", {}) or {}
        findings = result.findings
        checks: list[CheckResult] = []

        # outputKind is the first gate — if the agent didn't emit a
        # Findings (parsed as None or error), downstream checks are
        # vacuously meaningless.
        checks.append(self._check_output_kind(expected, findings))

        if findings is None:
            return VerificationReport(
                scenario=scenario,
                overall_passed=False,
                checks=checks,
                agent_cost_usd=result.metrics.cost_usd,
                agent_turns=result.metrics.turns_used,
                agent_wall_clock_seconds=result.metrics.wall_clock_seconds,
            )

        if "confidenceAtLeast" in expected:
            checks.append(self._check_confidence_at_least(expected, findings))

        if "problemMustMention" in expected:
            checks.append(
                self._check_must_mention(
                    name="problemMustMention.anyOf",
                    terms=(expected["problemMustMention"] or {}).get("anyOf", []),
                    text=findings.problem,
                )
            )

        if "problemMustNotMention" in expected:
            checks.append(
                self._check_must_not_mention(
                    name="problemMustNotMention",
                    forbidden=expected["problemMustNotMention"] or [],
                    text=findings.problem,
                )
            )

        if "remediationsMustInclude" in expected:
            remediation_text = "\n".join(r.action for r in findings.remediations)
            checks.append(
                self._check_must_mention(
                    name="remediationsMustInclude.anyOf",
                    terms=(expected["remediationsMustInclude"] or {}).get("anyOf", []),
                    text=remediation_text,
                )
            )

        if "improvementsMustIncludeCategory" in expected:
            checks.append(
                self._check_improvements_category(
                    expected=expected["improvementsMustIncludeCategory"] or {},
                    findings=findings,
                )
            )

        if "alsoCheckMustInclude" in expected:
            also_spec = expected["alsoCheckMustInclude"] or {}
            if also_spec.get("nonEmpty"):
                non_empty = len(findings.alsoCheck) > 0
                checks.append(
                    CheckResult(
                        name="alsoCheckMustInclude.nonEmpty",
                        passed=non_empty,
                        detail=f"alsoCheck has {len(findings.alsoCheck)} item(s)",
                    )
                )

        overall = all(c.passed for c in checks)
        return VerificationReport(
            scenario=scenario,
            overall_passed=overall,
            checks=checks,
            agent_cost_usd=result.metrics.cost_usd,
            agent_turns=result.metrics.turns_used,
            agent_wall_clock_seconds=result.metrics.wall_clock_seconds,
        )

    # ─── Individual check methods ──────────────────────────────────────────────

    def _check_output_kind(
        self, expected: dict[str, Any], findings: Findings | None
    ) -> CheckResult:
        wanted = expected.get("outputKind", "Findings")
        if wanted != "Findings":
            return CheckResult(
                name="outputKind",
                passed=False,
                detail=(
                    f"unsupported outputKind={wanted!r}; "
                    "verifier only understands 'Findings' today"
                ),
            )
        passed = findings is not None
        if passed:
            detail = "agent emitted Findings"
        else:
            detail = (
                "agent did not emit Findings — likely an error termination "
                "(check termination field in metrics) or a parse failure"
            )
        return CheckResult(name="outputKind", passed=passed, detail=detail)

    def _check_confidence_at_least(
        self, expected: dict[str, Any], findings: Findings
    ) -> CheckResult:
        wanted = expected["confidenceAtLeast"]
        actual = findings.confidence
        wanted_rank = CONFIDENCE_ORDER.get(wanted, -1)
        actual_rank = CONFIDENCE_ORDER.get(actual, -1)
        if wanted_rank < 0:
            return CheckResult(
                name="confidenceAtLeast",
                passed=False,
                detail=f"spec value {wanted!r} is not in {{low,medium,high}}",
            )
        passed = actual_rank >= wanted_rank
        return CheckResult(
            name="confidenceAtLeast",
            passed=passed,
            detail=f"wanted ≥{wanted}, got {actual}",
        )

    def _check_must_mention(
        self, name: str, terms: list[Any], text: str
    ) -> CheckResult:
        """Case-insensitive substring check: pass if ANY term appears in text.

        Terms are coerced to strings so YAML scalar quirks (e.g., a bare
        `45` parsed as int, or `env:` parsed as a dict) don't crash the
        verifier — they just get stringified and matched as-text. The
        spec author can later quote them to be explicit if desired.
        """
        if not terms:
            # Empty anyOf list is vacuously satisfied — spec authors signal
            # "no constraint" by omitting the field, not by emptying it.
            return CheckResult(
                name=name,
                passed=True,
                detail="anyOf list was empty (no constraint)",
            )
        text_lower = text.lower()
        # Coerce defensively — see docstring.
        terms_str = [str(t) for t in terms]
        matched = [t for t in terms_str if t.lower() in text_lower]
        if matched:
            return CheckResult(
                name=name,
                passed=True,
                detail=f"matched: {matched}",
            )
        return CheckResult(
            name=name,
            passed=False,
            detail=f"none of {terms} appeared in text",
        )

    def _check_must_not_mention(
        self, name: str, forbidden: list[Any], text: str
    ) -> CheckResult:
        """Case-insensitive substring check: pass if NONE of the forbidden
        terms appear. Catches "the agent hallucinated wrong-phase
        diagnosis" — e.g., an image-pull scenario should never name
        PodDisruptionBudget. Terms are coerced to str (see
        _check_must_mention)."""
        text_lower = text.lower()
        forbidden_str = [str(t) for t in forbidden]
        violations = [t for t in forbidden_str if t.lower() in text_lower]
        if violations:
            return CheckResult(
                name=name,
                passed=False,
                detail=f"forbidden terms found: {violations}",
            )
        return CheckResult(
            name=name,
            passed=True,
            detail="no forbidden terms appeared",
        )

    def _check_improvements_category(
        self, expected: dict[str, Any], findings: Findings
    ) -> CheckResult:
        """At least one improvement.category must match one of the spec's
        anyOf list. Category match is case-sensitive (category strings are
        structured names like 'pod_spec', 'admission_control')."""
        wanted = expected.get("anyOf", []) or []
        if not wanted:
            return CheckResult(
                name="improvementsMustIncludeCategory.anyOf",
                passed=True,
                detail="anyOf list was empty (no constraint)",
            )
        categories = [imp.category for imp in findings.improvements]
        matched = [c for c in categories if c in wanted]
        if matched:
            return CheckResult(
                name="improvementsMustIncludeCategory.anyOf",
                passed=True,
                detail=f"matched: {matched}",
            )
        return CheckResult(
            name="improvementsMustIncludeCategory.anyOf",
            passed=False,
            detail=f"wanted any of {wanted}; got categories {categories}",
        )


# ─── Harness ──────────────────────────────────────────────────────────────────


class Harness:
    """Orchestrates apply → run → verify → clean for each scenario.

    Each scenario lifecycle:
      1. `make scenario-apply SCENARIO=<name>` (unless --skip-apply)
      2. `python -m pod_launch_task --namespace ... --pod ... --quiet`
      3. Parse stdout as AgentResult; verify against the spec.
      4. `make scenario-clean SCENARIO=<name>` (unless --skip-clean) — runs
         in `finally`, so failures during steps 1-3 still trigger cleanup.

    Crashes in any phase are captured as a failed VerificationReport with
    `crash_reason` populated. The sweep continues across remaining
    scenarios; only the failed scenario is marked failed.
    """

    def __init__(
        self,
        verifier: Verifier,
        skip_apply: bool = False,
        skip_clean: bool = False,
    ):
        self.verifier = verifier
        self.skip_apply = skip_apply
        self.skip_clean = skip_clean

    def discover_scenarios(self) -> list[str]:
        """Find scenarios = every `<name>.yaml` that has a sibling
        `<name>.expected.yaml`. Sorted alphabetically."""
        scenarios: list[str] = []
        for yaml_path in sorted(SCENARIO_DIR.glob("*.yaml")):
            name = yaml_path.stem
            if name.endswith(".expected"):
                continue
            expected_path = SCENARIO_DIR / f"{name}.expected.yaml"
            if expected_path.exists():
                scenarios.append(name)
        return scenarios

    def run_scenario(self, scenario_name: str) -> VerificationReport:
        spec_path = SCENARIO_DIR / f"{scenario_name}.expected.yaml"
        with open(spec_path) as f:
            spec = yaml.safe_load(f) or {}
        target = spec.get("target", {}) or {}
        namespace = target.get("namespace")
        pod_name = target.get("podName")
        if not namespace or not pod_name:
            return self._crashed_report(
                scenario_name,
                f"spec is missing target.namespace or target.podName: {target}",
            )

        try:
            if not self.skip_apply:
                self._scenario_apply(scenario_name)
            result = self._run_agent(namespace, pod_name)
            return self.verifier.verify(scenario_name, result, spec)
        except subprocess.TimeoutExpired as e:
            return self._crashed_report(
                scenario_name, f"subprocess timeout: {e.cmd!r} after {e.timeout}s"
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "")[:500] if isinstance(e.stderr, str) else ""
            return self._crashed_report(
                scenario_name,
                f"subprocess failed: {e.cmd!r} exit={e.returncode}; stderr: {stderr}",
            )
        except ValidationError as e:
            return self._crashed_report(
                scenario_name,
                f"agent output failed AgentResult.model_validate_json: {e}",
            )
        except Exception as e:
            return self._crashed_report(
                scenario_name, f"unexpected harness error: {type(e).__name__}: {e}"
            )
        finally:
            if not self.skip_clean:
                self._scenario_clean(scenario_name)

    # ─── Subprocess helpers ────────────────────────────────────────────────────

    def _scenario_apply(self, scenario_name: str) -> None:
        subprocess.run(
            ["make", "scenario-apply", f"SCENARIO={scenario_name}"],
            check=True,
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,  # capture stderr in case of failure
            timeout=APPLY_TIMEOUT_SECONDS,
        )

    def _scenario_clean(self, scenario_name: str) -> None:
        # check=False here — cleanup failures are noisy but not fatal.
        subprocess.run(
            ["make", "scenario-clean", f"SCENARIO={scenario_name}"],
            check=False,
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=APPLY_TIMEOUT_SECONDS,
        )

    def _run_agent(self, namespace: str, pod_name: str) -> AgentResult:
        """Invokes the agent via `python -m agent_core pod-launch ...` in
        the same Python interpreter (sys.executable). The interpreter
        has the agent-task package installed because the Makefile
        activates the agent-task venv for `make eval`.

        Note `--quiet` is a top-level flag (owned by `agent_core.cli`),
        so it appears BEFORE the `pod-launch` subcommand. This is
        git-style; argparse rejects the reverse order.

        Captures stdout (the AgentResult JSON) and parses it via
        Pydantic. stderr (the verbose trajectory) is discarded — the
        harness only cares about the structured result.
        """
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_core",
                "--quiet",
                "pod-launch",
                "--namespace",
                namespace,
                "--pod",
                pod_name,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT_SECONDS,
        )
        return AgentResult.model_validate_json(completed.stdout)

    def _crashed_report(self, scenario: str, reason: str) -> VerificationReport:
        return VerificationReport(
            scenario=scenario,
            overall_passed=False,
            checks=[
                CheckResult(name="harness", passed=False, detail=reason),
            ],
            crash_reason=reason,
        )


# ─── CLI / output ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run pod-launch scenario evals end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scenario",
        help="Run only this scenario; default: discover and run all",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable output",
    )
    parser.add_argument(
        "--skip-apply",
        action="store_true",
        help="Assume the fixture is already applied (useful for iteration)",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Leave the fixture in place after running (useful for inspection)",
    )
    args = parser.parse_args(argv)

    verifier = Verifier()
    harness = Harness(verifier, skip_apply=args.skip_apply, skip_clean=args.skip_clean)

    if args.scenario:
        scenarios = [args.scenario]
    else:
        scenarios = harness.discover_scenarios()

    if not scenarios:
        print("No scenarios found.", file=sys.stderr)
        return 2

    reports: list[VerificationReport] = []
    for sc in scenarios:
        if not args.json:
            print(f"[{sc}] running...", file=sys.stderr, flush=True)
        report = harness.run_scenario(sc)
        reports.append(report)
        if not args.json:
            status = "PASS" if report.overall_passed else "FAIL"
            print(
                f"[{sc}] {status} (cost ${report.agent_cost_usd:.3f}, "
                f"turns {report.agent_turns}, wall {report.agent_wall_clock_seconds:.1f}s)",
                file=sys.stderr,
                flush=True,
            )

    if args.json:
        _emit_json_report(reports)
    else:
        _emit_human_report(reports)

    failed = sum(1 for r in reports if not r.overall_passed)
    return 0 if failed == 0 else 1


def _emit_json_report(reports: list[VerificationReport]) -> None:
    payload = {
        "reports": [r.model_dump() for r in reports],
        "summary": {
            "total": len(reports),
            "passed": sum(1 for r in reports if r.overall_passed),
            "failed": sum(1 for r in reports if not r.overall_passed),
            "total_cost_usd": round(
                sum(r.agent_cost_usd for r in reports), 6
            ),
            "total_wall_clock_seconds": round(
                sum(r.agent_wall_clock_seconds for r in reports), 1
            ),
        },
    }
    print(json.dumps(payload, indent=2))


def _emit_human_report(reports: list[VerificationReport]) -> None:
    print()
    for r in reports:
        status = "PASS" if r.overall_passed else "FAIL"
        marker = "✓" if r.overall_passed else "✗"
        print(
            f"{marker} {r.scenario}  [{status}]  "
            f"turns={r.agent_turns}  cost=${r.agent_cost_usd:.3f}  "
            f"wall={r.agent_wall_clock_seconds:.1f}s"
        )
        if r.crash_reason:
            print(f"    HARNESS CRASH: {r.crash_reason}")
        for c in r.checks:
            check_marker = "✓" if c.passed else "✗"
            detail = c.detail
            if len(detail) > 220:
                detail = detail[:220] + "..."
            print(f"    {check_marker} {c.name}: {detail}")
        print()

    total = len(reports)
    passed = sum(1 for r in reports if r.overall_passed)
    total_cost = sum(r.agent_cost_usd for r in reports)
    total_wall = sum(r.agent_wall_clock_seconds for r in reports)
    print(
        f"=== Summary: {passed}/{total} passed  |  "
        f"total agent cost ${total_cost:.3f}  |  "
        f"total wall-clock {total_wall:.1f}s ==="
    )


if __name__ == "__main__":
    sys.exit(main())
