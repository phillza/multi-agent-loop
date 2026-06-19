"""
AgentLoop Worker - Runs in its own terminal tab.
Shows Claude's thinking, tool calls, and results in real-time.
Press Ctrl+C to interrupt and optionally take over interactively.

Usage: python worker.py "Project Name"
"""

import subprocess
import json
import sys
import os
import time
from datetime import datetime

# Force UTF-8 stdout/stderr so monitor.py can capture output without encoding errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- Config ---
PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
WORKER_MODEL = "sonnet"
COORDINATOR_TIMEOUT = 600
LOOP_DELAY_SECONDS = 5
IMPROVE_DELAY_SECONDS = 120
BLOCKED_RETRY_SECONDS = 60

os.makedirs(LOG_DIR, exist_ok=True)

# --- Colors for terminal output ---
class C:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def cprint(color, text):
    print(f"{color}{text}{C.RESET}", flush=True)


# --- Project helpers ---
def read_project(project_name):
    with open(PROJECTS_FILE) as f:
        projects = json.load(f)
    return next((p for p in projects if p["name"] == project_name), None)


def get_project_dir(project):
    path = project.get("path", project["name"])
    return os.path.join(PROJECTS_BASE, path)


def load_project_context(project):
    project_dir = get_project_dir(project)
    context_parts = []
    for filename in ["CLAUDE.md", "AGENTS.md"]:
        filepath = os.path.join(project_dir, filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                if len(content) > 3000:
                    content = content[:3000] + "\n... (truncated)"
                context_parts.append(f"--- {filename} ---\n{content}")
            except Exception:
                pass
    if context_parts:
        return "\n\n".join(context_parts)
    return project.get("context", "No context available.")


def update_project(project_name, updates):
    with open(PROJECTS_FILE) as f:
        projects = json.load(f)
    for p in projects:
        if p["name"] == project_name:
            p.update(updates)
            if "completed_task" in updates:
                p["log"].append(updates.pop("completed_task"))
    with open(PROJECTS_FILE, "w") as f:
        json.dump(projects, f, indent=2)


def extract_json(raw_text):
    if not raw_text or not raw_text.strip():
        return None
    lines = [line.strip() for line in raw_text.strip().split("\n") if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    end = raw_text.rfind("}")
    if end == -1:
        return None
    depth = 0
    start = end
    while start >= 0:
        if raw_text[start] == "}":
            depth += 1
        elif raw_text[start] == "{":
            depth -= 1
        if depth == 0:
            break
        start -= 1
    if start < 0 or depth != 0:
        return None
    try:
        return json.loads(raw_text[start:end + 1])
    except json.JSONDecodeError:
        return None


# --- Display stream events ---
def display_event(event):
    """Pretty-print a stream-json event to the terminal."""
    etype = event.get("type", "")

    if etype == "assistant":
        msg = event.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    cprint(C.CYAN, text)
            elif block.get("type") == "tool_use":
                display_tool_use(block.get("name", "?"), block.get("input", {}))

    elif etype == "tool_use":
        name = event.get("name", event.get("tool", {}).get("name", "?"))
        inp = event.get("input", event.get("tool", {}).get("input", {}))
        display_tool_use(name, inp)

    elif etype == "tool_result":
        content = str(event.get("content", ""))
        # Show first 500 chars of result
        preview = content[:500]
        if len(content) > 500:
            preview += f"... ({len(content)} chars total)"
        cprint(C.DIM, f"  -> {preview}")

    elif etype == "result":
        cost = event.get("total_cost_usd", 0)
        duration = event.get("duration_ms", 0)
        turns = event.get("num_turns", 0)
        cprint(C.GREEN, f"\n--- Done | Cost: ${cost:.4f} | Time: {duration/1000:.1f}s | Turns: {turns} ---")


def display_tool_use(name, inp):
    """Show a tool call in a readable way."""
    if name == "Read":
        path = inp.get("file_path", "?")
        extra = ""
        if inp.get("offset"):
            extra = f" (lines {inp['offset']}-{inp['offset'] + inp.get('limit', 200)})"
        cprint(C.YELLOW, f"  [{name}] {path}{extra}")
    elif name == "Edit":
        path = inp.get("file_path", "?")
        old = (inp.get("old_string", "") or "")[:80].replace("\n", " ")
        cprint(C.YELLOW, f"  [{name}] {path}")
        cprint(C.DIM, f"    replacing: {old}...")
    elif name == "Write":
        path = inp.get("file_path", "?")
        cprint(C.YELLOW, f"  [{name}] {path}")
    elif name == "Bash":
        cmd = (inp.get("command", "?") or "?")[:150]
        cprint(C.MAGENTA, f"  [{name}] {cmd}")
    elif name == "Glob":
        cprint(C.YELLOW, f"  [{name}] {inp.get('pattern', '?')}")
    elif name == "Grep":
        cprint(C.YELLOW, f"  [{name}] '{inp.get('pattern', '?')}' in {inp.get('path', '.')}")
    else:
        cprint(C.YELLOW, f"  [{name}] {json.dumps(inp)[:120]}")


# --- Run Claude with streaming ---
def run_claude_streaming(prompt, project_dir, project_name):
    """Run claude --print with stream-json and display output live.
    Returns the final result text."""
    cmd = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--model", WORKER_MODEL,
        "--effort", "high",
        "--allowedTools", "Read", "Edit", "Write", "Bash", "Glob", "Grep",
    ]
    if project_dir and os.path.isdir(project_dir):
        cmd.extend(["--add-dir", project_dir])

    cwd = project_dir if project_dir and os.path.isdir(project_dir) else None

    # Also write to log file for dashboard
    safe_name = project_name.replace(" ", "_").replace("/", "_").lower()
    log_path = os.path.join(LOG_DIR, f"{safe_name}.log")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        bufsize=1,
    )

    proc.stdin.write(prompt)
    proc.stdin.close()

    final_result = ""
    session_id = None

    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n{'='*60}\n")
        logf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CLAUDE CALL START\n")
        logf.write(f"{'='*60}\n")
        logf.flush()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                etype = event.get("type", "")

                # Capture result and session ID
                if etype == "result":
                    final_result = event.get("result", "")
                    session_id = event.get("session_id")
                if etype == "system":
                    session_id = session_id or event.get("session_id")

                # Display to terminal
                display_event(event)

                # Write to log file for dashboard
                log_line = format_log_line(event)
                if log_line:
                    logf.write(log_line + "\n")
                    logf.flush()

            except json.JSONDecodeError:
                print(line)
                logf.write(line + "\n")
                logf.flush()

        logf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CLAUDE CALL END\n")
        logf.write(f"{'='*60}\n")

    proc.wait(timeout=COORDINATOR_TIMEOUT)
    return final_result.strip(), session_id


def format_log_line(event):
    """Format event for the log file (dashboard reads this)."""
    etype = event.get("type", "")
    if etype == "assistant":
        parts = []
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(f"[CLAUDE] {block['text'].strip()}")
            elif block.get("type") == "tool_use":
                parts.append(f"[TOOL] {block.get('name', '?')}")
        return "\n".join(parts) if parts else None
    elif etype == "tool_use":
        name = event.get("name", event.get("tool", {}).get("name", "?"))
        inp = event.get("input", event.get("tool", {}).get("input", {}))
        if name == "Bash":
            return f"[TOOL] Bash: {(inp.get('command', '?') or '?')[:120]}"
        elif name == "Read":
            return f"[TOOL] Read: {inp.get('file_path', '?')}"
        elif name == "Edit":
            return f"[TOOL] Edit: {inp.get('file_path', '?')}"
        elif name == "Write":
            return f"[TOOL] Write: {inp.get('file_path', '?')}"
        else:
            return f"[TOOL] {name}"
    elif etype == "tool_result":
        preview = str(event.get("content", ""))[:200].replace("\n", " ")
        return f"  -> {preview}"
    elif etype == "result":
        cost = event.get("total_cost_usd", 0)
        duration = event.get("duration_ms", 0)
        return f"[DONE] Cost: ${cost:.4f} | Duration: {duration/1000:.1f}s"
    return None


# --- Build prompts ---
def build_task_prompt(project):
    goals = project.get("goals", [])
    goals_text = "\n".join(f"  - {g}" for g in goals) if goals else ""
    goals_section = f"\nProject goals (keep these in mind):\n{goals_text}\n" if goals_text else ""

    return f"""You are an autonomous worker on the project "{project['name']}".
Your working directory is the project folder. You have full tool access (Read, Edit, Write, Bash, Glob, Grep).

TASK: {project['next_task']}

CONTEXT: {load_project_context(project)}
{goals_section}
INSTRUCTIONS:
1. Complete the task above using your tools. Read files, edit code, run scripts as needed.
2. When you are DONE, your FINAL line of output MUST be a JSON object on its own line like this:

{{"completed": "one sentence summary of what you did", "next_task": "specific next task to work on", "blocked": false, "blocker_description": null, "status": "in_progress", "wait_until": null}}

STATUS RULES:
- "in_progress" = there are more explicit tasks to do
- "improving" = all obvious tasks are done, project is working well, look for improvements next
- NEVER use "complete" - projects are continuously improved
- "blocked" = true ONLY if you need the user to do something (API keys, credentials, manual testing)

WAIT UNTIL:
- If the next task cannot be done until a specific time (e.g. waiting for match results, waiting for
  a scheduled job to run, waiting for data to be available), set "wait_until" to an ISO datetime string.
- Example: "wait_until": "2026-03-23T14:00:00" means don't try again until 2pm on March 23.
- The loop will skip this project until that time arrives. This saves tokens.
- Use the user's local timezone for any time-based decisions (the worker reads ``datetime.now()`` from the system clock).
- If the task can be done right now, set "wait_until": null.

CRITICAL: You MUST end your response with a valid JSON object. No markdown, no code fences around it. Just the raw JSON as the last line."""


def build_discovery_prompt(project):
    goals = project.get("goals", [])
    goals_text = "\n".join(f"  - {g}" for g in goals) if goals else "  - Make it better (no specific goals set)"
    recent_log = project.get("log", [])[-5:]
    log_text = "\n".join(f"  - {entry}" for entry in recent_log) if recent_log else "  (none)"
    project_dir = get_project_dir(project)

    return f"""
Project: {project['name']}
Project directory: {project_dir}/
Context (from project docs):
{load_project_context(project)}

Goals for this project:
{goals_text}

Recently completed work (DO NOT repeat these):
{log_text}

You are in IMPROVEMENT MODE. There are no explicit tasks left. Your job is to explore
the project and find the single highest-impact improvement you can make right now.

LOOK FOR improvements in these categories (in priority order):
1. PROFITABILITY - Can the model/strategy make more money?
2. DATA - Are there new data sources available? Gaps in historical data?
3. RELIABILITY - Are there scripts that fail silently? Missing error handling?
4. SPEED - Can scraping/processing/training run faster?
5. CODE QUALITY - Dead code? Duplicated logic? Missing tests?
6. DOCUMENTATION - Is CLAUDE.md/AGENTS.md up to date?

RULES:
- Pick ONE specific, concrete improvement. Not a vague "refactor X".
- Read the project's actual files before deciding. Don't guess.
- If the improvement requires the user's input, set blocked=true.
- Do NOT repeat work that's already in the recent log.

Respond with JSON only:
{{"completed": "one sentence: what you investigated and decided", "next_task": "the specific improvement task", "blocked": false, "blocker_description": null, "status": "improving", "improvement_category": "profitability|data|reliability|speed|code_quality|documentation"}}
"""


# --- Main worker loop ---
def worker_loop(project_name):
    project = read_project(project_name)
    if not project:
        cprint(C.RED, f"Project '{project_name}' not found in projects.json")
        return

    proj_dir = get_project_dir(project)

    cprint(C.BOLD, f"\n{'='*60}")
    cprint(C.BOLD, f"  AgentLoop Worker: {project_name}")
    cprint(C.BOLD, f"  Directory: {proj_dir}")
    cprint(C.BOLD, "  Press Ctrl+C to interrupt and take over")
    cprint(C.BOLD, f"{'='*60}\n")

    while True:
        current = read_project(project_name)
        if not current:
            cprint(C.RED, "Project removed from projects.json. Exiting.")
            break

        if current.get("status") == "paused":
            cprint(C.YELLOW, "Project is paused. Waiting 30s...")
            time.sleep(30)
            continue

        if current.get("blocked"):
            cprint(C.RED, f"BLOCKED: {current.get('blocker_description', 'No description')}")
            cprint(C.YELLOW, f"Retrying in {BLOCKED_RETRY_SECONDS}s...")
            time.sleep(BLOCKED_RETRY_SECONDS)
            continue

        # Check wait_until
        wait_until = current.get("wait_until")
        if wait_until:
            try:
                wait_time = datetime.fromisoformat(wait_until)
                if datetime.now() < wait_time:
                    wait_mins = int((wait_time - datetime.now()).total_seconds() / 60)
                    cprint(C.YELLOW, f"Waiting until {wait_until} ({wait_mins} mins left)")
                    time.sleep(300)
                    continue
                else:
                    cprint(C.GREEN, "Wait time passed, resuming!")
                    update_project(project_name, {"wait_until": None})
            except ValueError:
                update_project(project_name, {"wait_until": None})

        is_improving = current.get("status") == "improving"

        if is_improving:
            cprint(C.MAGENTA, "\n--- IMPROVEMENT MODE ---")
            prompt = build_discovery_prompt(current)
        else:
            cprint(C.GREEN, "\n--- TASK ---")
            cprint(C.BOLD, current["next_task"])
            print()
            prompt = build_task_prompt(current)

        # Run Claude with live streaming
        try:
            result_text, session_id = run_claude_streaming(prompt, proj_dir, project_name)
        except KeyboardInterrupt:
            print()
            cprint(C.YELLOW, "\nInterrupted! What do you want to do?")
            cprint(C.CYAN, "  1. Take over interactively (claude --continue)")
            cprint(C.CYAN, "  2. Skip this task and move to next")
            cprint(C.CYAN, "  3. Pause this project")
            cprint(C.CYAN, "  4. Quit this worker")
            try:
                choice = input(f"\n{C.BOLD}Enter choice (1-4): {C.RESET}").strip()
            except (KeyboardInterrupt, EOFError):
                choice = "4"

            if choice == "1":
                cprint(C.GREEN, "\nLaunching interactive Claude session...")
                cprint(C.DIM, "(Type your messages. Press Ctrl+C again to exit back to worker)")
                try:
                    resume_cmd = ["claude", "--continue"]
                    subprocess.run(resume_cmd, cwd=proj_dir)
                except KeyboardInterrupt:
                    cprint(C.YELLOW, "\nBack to worker loop.")
                continue
            elif choice == "2":
                cprint(C.YELLOW, "Skipping task...")
                continue
            elif choice == "3":
                update_project(project_name, {"status": "paused"})
                cprint(C.YELLOW, "Project paused.")
                break
            else:
                cprint(C.RED, "Worker exiting.")
                break

        # Parse result
        outcome = extract_json(result_text)
        if not outcome:
            cprint(C.RED, "Failed to parse JSON from Claude's response. Retrying...")
            time.sleep(LOOP_DELAY_SECONDS)
            continue

        # Update project state
        if is_improving:
            category = outcome.get("improvement_category", "unknown")
            cprint(C.MAGENTA, f"\nFound improvement [{category}]: {outcome.get('next_task', '')[:100]}")
            update_project(project_name, {
                "next_task": outcome.get("next_task", ""),
                "blocked": outcome.get("blocked", False),
                "blocker_description": outcome.get("blocker_description"),
                "status": "in_progress",
                "completed_task": f"[discovery] {outcome.get('completed', '')}"
            })
        else:
            cprint(C.GREEN, f"\nCompleted: {outcome.get('completed', '')}")
            updates = {
                "next_task": outcome.get("next_task", current["next_task"]),
                "blocked": outcome.get("blocked", False),
                "blocker_description": outcome.get("blocker_description"),
                "status": outcome.get("status", "in_progress"),
                "completed_task": outcome.get("completed", "")
            }
            if outcome.get("wait_until"):
                updates["wait_until"] = outcome["wait_until"]
                cprint(C.YELLOW, f"Waiting until: {outcome['wait_until']}")
            update_project(project_name, updates)

        # Delay before next cycle
        delay = IMPROVE_DELAY_SECONDS if outcome.get("status") == "improving" else LOOP_DELAY_SECONDS
        cprint(C.DIM, f"\nNext cycle in {delay}s...")
        time.sleep(delay)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python worker.py \"Project Name\"")
        print("\nAvailable projects:")
        with open(PROJECTS_FILE) as f:
            for p in json.load(f):
                print(f"  [{p['status']:12s}] {p['name']}")
        sys.exit(1)

    project_name = sys.argv[1]
    try:
        worker_loop(project_name)
    except KeyboardInterrupt:
        cprint(C.RED, "\nWorker stopped.")
