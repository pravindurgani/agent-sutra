---
description: Implement a feature following AgentSutra's design philosophy and invariants
allowed-tools: Read, Write, Edit, Bash(pytest*), Bash(python*), Bash(grep*), Bash(find*)
---

Implement: $ARGUMENTS

Follow this exact sequence:

**1. Understand context first.**
Read files in this order until you have enough context:
config.py → brain/state.py → brain/graph.py → then the specific files you'll modify.
Check CLAUDE.md for known issues — your task may relate to one.

**2. Check existing patterns.**
Before writing new code, grep for similar patterns in the codebase. Match the existing style:
- Functional-first (~70% functions, ~30% classes). Only use classes for dataclasses, Pydantic models, or ABCs.
- Error handling: specific exceptions → broad fallback with traceback → log warning → continue.
- pathlib.Path always, never string paths. `_private_prefix` for internal helpers.

**3. Implement with these constraints:**
- Type hints on ALL function signatures.
- Graceful degradation: wrap in try/except so failure logs a warning and the pipeline continues.
- No speculative abstractions — solve exactly what was asked, nothing more.
- If touching AgentState, update brain/state.py TypedDict AND initialise the field in graph.py run_task().
- If adding security patterns, add both a blocked test AND an allowed test.

**4. Write tests.**
- pytest with descriptive names: `test_should_<expected_behaviour>`.
- Cover happy path, at least one error path, and any security-critical paths.

**5. Verify.**
```
pytest tests/ -v -k "not docker" 2>&1 | tail -30
```

**6. Session log.**
Append an entry to `SESSION_LOG.md` with what was done, decisions made, and next steps.

Report what you changed, what tests you added, and the test results.
