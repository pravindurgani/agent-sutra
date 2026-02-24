# Coding Standards

## Python
- Use pathlib.Path instead of os.path
- Use logging module, never bare print() for diagnostics
- Add type hints to function signatures
- No bare except — always catch specific exceptions
- Use assert statements to verify outputs
- Prefer f-strings over .format() or %
- Use context managers (with) for file and resource handling

## Shell
- Start scripts with set -euo pipefail
- Quote all variable expansions: "$var" not $var
- Use absolute paths where possible
