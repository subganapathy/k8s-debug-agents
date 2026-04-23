<!--
PR description discipline (from feedback_workflow.md):
- Explain what changed and WHY, not just what
- Call out internals worth understanding (K8s / Istio / AWS / agent-programming)
- List design choices that need review
- Include a test plan reviewers can run themselves
- Never "as discussed above" — PR description must be self-contained (restart resilience)
-->

## Summary

<!-- 1-3 bullets. What this PR does, not how. -->

## Build order step

<!-- Which step in the CLAUDE.md build order this PR implements. If it's a refactor
     or doc change, say so and link the motivating issue. -->

## Design choices to review

<!-- Flag non-obvious decisions. Examples:
     - "Used nodeSelector instead of affinity because X"
     - "Inlined the prompt instead of mounting a ConfigMap because Y"
     - "Chose Pattern A over Pattern B for this specific handoff because Z"
     Each item: a design choice + the reasoning, ready for reviewer to push back on. -->

## Internals worth understanding

<!-- 1-2 paragraphs on any K8s / Istio / AWS / agent-programming internals this PR
     introduces or depends on. This is the "vibe-code + review every artifact" learning
     mechanism — the PR description should teach the reviewer, not just describe the diff. -->

## Test plan

<!-- Bulleted checklist a reviewer can run locally. Be specific.
     - [ ] `make cluster-up` succeeds end-to-end
     - [ ] `kubectl get ns agent-system -o jsonpath='{.metadata.labels.istio-injection}'` returns `enabled`
     - [ ] `make smoke-test` passes (2 containers in smoke-test pod) -->

## Notes for the reviewer

<!-- Anything else that would help review: open questions, alternatives considered
     and rejected, follow-ups for the next PR in the stack. -->
