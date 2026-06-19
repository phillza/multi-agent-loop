"""
Interactive Worker - Claude worker with live steering + auto-nudge.

Runs in a terminal tab (launched by agent_loop_interactive.py).
- Streams Claude output in real-time (like worker.py)
- Auto-loops through tasks from projects.json — never sits idle
- Type anything at any time to steer (becomes next prompt)
- Blocked/paused/wait_until states also accept typed overrides

Usage:
  python interactive_worker.py "Project Name"
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime

# Force UTF-8 so output doesn't break on Windows console
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


# --- Colors ---
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


# --- Stdin reader thread ---
# Reads lines from stdin in the background. User types -> goes in queue.
_user_input_queue: queue.Queue = queue.Queue()


def _stdin_reader():
    while True:
        try:
            line = sys.stdin.readline()
            if line:
                stripped = line.strip()
                if stripped:
                    _user_input_queue.put(stripped)
        except Exception:
            break


def start_stdin_reader():
    t = threading.Thread(target=_stdin_reader, daemon=True)
    t.start()


def get_user_input(timeout=0):
    """Non-blocking (timeout=0) or blocking with timeout. Returns message or None."""
    try:
        return _user_input_queue.get(timeout=timeout) if timeout > 0 else _user_input_queue.get_nowait()
    except queue.Empty:
        return None


# --- Project helpers ---
def read_project(project_name):
    with open(PROJECTS_FILE, encoding="utf-8") as f:
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
    with open(PROJECTS_FILE, encoding="utf-8") as f:
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
    lines = [l.strip() for l in raw_text.strip().split("\n") if l.strip()]
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
    if name == "Read":
        path = inp.get("file_path", "?")
        extra = f" (lines {inp['offset']}-{inp['offset'] + inp.get('limit', 200)})" if inp.get("offset") else ""
        cprint(C.YELLOW, f"  [{name}] {path}{extra}")
    elif name == "Edit":
        path = inp.get("file_path", "?")
        old = (inp.get("old_string", "") or "")[:80].replace("\n", " ")
        cprint(C.YELLOW, f"  [{name}] {path}")
        cprint(C.DIM, f"    replacing: {old}...")
    elif name == "Write":
        cprint(C.YELLOW, f"  [{name}] {inp.get('file_path', '?')}")
    elif name == "Bash":
        cprint(C.MAGENTA, f"  [{name}] {(inp.get('command', '?') or '?')[:150]}")
    elif name == "Glob":
        cprint(C.YELLOW, f"  [{name}] {inp.get('pattern', '?')}")
    elif name == "Grep":
        cprint(C.YELLOW, f"  [{name}] '{inp.get('pattern', '?')}' in {inp.get('path', '.')}")
    else:
        cprint(C.YELLOW, f"  [{name}] {json.dumps(inp)[:120]}")


def format_log_line(event):
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
        elif name in ("Read", "Edit", "Write"):
            return f"[TOOL] {name}: {inp.get('file_path', '?')}"
        return f"[TOOL] {name}"
    elif etype == "tool_result":
        preview = str(event.get("content", ""))[:200].replace("\n", " ")
        return f"  -> {preview}"
    elif etype == "result":
        cost = event.get("total_cost_usd", 0)
        duration = event.get("duration_ms", 0)
        return f"[DONE] Cost: ${cost:.4f} | Duration: {duration/1000:.1f}s"
    return None


# --- Run Claude with streaming ---
# Uses --max-turns to pause periodically so steering is picked up quickly.
CHUNK_TURNS = 10

def run_claude_chunk(prompt, project_dir, project_name, session_id=None):
    """Run Claude for up to CHUNK_TURNS turns.
    Checks user input queue mid-stream and returns it if found.
    Returns (result_text, session_id, pending_steer_message).
    """
    cmd = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--model", WORKER_MODEL,
        "--dangerously-skip-permissions",
        "--max-turns", str(CHUNK_TURNS),
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    if project_dir and os.path.isdir(project_dir):
        cmd.extend(["--add-dir", project_dir])

    cwd = project_dir if project_dir and os.path.isdir(project_dir) else None
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
    session_id_out = session_id
    pending_steer = None

    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n{'='*60}\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CLAUDE CALL START\n{'='*60}\n")
        logf.flush()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                etype = event.get("type", "")
                if etype == "result":
                    final_result = event.get("result", "")
                    session_id_out = event.get("session_id") or session_id_out
                if etype == "system":
                    session_id_out = session_id_out or event.get("session_id")
                display_event(event)
                log_line = format_log_line(event)
                if log_line:
                    logf.write(log_line + "\n")
                    logf.flush()
            except json.JSONDecodeError:
                print(line)
                logf.write(line + "\n")
                logf.flush()

            # Check for steering after every event — picked up at next chunk boundary
            if pending_steer is None:
                msg = get_user_input(timeout=0)
                if msg:
                    pending_steer = msg
                    cprint(C.GREEN, f"\n[YOU -> queued] {msg}")
                    cprint(C.DIM, "  (will steer after this action completes)")

        logf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CLAUDE CALL END\n{'='*60}\n")

    proc.wait(timeout=COORDINATOR_TIMEOUT)
    return final_result.strip(), session_id_out, pending_steer


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
1. Complete the task above using your tools.
2. When DONE, your FINAL line MUST be a JSON object:

{{"completed": "one sentence summary", "next_task": "specific next task", "blocked": false, "blocker_description": null, "status": "in_progress", "wait_until": null}}

STATUS RULES:
- "in_progress" = more tasks to do
- "improving" = project is working, look for improvements
- NEVER use "complete"
- "blocked" = true ONLY if you need the user (API keys, credentials, manual testing)

WAIT UNTIL: Set to ISO datetime if next task needs to wait, otherwise null.

CRITICAL: End with raw JSON on its own line. No markdown fences."""


def build_discovery_prompt(project):
    goals = project.get("goals", [])
    goals_text = "\n".join(f"  - {g}" for g in goals) if goals else "  - Make it better"
    recent_log = project.get("log", [])[-5:]
    log_text = "\n".join(f"  - {l}" for l in recent_log) if recent_log else "  (none)"
    project_dir = get_project_dir(project)

    return f"""Project: {project['name']}
Directory: {project_dir}/
Context: {load_project_context(project)}

Goals:
{goals_text}

Recently completed (do NOT repeat):
{log_text}

IMPROVEMENT MODE: Find the single highest-impact improvement. Check in order:
1. PROFITABILITY  2. DATA  3. RELIABILITY  4. SPEED  5. CODE QUALITY  6. DOCUMENTATION

Read actual files before deciding. Pick ONE specific, concrete improvement.

Respond with JSON only:
{{"completed": "what you investigated", "next_task": "specific improvement", "blocked": false, "blocker_description": null, "status": "improving", "improvement_category": "profitability|data|reliability|speed|code_quality|documentation"}}"""


def build_steer_prompt(project, user_message):
    """Incorporates a user steering message as the task."""
    return f"""You are an autonomous worker on the project "{project['name']}".
Your working directory is the project folder. You have full tool access.

CONTEXT: {load_project_context(project)}

The user has sent you this steering message:
"{user_message}"

Act on the user's message. When done, output the JSON status update:

{{"completed": "one sentence summary", "next_task": "specific next task", "blocked": false, "blocker_description": null, "status": "in_progress", "wait_until": null}}

CRITICAL: End with raw JSON on its own line."""


# --- Delay with user-input interruptible wait ---
def wait_with_input_check(seconds, message):
    """Wait up to `seconds`, but return any user input immediately if typed."""
    cprint(C.DIM, f"{message} (type to steer)")
    user_msg = get_user_input(timeout=seconds)
    return user_msg  # None if timeout elapsed, string if user typed


# --- Main loop ---
def interactive_worker_loop(project_name):
    project = read_project(project_name)
    if not project:
        cprint(C.RED, f"Project '{project_name}' not found in projects.json")
        return

    proj_dir = get_project_dir(project)

    cprint(C.BOLD, f"\n{'='*60}")
    cprint(C.BOLD, f"  Interactive Worker: {project_name}")
    cprint(C.BOLD, f"  Directory: {proj_dir}")
    cprint(C.BOLD, f"  Type anything to steer | Ctrl+C to stop")
    cprint(C.BOLD, f"{'='*60}\n")

    start_stdin_reader()

    while True:
        current = read_project(project_name)
        if not current:
            cprint(C.RED, "Project removed from projects.json. Exiting.")
            break

        # --- Paused ---
        if current.get("status") == "paused":
            user_msg = wait_with_input_check(30, "Project is paused. Waiting...")
            if user_msg:
                cprint(C.GREEN, f"\n[YOU] {user_msg}")
                update_project(project_name, {"status": "in_progress"})
                prompt = build_steer_prompt(current, user_msg)
            else:
                continue

        # --- Blocked ---
        elif current.get("blocked"):
            blocker = current.get("blocker_description", "No description")
            cprint(C.RED, f"BLOCKED: {blocker}")
            user_msg = wait_with_input_check(BLOCKED_RETRY_SECONDS, f"Retry in {BLOCKED_RETRY_SECONDS}s...")
            if user_msg:
                cprint(C.GREEN, f"\n[YOU] {user_msg}")
                update_project(project_name, {"blocked": False, "blocker_description": None})
                prompt = build_steer_prompt(current, user_msg)
            else:
                continue

        # --- Wait until ---
        elif current.get("wait_until"):
            try:
                wait_time = datetime.fromisoformat(current["wait_until"]).replace(tzinfo=None)
                if datetime.now() < wait_time:
                    wait_mins = int((wait_time - datetime.now()).total_seconds() / 60)
                    user_msg = wait_with_input_check(300, f"Waiting until {current['wait_until']} ({wait_mins} mins left).")
                    if user_msg:
                        cprint(C.GREEN, f"\n[YOU] {user_msg}")
                        update_project(project_name, {"wait_until": None})
                        prompt = build_steer_prompt(current, user_msg)
                    else:
                        continue
                else:
                    cprint(C.GREEN, "Wait time passed, resuming!")
                    update_project(project_name, {"wait_until": None})
                    continue
            except ValueError:
                update_project(project_name, {"wait_until": None})
                continue

        # --- Normal: build initial prompt ---
        else:
            user_msg = get_user_input(timeout=0)  # non-blocking check before starting
            if user_msg:
                cprint(C.GREEN, f"\n[YOU] {user_msg}")
                initial_prompt = build_steer_prompt(current, user_msg)
            elif current.get("status") == "improving":
                cprint(C.MAGENTA, "\n--- IMPROVEMENT MODE ---")
                initial_prompt = build_discovery_prompt(current)
            else:
                cprint(C.GREEN, f"\n--- TASK ---")
                cprint(C.BOLD, current.get("next_task", "No task set"))
                print()
                initial_prompt = build_task_prompt(current)

        # --- Chunk loop: run Claude in chunks, checking for steering between each ---
        try:
            result_text = ""
            session_id = None
            prompt = initial_prompt

            while True:
                result_text, session_id, pending_steer = run_claude_chunk(
                    prompt, proj_dir, project_name, session_id=session_id
                )

                # If task produced a valid JSON outcome, we're done
                if extract_json(result_text):
                    break

                # Hit turn limit — steer or continue
                if pending_steer:
                    cprint(C.GREEN, f"\n[YOU] {pending_steer}")
                    prompt = build_steer_prompt(current, pending_steer)
                else:
                    cprint(C.DIM, "Turn limit reached, continuing...")
                    prompt = "Continue working on the task. Output the JSON status update when done."

        except KeyboardInterrupt:
            print()
            cprint(C.RED, "\nWorker stopped.")
            break

        # --- Parse result ---
        outcome = extract_json(result_text)
        if not outcome:
            cprint(C.RED, "Could not parse JSON from response. Retrying in 5s...")
            time.sleep(5)
            continue

        # --- Update project state ---
        is_discovery = "improvement_category" in outcome
        if is_discovery:
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
                "next_task": outcome.get("next_task", current.get("next_task", "")),
                "blocked": outcome.get("blocked", False),
                "blocker_description": outcome.get("blocker_description"),
                "status": outcome.get("status", "in_progress"),
                "completed_task": outcome.get("completed", ""),
            }
            if outcome.get("wait_until"):
                updates["wait_until"] = outcome["wait_until"]
                cprint(C.YELLOW, f"Waiting until: {outcome['wait_until']}")
            update_project(project_name, updates)

        # --- Pause before next cycle, interruptible ---
        delay = IMPROVE_DELAY_SECONDS if outcome.get("status") == "improving" else LOOP_DELAY_SECONDS
        user_msg = wait_with_input_check(delay, f"\nAuto-continuing in {delay}s...")
        if user_msg:
            _user_input_queue.put(user_msg)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python interactive_worker.py \"Project Name\"")
        sys.exit(1)

    project_name = " ".join(sys.argv[1:])
    try:
        interactive_worker_loop(project_name)
    except KeyboardInterrupt:
        pass
