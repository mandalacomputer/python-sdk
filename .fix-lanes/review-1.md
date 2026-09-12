# review 1

- base: `origin/main`
- head: `55c5b1d` on `OPL-4804-mid-run-auth-refusals`
- when: 2026-09-12T22:25:59Z

# Codex Adversarial Review

Target: branch diff against origin/main
Verdict: needs-attention

A refusal can still lose completed steps from e.agent. All 114 targeted tests pass, but miss this case.

Findings:
- [medium] An empty steps list shadows the actual steps_taken record (src/mandala_computer/_agent.py:245-247)
  Reproduced through agent_once: a 403 body containing steps: [] and steps_taken: [{"n": 1, "action": "left_click"}] produces e.agent.steps == (). The attachment gate accepts steps_taken, but this converter selects steps instead. If a server emits both aliases with an empty default steps field, callers lose the completed-action record from e.agent despite it remaining in e.body. This server shape is a compatibility scenario, not established current server behavior.
  Recommendation: Normalize the HTTP payload in _attach_agent_partial using the same steps_taken-first selection as its gate, preserving SSE precedence. Add sync/async regression cases with empty steps and populated steps_taken.

Next steps:
- Fix the inconsistent step selection and rerun agent, parity, and SSE tests.
