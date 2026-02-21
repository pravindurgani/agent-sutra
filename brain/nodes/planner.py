from __future__ import annotations

import logging
from pathlib import Path

import config
from brain.state import AgentState
from tools import claude_client
from tools.file_manager import get_file_content, format_file_metadata_for_prompt
from tools.projects import get_project_context

logger = logging.getLogger(__name__)

# ── Shared capability blocks injected into ALL system prompts ─────────

TDD_INSTRUCTION = """
CRITICAL: Write assert statements in your code to verify correctness.
- For data tasks: assert row counts, column names, value ranges after each operation
- For code tasks: include at least 2 assert statements validating output
- For file tasks: assert output files exist and are non-empty
- Print "ALL ASSERTIONS PASSED" at the end if everything succeeds
These assertions act as built-in tests. If any fail, the auditor will catch it."""

CAPABILITIES_BLOCK = """
SYSTEM CAPABILITIES (you have full access):
- INTERNET: You have full internet access via requests, beautifulsoup4, duckduckgo-search
  - Scrape websites, call REST APIs, download files
  - Search the web: from duckduckgo_search import DDGS; results = DDGS().text("query", max_results=5)
- RUNTIME INSTALLS: You can pip install any library at runtime
  - subprocess.run(["pip3", "install", "package_name"], check=True)
  - Always install before importing if a library might not be present
- LOCAL AI MODELS (Ollama at http://localhost:11434):
  - Pull models: subprocess.run(["ollama", "pull", "model_name"])
  - Generate: requests.post("http://localhost:11434/api/generate", json={{"model": "...", "prompt": "..."}})
  - Use local models when instructed or for offline/private processing
- FILESYSTEM: Full read/write access to the entire home directory
  - Can create, read, edit, delete any file under ~/
  - Can navigate project directories, read configs, inspect code
- SHELL: Can run any bash command — git, npm, brew, docker, etc.

BIG DATA RULES (CRITICAL for large datasets):
- If the user uploads or references a large dataset (thousands+ rows), NEVER load raw data into context
- Write a local Python script using pandas or duckdb to process the file locally
- Extract insights, compute statistics, and print ONLY the summary to stdout
- For very large files (100k+ rows), prefer duckdb over pandas for memory efficiency
- Always use openpyxl engine for Excel files: pd.read_excel(path, engine="openpyxl")
"""

# ── Task-type specific system prompts ─────────────────────────────────

PROJECT_SYSTEM = """You are an expert at orchestrating existing software projects.

{project_context}

Your job is to create a plan that uses the project's EXISTING commands.
Do NOT write new code from scratch. Use the commands listed above.

PARAMETER EXTRACTION (CRITICAL):
The project commands use placeholder parameters like {{file}}, {{client}}, {{keyword}}, etc.
You MUST extract these values from the user's message and the uploaded file paths.
- If the user mentions a client/company name (e.g. "Light & Wonder", "Kambi"), that is the {{client}} parameter.
- If uploaded files are listed, use the EXACT file path as the {{file}} parameter.
- If you cannot determine a required parameter, state clearly what is missing.

REFERENCE FILE SEARCH:
If the user mentions a template, past report, reference file, or "similar to X":
- Include a step to search for it: find ~/ -type f -name '*keyword*' -maxdepth 5
- Look in the project directory first, then expand the search.
- Use the found reference to guide how to run the command.
""" + CAPABILITIES_BLOCK + """
Your plan must:
1. List the extracted parameters and their values
2. Identify which command(s) to run and in what order (with parameters filled in)
3. Specify any prerequisites (venv, env vars, running services)
4. Describe what output to expect

Output a clear numbered plan. Each step should specify the exact shell command to run with ALL parameters filled in."""

CODE_SYSTEM = """You are an expert software architect and developer. Given a task, create a precise execution plan.

Your plan must include:
1. What language/framework to use
2. File structure (if multi-file)
3. Step-by-step implementation details
4. Expected output format
5. Assert statements to verify correctness

{tdd}
""" + CAPABILITIES_BLOCK + """
Be specific. Write the plan so a code generator can follow it exactly.
Output the plan in clear numbered steps."""

DATA_SYSTEM = """You are a data analysis expert. Given a task and data file info, create a precise analysis plan.

Your plan must include:
1. What libraries to use (pandas, duckdb, matplotlib, etc.)
2. Data loading and cleaning steps
3. Analysis operations with specific column references
4. Output format (charts, tables, summary text)
5. Assert statements to verify data integrity at each step

{tdd}
""" + CAPABILITIES_BLOCK + """
Be specific about column names if file content is provided."""

FILE_SYSTEM = """You are a file processing expert. Given a task, create a precise file transformation plan.

Your plan must include:
1. Input file format detection
2. Transformation steps
3. Output file format and naming
4. Assert statements verifying output file exists and has correct format

{tdd}
""" + CAPABILITIES_BLOCK

AUTOMATION_SYSTEM = """You are an automation expert. Given a task, create a precise automation plan.

Your plan must include:
1. What to automate (scraping, API calls, etc.)
2. Required libraries (install with pip if needed)
3. Step-by-step process
4. Output/report format
5. Error handling and retry strategy
6. Assert statements validating results

{tdd}
""" + CAPABILITIES_BLOCK

UI_DESIGN_SYSTEM = """You are an expert UI/UX designer and front-end developer.
Given a task, create a plan for generating a self-contained HTML file.

Your plan must include:
1. Layout structure (header, hero, sections, footer)
2. Visual design decisions (color scheme, typography, spacing)
3. Components to include (cards, charts, tables, navigation, forms)
4. Responsive design considerations (mobile-first breakpoints)
5. Technology: single HTML file using Tailwind CSS (CDN), Chart.js if charts needed, inline JavaScript

{tdd}

The output MUST be a single self-contained .html file that opens directly in a browser.
Use Tailwind CSS via CDN link, not npm. All styles and scripts inline.
Be specific about exact Tailwind classes and layout decisions."""

FRONTEND_SYSTEM = """You are an expert frontend engineer creating production-quality web applications.

Given a task, create a detailed implementation plan.

Your plan must include:
1. Application architecture (components, data flow, state management)
2. Technology stack decision:
   - Simple one-page: single HTML + Tailwind CSS CDN + Chart.js
   - Complex interactive: React via CDN (babel-standalone) + Tailwind CDN in a single HTML
   - Full project: proper multi-file structure (HTML, CSS, JS modules)
3. Component hierarchy and layout structure
4. Responsive design breakpoints (mobile-first)
5. Data handling (realistic placeholders, API mocking if needed)
6. Animations, transitions, and micro-interactions
7. Accessibility considerations

{tdd}
""" + CAPABILITIES_BLOCK + """
Output MUST be self-contained and openable directly in any browser.
For React: use babel-standalone CDN for JSX transformation in-browser.
For charts: use Chart.js CDN. For icons: use Heroicons or FontAwesome CDN.
Be specific about exact component structure and Tailwind classes."""


def plan(state: AgentState) -> dict:
    """Create an execution plan based on task type and user message."""
    task_type = state.get("task_type", "code")

    # Build system prompt based on task type
    if task_type == "project":
        project = state.get("project_config", {})
        project_context = get_project_context(project) if project else "No project context available."
        system = PROJECT_SYSTEM.format(project_context=project_context)
    elif task_type == "frontend":
        system = FRONTEND_SYSTEM.format(tdd=TDD_INSTRUCTION)
    elif task_type == "ui_design":
        system = UI_DESIGN_SYSTEM.format(tdd=TDD_INSTRUCTION)
    elif task_type == "data":
        system = DATA_SYSTEM.format(tdd=TDD_INSTRUCTION)
    elif task_type == "file":
        system = FILE_SYSTEM.format(tdd=TDD_INSTRUCTION)
    elif task_type == "automation":
        system = AUTOMATION_SYSTEM.format(tdd=TDD_INSTRUCTION)
    else:
        system = CODE_SYSTEM.format(tdd=TDD_INSTRUCTION)

    prompt = f"Task: {state['message']}"

    # Include conversation context if available
    if state.get("conversation_context"):
        prompt += f"\n\nCONVERSATION CONTEXT (recent history):\n{state['conversation_context']}"

    # Smart file context: metadata-only for big data files, full content for small/code files
    if state.get("files"):
        for fpath in state["files"]:
            p = Path(fpath)
            if not p.exists():
                continue
            if p.suffix in (".csv", ".tsv", ".xlsx", ".parquet", ".json"):
                from tools.file_manager import get_file_metadata
                meta = get_file_metadata(p)
                if meta.get("row_count", 0) > config.BIG_DATA_ROW_THRESHOLD:
                    # Large file — metadata only, process locally
                    prompt += "\n\n" + format_file_metadata_for_prompt(p)
                else:
                    # Small data file — include content for better planning
                    content = get_file_content(p, max_chars=10000)
                    prompt += f"\n\n--- File: {p.name} ({meta.get('size_human', '?')}, ~{meta.get('row_count', '?')} data rows) ---\n{content}"
            else:
                content = get_file_content(p, max_chars=10000)
                prompt += f"\n\n--- File: {p.name} ---\n{content}"

    # Include audit feedback if this is a retry
    if state.get("audit_feedback"):
        prompt += f"\n\n--- PREVIOUS ATTEMPT FAILED ---\n{state['audit_feedback']}"
        if state.get("execution_result"):
            prompt += f"\n\nExecution output:\n{state['execution_result'][:3000]}"
        prompt += "\nRevise the plan to fix these specific issues."

    # Enable thinking only for tasks that genuinely benefit from deep reasoning
    use_thinking = task_type in ("frontend", "ui_design", "project")
    response = claude_client.call(prompt, system=system, max_tokens=3000, thinking=use_thinking)
    logger.info("Plan created for task %s (type=%s, %d chars, thinking=%s)", state["task_id"], task_type, len(response), use_thinking)
    return {"plan": response}
