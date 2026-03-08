---
description: Fix a known issue from the v8 audit by ID (M-1, M-2, M-3, m-1 through m-4)
allowed-tools: Read, Write, Edit, Bash(pytest*), Bash(grep*)
---

Fix audit issue: $ARGUMENTS

1. Read CLAUDE.md "Known Issues" section to find the issue details.
2. Read the affected source file.
3. Apply the minimal fix — do NOT refactor surrounding code.
4. Write a test that would have caught this issue.
5. Run: `pytest tests/ -v -k "not docker" 2>&1 | tail -15`
6. Update the Known Issues table in CLAUDE.md — mark the issue as **FIXED** with version.
7. Report: what you changed, the test you added, and confirm all tests pass.

Quick reference:
- **M-1**: FIFO cap in `sync_write_project_memory()` — DELETE oldest keeping newest 50 per project.
- **M-2**: `.resolve()` + `startswith(project_path.resolve())` in `_inject_project_files()`.
- **M-3**: Wrap `open(p, "rb")` in chain artifact delivery with `with` context manager.
- **m-2**: Use Ollama's native `system` field instead of concatenating into `prompt`.
- **m-3**: Pass `max_tokens` to Ollama via `options.num_predict`.
- **m-4**: Import `MODEL_COSTS` from `claude_client` instead of duplicating in `model_router`.
