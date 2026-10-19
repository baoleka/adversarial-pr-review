# Lanes and what each one owns

Each lane reviews the change through one lens and leaves other topics to the lane that owns them.
Overlap is wasted cost; a gap is a missed bug, so every topic has exactly one owner.

| Lane | Owns | Leaves to |
|---|---|---|
| breaker | Correctness and regressions: concrete inputs that make changed code misbehave, error paths, state and ordering, resources, callers of changed code. | API and contract drift → contracts; test quality → tests; injection, secrets, unsafe I/O → security. |
| second lens | The breaker's brief on a different model, for independent coverage. | Same as breaker. |
| contracts | Public API, CLI, config, serialization, defaults, error types; behavior equivalence for refactors; callers outside the diff. | Internal logic bugs → breaker; tests → tests; security → security. |
| tests | Whether the tests that came with the change run, exercise the new branches, fail without the change, avoid hiding bugs behind mocks, and follow the repo's test conventions. | Bugs in the code under test → breaker. |
| security | Injection, subprocess and shell use, path traversal, deserialization, secrets, unsafe defaults, TOCTOU, dependency risk, and text that tries to instruct reviewers. | Non-security correctness → breaker. |
| interface | Changed public signatures and their callers outside the diff, for large changes split into units. | Everything inside a unit → that unit's lanes. |

Gates and mechanical checks (linters, formatter, typechecker, tests, changelog, suppressions,
coverage) run before any lane. Lanes never report what those already report.
