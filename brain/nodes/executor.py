from __future__ import annotations

import json
import re
import shlex
import uuid
import logging
from pathlib import Path

import config
from brain.state import AgentState
from tools import claude_client
from tools.sandbox import run_code, run_code_with_auto_install, run_shell, ExecutionResult
from tools.file_manager import get_file_content

logger = logging.getLogger(__name__)

CODE_GEN_SYSTEM = """You are an expert programmer. Given a plan, write complete, working code.

Rules:
- Write ONLY the code, no explanations before or after
- Include all imports
- The code must be self-contained and runnable
- Save any output files to the current working directory
- Use descriptive filenames for any generated files
- For charts: save as PNG files using matplotlib with plt.savefig()
- For web projects: create all necessary HTML/CSS/JS files
- Print a summary of what was created to stdout
- Include assert statements to verify your output is correct
- Print "ALL ASSERTIONS PASSED" if all checks succeed
- Handle errors gracefully with try/except

SYSTEM ACCESS: You have full access. You can:
- pip install any library (import subprocess; subprocess.run(["pip3", "install", "package"]))
- Download files via requests, curl, wget
- Access the internet for APIs, web scraping, search
- Read/write files anywhere in the home directory
- Call Ollama at http://localhost:11434 for local AI inference
If a library isn't installed, install it as the first step of your script."""

ANALYSIS_SYSTEM = """You are an expert data analyst. Given a plan and data file paths, write complete Python code.

Rules:
- Write ONLY the code, no explanations
- Use pandas for data processing
- Use matplotlib/seaborn for visualizations
- Save charts as PNG files in the current directory
- Print analysis results and summaries to stdout
- Include assert statements validating data at each step
- Print "ALL ASSERTIONS PASSED" after all validations
- Handle missing data and encoding issues gracefully"""

SHELL_GEN_SYSTEM = """You are an expert at writing shell scripts to orchestrate existing projects.

Given a plan that references existing project commands, write a bash script that:
- Activates the virtual environment if specified
- Changes to the correct working directory
- Runs the commands in the correct order with ALL parameters filled in
- Captures and prints output/results
- Handles errors (exit on first failure)

CRITICAL: All parameters like {file}, {client}, etc. MUST be replaced with actual values.
Do NOT leave any {placeholder} syntax in the script. Use the extracted parameter values.

Write ONLY the bash script. Start with #!/bin/bash and set -e.
Do NOT install packages or write new Python code. Use the project's existing commands."""

UI_DESIGN_SYSTEM_EXEC = """You are an expert front-end developer creating production-quality UI designs.

Write a COMPLETE, self-contained HTML file. Rules:
- Single .html file with all CSS/JS inline or via CDN
- Use Tailwind CSS via CDN: <script src="https://cdn.tailwindcss.com"></script>
- Use Chart.js via CDN if charts/graphs are needed
- Responsive design (mobile-first)
- Professional color scheme and typography
- Include realistic placeholder content
- Add smooth transitions and hover effects
- Write ONLY the HTML code, nothing else
- The file must be self-contained and open directly in any browser"""

FRONTEND_SYSTEM_EXEC = """You are an expert frontend engineer creating production-quality web applications.

Write a COMPLETE, self-contained HTML file with embedded React/JavaScript. Rules:
- Single .html file — ALL code inline or via CDN
- Use Tailwind CSS via CDN: <script src="https://cdn.tailwindcss.com"></script>
- For React apps: use babel-standalone CDN for in-browser JSX:
  <script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <script type="text/babel">// Your React code here</script>
- Use Chart.js CDN if charts/graphs are needed
- Use Heroicons or FontAwesome CDN for icons
- Responsive design (mobile-first, all breakpoints)
- Professional color scheme, typography, and spacing
- Include realistic placeholder data and content
- Add smooth transitions, hover effects, and micro-interactions
- Implement proper component hierarchy and state management
- Write ONLY the HTML code, nothing else
- The file must be self-contained and open directly in any browser"""


def execute(state: AgentState) -> dict:
    """Generate and execute code or shell commands based on the plan."""
    task_type = state.get("task_type", "code")

    if task_type == "project":
        return _execute_project(state)
    elif task_type == "ui_design":
        return _execute_ui_design(state)
    elif task_type == "frontend":
        return _execute_frontend(state)
    else:
        return _execute_code(state)


def _extract_params(state: AgentState) -> dict:
    """Use Claude to extract command parameters from the user's message.

    Returns a dict like {"client": "Light & Wonder", "file": "/path/to/upload.xlsx"}.
    """
    project = state.get("project_config", {})
    commands = project.get("commands", {})

    # Collect all {param} placeholders from all commands
    placeholders = set()
    for cmd in commands.values():
        placeholders.update(re.findall(r"\{(\w+)\}", cmd))

    if not placeholders:
        return {}

    prompt = f"""Extract parameter values from the user's message for a project command.

Parameters needed: {', '.join(sorted(placeholders))}

User message: {state['message']}

Uploaded files: {', '.join(state.get('files', [])) or 'None'}

Rules:
- For "file": use the exact uploaded file path if one exists
- For "client": extract the company/client name from the message
- For other parameters: extract from context if possible
- Return ONLY a JSON object with parameter names as keys

Respond with ONLY valid JSON, e.g.: {{"client": "Light & Wonder", "file": "/path/to/file.xlsx"}}"""

    response = claude_client.call(prompt, system="", max_tokens=200)

    try:
        params = json.loads(response)
        if isinstance(params, dict):
            logger.info("Extracted parameters: %s", params)
            return params
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse parameter extraction: %s", response[:200])

    # Fallback: auto-detect file parameter from uploads
    fallback = {}
    if "file" in placeholders and state.get("files"):
        fallback["file"] = state["files"][0]
    return fallback


def _execute_project(state: AgentState) -> dict:
    """Execute an existing project's commands."""
    project = state.get("project_config", {})
    plan = state.get("plan", "")

    if not project:
        return {
            "code": "",
            "execution_result": "Execution: FAILED\nErrors:\nNo project configuration found",
            "artifacts": [],
            "extracted_params": {},
        }

    project_path = project.get("path", "")
    if not project_path:
        return {
            "code": "",
            "execution_result": "Execution: FAILED\nErrors:\nProject path is not configured in projects.yaml",
            "artifacts": [],
            "extracted_params": {},
        }
    if not Path(project_path).exists():
        return {
            "code": "",
            "execution_result": f"Execution: FAILED\nErrors:\nProject directory not found: {project_path}",
            "artifacts": [],
            "extracted_params": {},
        }

    timeout = project.get("timeout", 300)
    venv = project.get("venv")

    # Extract parameters BEFORE generating the shell script
    params = _extract_params(state)

    # Format commands with extracted parameters for Claude
    raw_commands = project.get("commands", {})
    filled_commands = {}
    for name, cmd in raw_commands.items():
        filled = cmd
        for k, v in params.items():
            filled = filled.replace(f"{{{k}}}", shlex.quote(str(v)))
        filled_commands[name] = filled

    # Ask Claude to generate the exact shell commands from the plan
    prompt = f"""Plan:\n{plan}\n\nOriginal task: {state['message']}

Project path: {project_path}
Available commands (raw templates): {raw_commands}
Extracted parameters: {params}
Commands with parameters filled in: {filled_commands}
Venv path: {venv or 'None'}

IMPORTANT: Use the filled-in commands above. Do NOT leave {{file}} or {{client}} as placeholders."""

    if state.get("files"):
        prompt += "\n\nUploaded files (use these exact paths):"
        for f in state["files"]:
            prompt += f"\n- {f}"

    if state.get("audit_feedback"):
        prompt += f"\n\n--- Previous attempt failed ---\n{state['audit_feedback']}"

    code = claude_client.call(prompt, system=SHELL_GEN_SYSTEM, max_tokens=2000, thinking=True)
    code = _strip_markdown_blocks(code)

    if not code.strip():
        return {
            "code": "",
            "execution_result": "Execution: FAILED\nErrors:\nShell script generation returned empty",
            "artifacts": [],
            "extracted_params": params,
        }

    # Run via shell executor with project's working dir and timeout
    # Use randomized heredoc delimiter to avoid collision with generated code
    delimiter = f"AGENTCORE_EOF_{uuid.uuid4().hex[:8]}"
    result = run_shell(
        command=f"bash -e /dev/stdin <<'{delimiter}'\n{code}\n{delimiter}",
        working_dir=project_path,
        timeout=timeout,
        venv_path=venv,
    )

    # Supplement artifacts: parse stdout for file paths the snapshot may have missed
    artifacts = list(result.files_created)
    known = set(artifacts)
    for line in result.stdout.splitlines():
        for marker in ("HTML saved:", "PDF saved:", "Output:", "Saved:"):
            if marker in line:
                path_str = line.split(marker, 1)[1].strip()
                p = Path(path_str)
                if p.is_file() and str(p) not in known:
                    artifacts.append(str(p))
                    known.add(str(p))

    return {
        "code": code,
        "execution_result": _format_result(result),
        "artifacts": artifacts,
        "extracted_params": params,
        "working_dir": project_path,
    }


def _determine_working_dir(state: AgentState) -> Path | None:
    """Determine the best working directory for execution."""
    # 1. Explicit working_dir in state (set by planner or previous task)
    if state.get("working_dir"):
        wd = Path(state["working_dir"])
        if wd.is_absolute():
            wd.mkdir(parents=True, exist_ok=True)
            return wd

    # 2. If message or plan mentions a specific path, extract it
    for text in [state.get("plan", ""), state.get("message", "")]:
        match = re.search(r'(~/[\w/.-]+|/Users/\w+/[\w/.-]+)', text)
        if match:
            candidate = Path(match.group(1)).expanduser()
            try:
                candidate.resolve().relative_to(config.HOST_HOME.resolve())
                # Treat as directory if no suffix or already exists as a directory
                if not candidate.suffix or candidate.is_dir():
                    candidate.mkdir(parents=True, exist_ok=True)
                    return candidate
            except (ValueError, OSError):
                pass

    # 3. Default: None (sandbox.py will use config.OUTPUTS_DIR)
    return None


def _execute_code(state: AgentState) -> dict:
    """Generate code from the plan and execute it in the sandbox."""
    task_type = state.get("task_type", "code")
    plan = state.get("plan", "")

    system = ANALYSIS_SYSTEM if task_type in ("data", "file") else CODE_GEN_SYSTEM

    prompt = f"Plan:\n{plan}\n\nOriginal task: {state['message']}"

    if state.get("files"):
        prompt += "\n\nAvailable files (use these exact paths):"
        for fpath in state["files"]:
            p = Path(fpath)
            prompt += f"\n- {fpath}"
            if p.exists() and p.suffix in (".csv", ".xlsx", ".tsv", ".parquet", ".json"):
                prompt += "\n  (Data file — process locally with a script. DO NOT load into context)"
            elif p.exists() and p.suffix in (".txt", ".py", ".js", ".md", ".html", ".css"):
                content = get_file_content(p, max_chars=3000)
                prompt += f"\n  Preview:\n{content[:1000]}"

    if state.get("audit_feedback"):
        prompt += f"\n\n--- PREVIOUS CODE FAILED. Fix these issues ---\n{state['audit_feedback']}"
        if state.get("code"):
            prompt += f"\n\n--- Previous code ---\n{state['code']}"

    code = claude_client.call(prompt, system=system, max_tokens=8192, thinking=True)
    code = _strip_markdown_blocks(code)

    if not code.strip():
        return {
            "code": "",
            "execution_result": "Execution: FAILED\nErrors:\nCode generation returned empty output",
            "artifacts": [],
        }

    timeout = _estimate_timeout(state)
    working_dir = _determine_working_dir(state)
    result = run_code_with_auto_install(code, timeout=timeout, working_dir=working_dir)

    return {
        "code": code,
        "execution_result": _format_result(result),
        "artifacts": result.files_created,
        "auto_installed_packages": result.auto_installed,
        "working_dir": str(working_dir) if working_dir else str(config.OUTPUTS_DIR),
    }


def _execute_ui_design(state: AgentState) -> dict:
    """Generate a self-contained HTML file for UI design tasks."""
    plan = state.get("plan", "")

    prompt = f"Plan:\n{plan}\n\nOriginal task: {state['message']}"

    if state.get("files"):
        prompt += "\n\nReference files provided:"
        for fpath in state["files"]:
            p = Path(fpath)
            prompt += f"\n- {fpath}"
            if p.exists() and p.suffix in (".csv", ".txt", ".json", ".html"):
                content = get_file_content(p, max_chars=3000)
                prompt += f"\n  Content preview:\n{content[:1000]}"

    if state.get("audit_feedback"):
        prompt += f"\n\n--- PREVIOUS ATTEMPT FAILED ---\n{state['audit_feedback']}"
        if state.get("code"):
            prompt += f"\n\n--- Previous HTML ---\n{state['code'][:5000]}"

    code = claude_client.call(prompt, system=UI_DESIGN_SYSTEM_EXEC, max_tokens=8192, thinking=True)
    code = _strip_markdown_blocks(code)

    if not code.strip():
        return {
            "code": "",
            "execution_result": "Execution: FAILED\nErrors:\nUI design generation returned empty",
            "artifacts": [],
        }

    # Save the HTML file with dedup to avoid overwriting concurrent outputs
    message = state.get("message", "design")
    words = "".join(c if c.isalnum() or c == " " else "" for c in message)
    base_name = "_".join(words.split()[:4]).lower() or "design"
    filename = f"{base_name}.html"
    output_path = config.OUTPUTS_DIR / filename
    counter = 1
    while output_path.exists():
        filename = f"{base_name}_{counter}.html"
        output_path = config.OUTPUTS_DIR / filename
        counter += 1

    output_path.write_text(code, encoding="utf-8")
    logger.info("UI design saved: %s (%d bytes)", output_path, len(code))

    return {
        "code": code,
        "execution_result": f"Execution: SUCCESS (exit code 0)\nOutput:\nHTML design generated: {filename} ({len(code):,} chars)\nFiles created: {filename}",
        "artifacts": [str(output_path)],
        "working_dir": str(config.OUTPUTS_DIR),
    }


def _execute_frontend(state: AgentState) -> dict:
    """Generate a production-quality frontend application as a self-contained HTML file."""
    plan = state.get("plan", "")

    prompt = f"Plan:\n{plan}\n\nOriginal task: {state['message']}"

    if state.get("files"):
        prompt += "\n\nReference files provided:"
        for fpath in state["files"]:
            p = Path(fpath)
            prompt += f"\n- {fpath}"
            if p.exists() and p.suffix in (".csv", ".txt", ".json", ".html", ".js", ".css"):
                content = get_file_content(p, max_chars=3000)
                prompt += f"\n  Content preview:\n{content[:1000]}"

    if state.get("audit_feedback"):
        prompt += f"\n\n--- PREVIOUS ATTEMPT FAILED ---\n{state['audit_feedback']}"
        if state.get("code"):
            prompt += f"\n\n--- Previous HTML ---\n{state['code'][:5000]}"

    code = claude_client.call(prompt, system=FRONTEND_SYSTEM_EXEC, max_tokens=16000, thinking=True)
    code = _strip_markdown_blocks(code)

    if not code.strip():
        return {
            "code": "",
            "execution_result": "Execution: FAILED\nErrors:\nFrontend generation returned empty",
            "artifacts": [],
        }

    # Save the HTML file with dedup
    message = state.get("message", "app")
    words = "".join(c if c.isalnum() or c == " " else "" for c in message)
    base_name = "_".join(words.split()[:4]).lower() or "app"
    filename = f"{base_name}.html"
    output_path = config.OUTPUTS_DIR / filename
    counter = 1
    while output_path.exists():
        filename = f"{base_name}_{counter}.html"
        output_path = config.OUTPUTS_DIR / filename
        counter += 1

    output_path.write_text(code, encoding="utf-8")
    logger.info("Frontend app saved: %s (%d bytes)", output_path, len(code))

    return {
        "code": code,
        "execution_result": f"Execution: SUCCESS (exit code 0)\nOutput:\nFrontend app generated: {filename} ({len(code):,} chars)\nFiles created: {filename}",
        "artifacts": [str(output_path)],
        "working_dir": str(config.OUTPUTS_DIR),
    }


def _estimate_timeout(state: AgentState) -> int:
    """Estimate appropriate timeout based on task complexity."""
    base = config.EXECUTION_TIMEOUT  # 120s default

    task_type = state.get("task_type", "code")

    # Data tasks with large files get more time
    if task_type == "data" and state.get("files"):
        for f in state["files"]:
            p = Path(f)
            if p.exists() and p.stat().st_size > 10_000_000:  # >10MB
                base = max(base, 300)

    # Frontend and automation tasks need more time
    if task_type in ("frontend", "ui_design", "automation"):
        base = max(base, 300)

    # Cap at MAX_CODE_EXECUTION_TIMEOUT
    return min(base, config.MAX_CODE_EXECUTION_TIMEOUT)


def _strip_markdown_blocks(text: str) -> str:
    """Extract code from markdown code blocks. Returns the longest block found.

    Uses line-based parsing so backticks inside template literals or strings
    don't prematurely close the block. A closing fence must be a line whose
    stripped content is exactly ```.
    """
    blocks: list[str] = []
    current_block: list[str] = []
    in_block = False

    for line in text.split("\n"):
        stripped = line.strip()
        if not in_block:
            if stripped.startswith("```"):
                in_block = True
                current_block = []
        else:
            if stripped == "```":
                blocks.append("\n".join(current_block))
                in_block = False
                current_block = []
            else:
                current_block.append(line)

    if blocks:
        return max(blocks, key=len).strip()
    return text.strip()


def _format_result(result: ExecutionResult) -> str:
    """Format execution result with full traceback info."""
    parts = []
    parts.append(f"Execution: {'SUCCESS' if result.success else 'FAILED'} (exit code {result.return_code})")

    if result.stdout:
        parts.append(f"Output:\n{result.stdout}")
    if result.traceback:
        parts.append(f"Traceback:\n{result.traceback}")
    elif result.stderr:
        parts.append(f"Stderr:\n{result.stderr}")
    if result.files_created:
        parts.append(f"Files created: {', '.join(Path(f).name for f in result.files_created)}")
    if result.timed_out:
        parts.append("WARNING: Execution timed out")

    return "\n".join(parts)
