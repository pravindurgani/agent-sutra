---
description: Audit a pipeline node against AgentSutra invariants and security standards
allowed-tools: Read, Bash(pytest*), Bash(grep*), Bash(wc*)
---

Audit the pipeline node: $ARGUMENTS

Read the source file thoroughly, then check each of these:

1. **Pipeline invariant:** Does it respect the fixed 5-stage pipeline? No dynamic graph changes, no new stages.
2. **Model usage:** Sonnet for generation, Opus for audit ONLY. If this is the auditor, confirm it uses `config.COMPLEX_MODEL`. If any other node, confirm `config.DEFAULT_MODEL` or `route_and_call()`.
3. **Graceful degradation:** Every feature wrapped in try/except → log warning → continue. Nothing can crash delivery.
4. **Error handling:** Specific exceptions first (APIError, RateLimitError, ConnectionError), broad `Exception as e` only as final fallback with `traceback.format_exc()`. No bare `except:`.
5. **Thread safety:** If accessing shared state (`_live_output`, `_task_stages`, `_sync_db_lock`), confirm proper `with lock:` usage.
6. **Security:** Any new shell commands that should be in the Tier 1 blocklist? Any credential exposure?
7. **Type hints:** Present on all function signatures?
8. **Logging:** Uses `logging` module, never `print()`.

Then run relevant tests:
```
pytest tests/ -v -k "$ARGUMENTS" 2>&1 | tail -20
```

If any sandbox or security changes were made, also run:
```
pytest tests/test_sandbox.py tests/test_stress_v8.py tests/test_stress_v8_audit2.py tests/test_v8_remediation.py -v 2>&1 | tail -20
```

Cross-reference findings against the Known Issues table in CLAUDE.md.

Report: what's correct, what violates invariants, what's missing tests.
