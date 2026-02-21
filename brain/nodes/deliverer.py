from __future__ import annotations

import logging
from pathlib import Path

from brain.state import AgentState
from tools import claude_client

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM = """You are formatting a task result for delivery via Telegram chat.
You receive the original request, the execution output, and context.
Write a polished, structured response.

Formatting rules:
- Start with a clear 1-sentence summary of what was accomplished
- Use sections with headers where helpful (just CAPS or bold-style text)
- Use bullet points (•) for lists
- For code tasks: describe what the code does and key results. Do NOT paste the full source code — it will be attached as a file
- For data/analysis tasks: highlight key findings, numbers, patterns, and insights
- For project tasks: summarize what ran and the meaningful output
- If assertions passed, mention briefly (e.g. "All 5 validation checks passed")
- If there were retries, briefly note what was corrected
- Mention attached files at the end if any
- Keep response under 1800 characters (Telegram limit)
- Be informative, concise, and professional
- Use plain text only (no markdown links, no HTML tags)
- Do NOT include raw tracebacks, stderr, or full code listings"""


def deliver(state: AgentState) -> dict:
    """Format the final response for delivery back to the user via Telegram."""
    task_type = state.get("task_type", "code")
    verdict = state.get("audit_verdict", "pass")
    retry_count = state.get("retry_count", 0)
    artifacts = list(state.get("artifacts", []))

    # Save generated code as a file attachment (only on successful execution)
    if task_type in ("code", "automation", "data", "file") and state.get("code") and verdict == "pass":
        code_file = _save_code_artifact(state)
        if code_file and code_file not in artifacts:
            artifacts.append(code_file)

    # Build context for the summary generator
    execution_result = state.get("execution_result", "")
    execution_output = _extract_output(execution_result)

    # For project tasks, include parameter info
    param_info = ""
    if task_type == "project" and state.get("extracted_params"):
        param_info = f"\nParameters used: {state['extracted_params']}"

    prompt = f"""Original request: {state['message']}

Task type: {task_type}
Status: {"Completed successfully" if verdict == "pass" else f"Completed with issues (after {retry_count} retries)"}
{f"Retry note: {state.get('audit_feedback', '')[:300]}" if retry_count > 0 and verdict == "pass" else ""}
{param_info}

Execution output (stdout):
{execution_output[:3000]}

{f"Code description: {_describe_code(state.get('code', ''))}" if state.get('code') else ""}

Files generated: {', '.join(Path(f).name for f in artifacts if Path(f).exists()) or 'None'}"""

    try:
        summary = claude_client.call(
            prompt,
            system=SUMMARY_SYSTEM,
            max_tokens=800,
            temperature=0.3,
        )
        # Trim to Telegram-safe length
        if len(summary) > 3800:
            summary = summary[:3800] + "..."
    except Exception as e:
        logger.warning("Summary generation failed, using fallback: %s", e)
        summary = _fallback_response(state, artifacts)

    # Append file list at the bottom if not already mentioned
    file_names = [Path(f).name for f in artifacts if Path(f).exists()]
    if file_names and not any(fn in summary for fn in file_names):
        summary += f"\n\nAttached: {', '.join(file_names)}"

    logger.info(
        "Delivery prepared for task %s (%d chars, %d artifacts)",
        state["task_id"],
        len(summary),
        len(artifacts),
    )

    return {"final_response": summary, "artifacts": artifacts}


def _extract_output(execution_result: str) -> str:
    """Extract the meaningful stdout from execution result."""
    if not execution_result:
        return "(no output)"

    # Pull just the Output: section
    if "Output:" in execution_result:
        output = execution_result.split("Output:", 1)[1]
        for separator in ("Stderr:", "Traceback:", "Files created:"):
            if separator in output:
                output = output.split(separator, 1)[0]
        return output.strip() or "(no output)"

    return execution_result[:2000]


def _describe_code(code: str) -> str:
    """Create a brief description of what the code does (without including the code)."""
    lines = code.strip().split("\n")
    imports = [l.strip() for l in lines if l.strip().startswith(("import ", "from "))]
    asserts = sum(1 for l in lines if "assert " in l)
    functions = [l.strip() for l in lines if l.strip().startswith("def ")]

    parts = []
    if imports:
        libs = set()
        for imp in imports:
            if imp.startswith("import "):
                libs.add(imp.split()[1].split(".")[0])
            elif imp.startswith("from "):
                libs.add(imp.split()[1].split(".")[0])
        parts.append(f"Uses: {', '.join(sorted(libs)[:8])}")
    parts.append(f"{len(lines)} lines of Python")
    if functions:
        parts.append(f"{len(functions)} functions defined")
    if asserts:
        parts.append(f"{asserts} assertions")
    return " | ".join(parts)


def _save_code_artifact(state: AgentState) -> str | None:
    """Save generated code to a .py file for attachment."""
    code = state.get("code", "")
    if not code.strip():
        return None

    try:
        # Derive filename from the task message
        message = state.get("message", "script")
        # Create a safe filename from the first few words
        words = "".join(c if c.isalnum() or c == " " else "" for c in message)
        words = "_".join(words.split()[:4]).lower()
        if not words:
            words = "script"

        import config
        output_dir = config.OUTPUTS_DIR
        filename = f"{words}.py"
        filepath = output_dir / filename
        counter = 1
        while filepath.exists():
            filename = f"{words}_{counter}.py"
            filepath = output_dir / filename
            counter += 1

        filepath.write_text(code, encoding="utf-8")
        return str(filepath)
    except Exception as e:
        logger.warning("Failed to save code artifact: %s", e)
        return None


def _fallback_response(state: AgentState, artifacts: list[str]) -> str:
    """Fallback formatting when Claude summary fails."""
    task_type = state.get("task_type", "code")
    verdict = state.get("audit_verdict", "pass")
    retry_count = state.get("retry_count", 0)

    parts = []

    if verdict == "pass":
        if task_type == "project":
            parts.append(f"Project '{state.get('project_name', 'Unknown')}' executed successfully.")
        else:
            parts.append("Task completed successfully.")
    else:
        parts.append(f"Task completed with issues (after {retry_count} retries).")

    # Add execution output summary
    execution_result = state.get("execution_result", "")
    output = _extract_output(execution_result)
    if output and output != "(no output)":
        # Show last meaningful lines
        lines = [l for l in output.strip().split("\n") if l.strip()]
        if len(lines) > 15:
            parts.append("Key output:\n" + "\n".join(lines[-15:]))
        else:
            parts.append(output)

    file_names = [Path(f).name for f in artifacts if Path(f).exists()]
    if file_names:
        parts.append(f"\nAttached: {', '.join(file_names)}")

    return "\n\n".join(parts)
