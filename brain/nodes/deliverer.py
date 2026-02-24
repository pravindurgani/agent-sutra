from __future__ import annotations

import uuid
import logging
from pathlib import Path

from brain.state import AgentState
from tools import claude_client
from storage.db import sync_write_project_memory

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM = """You are formatting a task result for delivery via Telegram chat.
You receive the original request, the execution output, and context.
Write a polished, structured response.

CRITICAL RULE: If the status says FAILED, you MUST clearly state the task failed and explain why.
NEVER claim files were created, saved, or generated unless they are listed under "Files generated".
NEVER fabricate success from a failed execution. Be honest about what happened and what went wrong.

Formatting rules:
- Start with a clear 1-sentence summary of what was accomplished (or what failed)
- Use sections with headers where helpful (just CAPS or bold-style text)
- Use bullet points (•) for lists
- For code tasks: describe what the code does and key results. Do NOT paste the full source code — it will be attached as a file
- For data/analysis tasks: highlight key findings, numbers, patterns, and insights
- For project tasks: summarize what ran and the meaningful output
- For FAILED tasks: state what went wrong, what was attempted, and suggest how to fix it
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

    # Don't attach files from a failed task — they may be wrong or incomplete
    if verdict != "pass":
        artifacts = []

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

    # Build status line and failure context
    if verdict == "pass":
        status_line = "Status: Completed successfully"
        failure_context = ""
        if retry_count > 0:
            status_line += f" (after {retry_count} retries)"
            failure_context = f"\nRetry note: {state.get('audit_feedback', '')[:300]}"
    else:
        status_line = f"Status: FAILED after {retry_count} retries"
        audit_feedback = state.get("audit_feedback", "")
        failure_context = f"""
IMPORTANT: This task FAILED. The audit verdict is FAIL.
Do NOT claim the task succeeded or that files were created unless they appear in "Files generated" below.
Failure reason: {audit_feedback[:500]}"""

    prompt = f"""Original request: {state['message']}

Task type: {task_type}
{status_line}
{failure_context}
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

    try:
        _extract_and_store_memory(state)
    except Exception as e:
        logger.warning("Failed to store project memory: %s", e)

    # Suggest next step based on historical task sequences
    if verdict == "pass" and task_type == "project" and state.get("project_name"):
        suggestion = _suggest_next_step(state["project_name"], state["user_id"])
        if suggestion:
            summary += f"\n\n{suggestion}"

    return {"final_response": summary, "artifacts": artifacts}


def _extract_and_store_memory(state: AgentState) -> None:
    """Extract a success or failure pattern and persist to project_memory."""
    project_name = state.get("project_name")
    if not project_name:
        return  # Only store memories for project-type tasks

    verdict = state.get("audit_verdict", "")
    task_id = state.get("task_id", "")

    if verdict == "pass":
        content = (
            f"Task: {state['message'][:200]}. "
            f"Command used: {state.get('code', '')[:300]}. "
            f"Params: {state.get('extracted_params', {})}."
        )
        sync_write_project_memory(project_name, "success_pattern", content, task_id)
    else:
        feedback = state.get("audit_feedback", "")[:300]
        content = f"Task: {state['message'][:200]}. Failed: {feedback}"
        sync_write_project_memory(project_name, "failure_pattern", content, task_id)


def _suggest_next_step(project_name: str, user_id: int) -> str | None:
    """Infer the most common follow-up task from historical sequences.

    Queries the tasks table for completed project tasks that followed
    the current project within 30 minutes. If the same follow-up has
    occurred 2+ times, suggest it.

    NOTE: This will return None until project_memory has accumulated
    2+ weeks of real task data. That's expected.
    """
    import sqlite3

    query = """
        SELECT t2.message, COUNT(*) as frequency
        FROM tasks t1
        JOIN tasks t2 ON t2.user_id = t1.user_id
            AND t2.created_at > t1.completed_at
            AND julianday(t2.created_at) - julianday(t1.completed_at) < 0.0208
            AND t2.task_type = 'project'
            AND t2.status = 'completed'
        WHERE t1.user_id = ?
            AND t1.task_type = 'project'
            AND t1.message LIKE ?
            AND t1.status = 'completed'
        GROUP BY t2.message
        HAVING COUNT(*) >= 2
        ORDER BY frequency DESC
        LIMIT 1
    """

    try:
        import config as _cfg
        conn = sqlite3.connect(str(_cfg.DB_PATH), timeout=20.0)
        try:
            cursor = conn.execute(query, (user_id, f"%{project_name}%"))
            row = cursor.fetchone()
            if row:
                return f"Suggested next step: You usually run \"{row[0][:100]}\" after this. (seen {row[1]} times)"
            return None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Temporal inference failed: %s", e)
        return None


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
        filename = f"{words}_{uuid.uuid4().hex[:6]}.py"
        filepath = output_dir / filename

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
        parts.append(f"Task FAILED after {retry_count} retries. No output was produced.")
        audit_feedback = state.get("audit_feedback", "")
        if audit_feedback:
            parts.append(f"Failure reason: {audit_feedback[:500]}")

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
