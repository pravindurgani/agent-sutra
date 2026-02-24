# Coding Standards (read by AgentSutra planner)

## Python
- Use pathlib.Path, never os.path
- Always use logging module, never bare print() for status messages
- All functions must have type hints
- No bare except clauses — catch specific exceptions
- Use assert statements for all intermediate validation
- Prefer f-strings over .format()
- Use with statements for all file I/O

## Shell
- Use set -euo pipefail at the top of every bash script
- Quote all variable expansions
