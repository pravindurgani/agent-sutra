---
description: Review staged changes against AgentSutra standards before committing
allowed-tools: Read, Bash(git diff:*), Bash(git status:*), Bash(git log:*), Bash(pytest*), Bash(grep*)
---

## Context
- Staged diff: !`git diff --staged`
- Unstaged changes: !`git diff --stat`
- Current branch: !`git branch --show-current`
- Last 3 commits: !`git log --oneline -3`

Review all staged changes against AgentSutra standards:

**Code quality:**
1. Type hints on all function signatures?
2. Google-style docstrings on public functions?
3. No `print()` — uses `logging` module?
4. No bare `except:` — specific exceptions first?
5. pathlib.Path used, not string paths?

**Safety:**
6. Graceful degradation — any new feature that could crash the pipeline?
7. New shell commands that should be in the Tier 1 blocklist?
8. Credential exposure? Check no `.env`, `.db`, or credential files in staged changes.
9. If AgentState modified — new field initialised in `run_task()`?
10. If auditor touched — still uses `config.COMPLEX_MODEL` only?
11. OWASP top-10 patterns in changed files? (injection, XSS, path traversal, etc.)

**Testing:**
11. Tests for all new/changed functionality?
12. Run: `pytest tests/ -v -k "not docker" 2>&1 | tail -15`

If everything passes, suggest a conventional commit message (feat:/fix:/refactor:/docs:/test:/chore:).
