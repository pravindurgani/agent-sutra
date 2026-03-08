# AgentSutra development commands

# Run all tests (625 pass without Docker)
test *ARGS:
    python3 -m pytest tests/ -v {{ARGS}}

# Quick tests — skip Docker, stop on first failure
test-quick:
    python3 -m pytest tests/ -v -k "not docker" -x

# Security-critical tests only
test-security:
    python3 -m pytest tests/test_sandbox.py tests/test_stress_v8.py tests/test_stress_v8_audit2.py tests/test_v8_remediation.py -v

# Lint with ruff
lint:
    ruff check .

# Format with ruff
format:
    ruff format .

# Run the bot
run:
    python3 main.py
