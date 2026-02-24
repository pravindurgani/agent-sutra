from __future__ import annotations

import json
import logging

from brain.state import AgentState
from tools import claude_client
import config

logger = logging.getLogger(__name__)

SYSTEM_BASE = """You are a STRICT quality auditor for an AI agent system. You are a different model from the one that generated the code, providing adversarial review.

Review the original task, the plan, the generated code, and the execution result.

Respond with ONLY a JSON object:
{
    "verdict": "pass" or "fail",
    "feedback": "Specific explanation of what's wrong and exact fix needed (if fail), or brief confirmation of correctness (if pass)"
}

Be STRICT. Only pass if the output genuinely fulfills the task.

DO NOT FAIL for:
- Deprecation warnings in stderr (these are informational)
- pip install output in stderr (package installation messages)
- Missing "ALL ASSERTIONS PASSED" if the task is a project or shell invocation
- Minor formatting differences from the request
- Warnings that don't affect the actual output

ONLY FAIL for:
- Non-zero exit code WITH actual errors (not just warnings)
- Code that doesn't address the user's actual request
- Missing output files when files were expected
- Obvious logical errors in the output
- Tracebacks indicating crashes"""

AUDIT_CRITERIA = {
    "code": """
Evaluate:
1. Does the code actually accomplish what was asked?
2. Did execution succeed (exit code 0)?
3. Did all assert statements pass? Look for "ALL ASSERTIONS PASSED" in output.
4. Are there tracebacks or errors in stderr?
5. Is the output complete, not truncated?

FAIL if: non-zero exit code, any assertion failed, traceback present, output doesn't match request, obvious logical errors.""",

    "data": """
Evaluate:
1. Does the analysis correctly address the user's question?
2. Did execution succeed (exit code 0)?
3. Did all data validation assertions pass? Look for "ALL ASSERTIONS PASSED".
4. Were output files (charts, CSVs) generated?
5. Are there tracebacks or errors?

FAIL if: non-zero exit code, assertion failures, no output files when expected, traceback present.""",

    "project": """
Evaluate:
1. Did the project command execute successfully (exit code 0)?
2. Were the correct parameters extracted and used (check the command for proper client name, file paths)?
3. Did the command produce expected output files?
4. Is the stdout output meaningful (not empty or error-only)?
5. Were there any errors or warnings that indicate failure?

NOTE: Project commands do NOT use Python assert statements. Do NOT look for "ALL ASSERTIONS PASSED".
Instead, check: exit code 0, expected files created, meaningful output in stdout.

FAIL if: non-zero exit code, wrong parameters used, no output files when expected, error messages in output.""",

    "ui_design": """
Evaluate:
1. Was an HTML file generated?
2. Does the HTML contain proper structure (<!DOCTYPE html>, <html>, <head>, <body>)?
3. Does it include Tailwind CSS (CDN link present)?
4. Does the design address what the user asked for (correct layout, sections, content)?
5. Is it self-contained (no broken external dependencies)?

FAIL if: no HTML file generated, broken HTML structure, missing Tailwind CSS, doesn't match the requested design.""",

    "file": """
Evaluate:
1. Were output files generated as expected?
2. Did execution succeed (exit code 0)?
3. Did file validation assertions pass?
4. Is the output in the correct format?

FAIL if: non-zero exit code, no output files, wrong format, assertion failures.""",

    "automation": """
Evaluate:
1. Did the automation run successfully (exit code 0)?
2. Were the expected results produced?
3. Did all validation assertions pass?
4. Were there connection errors or timeouts?

FAIL if: non-zero exit code, no results produced, assertion failures, unhandled errors.""",

    "frontend": """
Evaluate:
1. Was an HTML file generated?
2. Does the HTML contain proper structure (<!DOCTYPE html>, <html>, <head>, <body>)?
3. Does it include Tailwind CSS (CDN link present)?
4. For React apps: are React, ReactDOM, and Babel CDN scripts included?
5. Does it implement the requested features (components, interactivity, data display)?
6. Is it self-contained (no broken external dependencies, all via CDN)?
7. Is it responsive (mobile-first breakpoints)?

FAIL if: no HTML file generated, broken HTML structure, missing Tailwind/React CDN, doesn't implement requested features.""",
}


def audit(state: AgentState) -> dict:
    """Review execution output against the original task.

    Uses a DIFFERENT model (Opus) than the executor (Sonnet) to prevent
    the echo chamber effect where the same model approves its own work.
    Selects task-type-specific audit criteria for accurate evaluation.
    """
    task_type = state.get("task_type", "code")

    # Short-circuit: detect environment errors that retries cannot fix.
    # These are sandbox/infrastructure failures, not code logic errors.
    execution_result = state.get("execution_result", "")
    env_error = _detect_environment_error(execution_result)
    if env_error:
        logger.warning(
            "Environment error detected for task %s, skipping code-level retry: %s",
            state["task_id"], env_error,
        )
        return {
            "audit_verdict": "fail",
            "audit_feedback": f"ENVIRONMENT ERROR (not a code issue, retrying will not help): {env_error}",
            "retry_count": config.MAX_RETRIES,  # Force skip to delivery
        }

    # Select task-type-specific audit criteria
    criteria = AUDIT_CRITERIA.get(task_type, AUDIT_CRITERIA["code"])
    system = SYSTEM_BASE + "\n" + criteria

    prompt = f"""Original task: {state['message']}

Task type: {task_type}

Plan:
{state.get('plan', 'N/A')[:3000]}

Generated code:
{state.get('code', 'N/A')[:5000]}

Execution result:
{state.get('execution_result', 'N/A')[:5000]}"""

    # Include extracted parameters for project tasks
    if task_type == "project" and state.get("extracted_params"):
        prompt += f"\n\nExtracted parameters: {state.get('extracted_params')}"

    # Use a DIFFERENT model for adversarial review (cross-model verification)
    # Executor uses DEFAULT_MODEL (Sonnet), Auditor uses COMPLEX_MODEL (Opus)
    audit_model = config.COMPLEX_MODEL if config.COMPLEX_MODEL != config.DEFAULT_MODEL else config.DEFAULT_MODEL

    response = claude_client.call(
        prompt,
        system=system,
        model=audit_model,
        max_tokens=800,
        temperature=0.0,
    )

    try:
        parsed = json.loads(response)
        verdict = parsed.get("verdict", "fail")
        feedback = parsed.get("feedback", "")
    except json.JSONDecodeError:
        # Try to extract JSON from response that may have extra text
        json_match = _extract_json(response)
        if json_match:
            verdict = json_match.get("verdict", "fail")
            feedback = json_match.get("feedback", response)
        elif "pass" in response.lower()[:50]:
            verdict = "pass"
            feedback = response
        else:
            # Fail-safe: ambiguous audit response should NOT let bad output through
            verdict = "fail"
            feedback = f"Audit response was unparseable: {response[:300]}"

    retry_count = state.get("retry_count", 0)
    if verdict != "pass":
        retry_count += 1

    logger.info(
        "Audit for task %s: %s (retry %d, model=%s, type=%s)",
        state["task_id"],
        verdict,
        retry_count,
        audit_model,
        task_type,
    )

    return {
        "audit_verdict": verdict,
        "audit_feedback": feedback,
        "retry_count": retry_count,
    }


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object containing 'verdict' from text with possible extra content.

    Uses balanced-brace matching to handle nested braces in feedback strings.
    """
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start:i + 1]
                if '"verdict"' in candidate:
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict) and "verdict" in parsed:
                            return parsed
                    except json.JSONDecodeError:
                        pass
                start = -1
            elif depth < 0:
                # Stray closing brace — reset to avoid poisoning subsequent parsing
                depth = 0
                start = -1
    return None


# ── Environment error detection ───────────────────────────────────

# Patterns that indicate environment/infrastructure failures (not code bugs)
_ENV_ERROR_PATTERNS = [
    # Python can't start due to invalid file descriptors
    ("can't initialize sys standard streams", "Python stdin/stdout initialisation failed (daemon context)"),
    ("Bad file descriptor", "Invalid file descriptor inherited from parent process"),
    # Sandbox execution failures
    ("No space left on device", "Disk full"),
    # Network unreachable in sandbox
    ("Name or service not known", "DNS resolution failed (no network access)"),
    # Execution timeouts — retrying the same code with the same timeout will fail again
    # run_shell returns: "Timed out after {t}s" ; run_code returns: "Execution timed out after {t}s"
    ("Timed out after", "Execution timed out (increasing timeout or optimising the command may help)"),
    ("timed out after", "Execution timed out (increasing timeout or optimising the command may help)"),
    ("killed process group", "Process was killed due to timeout"),
    # NOTE: "Permission denied" and "Connection refused" intentionally excluded.
    # These are frequently code-level errors (wrong path, wrong port) that the
    # audit-retry loop CAN fix. Only truly unrecoverable infrastructure failures
    # belong here.
]


def _detect_environment_error(execution_result: str) -> str | None:
    """Detect environment/infrastructure errors that code retries cannot fix.

    Returns a human-readable description of the environment error, or None
    if the failure appears to be a code logic issue (suitable for retry).
    """
    if not execution_result:
        return None
    for pattern, description in _ENV_ERROR_PATTERNS:
        if pattern in execution_result:
            return description
    return None
