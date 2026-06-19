"""
launch_team.py - Launch a project team in autonomous or interactive mode.

Default mode starts one runtime supervisor tab, which then manages headless
worker and strategist passes for that project. Use --interactive to get the
older walkie-talkie tabs instead.

Usage:
  # Default autonomous mode
  python launch_team.py "my-project"

  # Force the older interactive walkie-talkie flow
  python launch_team.py "my-project" --interactive

  # Autonomous team with custom worker and orchestrator defaults
  python launch_team.py "my-project" --orch-tool codex --orch-model gpt-5.4 --workers 3 --worker-tool opencode --worker-model minimax/MiniMax-M2.7

  # Mixed worker tools
  python launch_team.py "my-project" --workers opencode opencode codex

  # Per-worker models when mixing tools
  python launch_team.py "my-project" --workers opencode opencode codex --worker-models minimax/MiniMax-M2.7 minimax/MiniMax-M2.7 gpt-5.4

  # Add a strategist to the autonomous runtime
  python launch_team.py "my-project" --with-strategist --strategist-tool codex --strategist-model gpt-5.4
  python launch_team.py "my-project" --with-strategist --strategist-interval-minutes 30
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from runtime_overrides import merge_project_override

PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")
AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
# Claude model shorthand -> full model ID
CLAUDE_MODELS = {
    "opus":   "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}

OPENCODE_MINIMAX_RE = re.compile(r"^minimax-(\d+(?:\.\d+)?(?:-highspeed)?)$", re.IGNORECASE)

# Per-tool config: how to build the launch command
# model_flag  — flag to pass the model name (None = not supported)
# effort_flag — flag to pass thinking effort (None = not supported)
# prompt_file_flag — flag to pass a system prompt file (None = not supported, use combined message)
# add_dir_flag — flag to add an extra directory to context
TOOL_CONFIGS = {
    "claude": {
        "base": ["claude", "--dangerously-skip-permissions"],
        "model_flag":       "--model",
        "effort_flag":      "--effort",
        "prompt_file_flag": "--append-system-prompt-file",
        "add_dir_flag":     "--add-dir",
        "default_model":    "claude-opus-4-6",
    },
    "codex": {
        "base": ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        "model_flag":       "--model",
        "effort_flag":      None,
        "prompt_file_flag": None,
        "add_dir_flag":     None,
        "default_model":    None,  # use codex default
    },
    "opencode": {
        "base": ["opencode"],
        "model_flag":       "--model",
        "effort_flag":      None,
        "prompt_file_flag": None,
        "prompt_flag":      "--prompt",
        "add_dir_flag":     None,
        "default_model":    None,
    },
}


# ---------------------------------------------------------------------------
# Project helpers
# ---------------------------------------------------------------------------

def load_projects():
    with open(PROJECTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def find_project(name):
    projects = load_projects()
    exact = next((p for p in projects if p["name"].lower() == name.lower()), None)
    if exact:
        return exact
    partial = [p for p in projects if name.lower() in p["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        print(f"Ambiguous name '{name}'. Matches:")
        for p in partial:
            print(f"  - {p['name']}")
        sys.exit(1)
    return None


def get_project_dir(project):
    path = project.get("path", project["name"])
    return os.path.join(PROJECTS_BASE, path).replace("\\", "/")


def make_handle(name):
    h = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return h.strip("-")[:20]


def find_task_board(project_dir):
    candidates = [
        os.path.join(project_dir, "TASK_BOARD.md"),
        os.path.join(project_dir, "tasks", "TASK_BOARD.md"),
        os.path.join(project_dir, "tasks", "AGENT_STARTUP.md"),
    ]
    found = [p for p in candidates if os.path.exists(p)]
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def build_cmd(tool, model, effort, prompt, prompt_is_file=False):
    """
    Build the launch command for a given tool.

    tool           — claude | codex | opencode
    model          — model string (or None for tool default)
    effort         — low | medium | high (only used by claude)
    prompt         — the prompt/message string, or path if prompt_is_file=True
    prompt_is_file — if True, prompt is a file path (only valid for tools with prompt_file_flag)
    """
    cfg = TOOL_CONFIGS[tool]
    cmd = cfg["base"].copy()

    # Resolve tool-specific model shorthands so the config can stay simple.
    if tool == "claude" and model in CLAUDE_MODELS:
        model = CLAUDE_MODELS[model]
    elif tool == "opencode":
        match = OPENCODE_MINIMAX_RE.match(model or "")
        if match:
            model = f"minimax/MiniMax-M{match.group(1)}"

    if model and cfg["model_flag"]:
        cmd += [cfg["model_flag"], model]

    if effort and cfg["effort_flag"]:
        cmd += [cfg["effort_flag"], effort]

    if tool == "claude":
        cmd += ["--add-dir", AGENTLOOP_DIR]

    if prompt_is_file and cfg["prompt_file_flag"]:
        cmd += [cfg["prompt_file_flag"], prompt]
    elif cfg.get("prompt_flag"):
        cmd += [cfg["prompt_flag"], prompt]
    else:
        cmd += [prompt]

    return cmd


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_orchestrator_prompt(project, channel, worker_handles, orch_handle):
    name = project["name"]
    project_dir = get_project_dir(project)
    task_board = find_task_board(project_dir)
    task_board_line = (
        f"Task board: {task_board}"
        if task_board
        else "No task board found — read CLAUDE.md and AGENTS.md to understand what needs doing."
    )
    workers_str = ", ".join(f"@{h}" for h in worker_handles)

    return f"""You are the project orchestrator for: {name}
Your walkie-talkie handle: {orch_handle}
Project channel: #{channel}
Project directory: {project_dir}
Projects file: {PROJECTS_FILE}
{task_board_line}

YOUR WORKERS: {workers_str}

STARTUP — do in order:
1. radio_join as "{orch_handle}"
2. radio_channel_create "{channel}"
3. radio_channel_invite each worker to "{channel}"
4. Read CLAUDE.md, AGENTS.md, and the task board in {project_dir}
5. radio_standby on #{channel} — wait for workers to announce themselves
6. If radio_join fails because "{orch_handle}" is already registered, stop and wait for a restart. Do not keep working under a fallback name.

HANDSHAKE (never assign a task until you receive READY):
- Workers announce on #{channel}: "READY [worker]: ready for task"
- When you receive READY: assign that worker one task via radio_over @[worker] on channel "#{channel}" "TASK [ID]: [description]"
- Worker confirms: "RECEIVED [worker]: [id] starting now"
- Then radio_standby again
- Do not wait for multiple workers before assigning. Every READY requires an immediate task reply.

MAIN LOOP:
- Use channel #{channel} for all worker communication
- Use #all only when the global @orchestrator contacts you
- After every READY, RECEIVED, DONE, or BLOCKED message, decide the next action immediately and then return to radio_standby on #{channel}
- Long waits in radio_standby are normal
- Do not stop to summarize while workers are active

When you receive "DONE [worker]: [task_id] - [summary]":
  1. Mark the task complete in the task board
  2. Update {PROJECTS_FILE}: append to the project log, update next_task
  3. Assign the worker their next unclaimed task
  4. If the board is empty: assign "TASK: Read CLAUDE.md and AGENTS.md, brainstorm the most impactful improvement, implement it."

When you receive "BLOCKED [worker]: [reason]":
  1. Try to reassign or skip the task, note the blocker
  2. Continue with other workers

When a worker says "DECLINING REPEATED TASK" or tells you a task is already done:
  1. Do not repeat the same assignment
  2. Check the board and recent completions
  3. Immediately assign a different valid task, or explicitly tell the worker to stay ready if nothing useful is available

When @orchestrator (global) asks for status:
  Report which workers are active, what they're working on, and tasks remaining.

STUCK WORKER RECOVERY:
  If a worker has been silent 15+ minutes, re-send their task.

TASK RULES:
  - One task per worker at a time — track who has what
  - Prefer tasks that touch different files so workers don't conflict
  - Never assign the same task to two workers

NEVER stop. Always assigning, listening at radio_standby, or updating records.
"""


def build_worker_prompt(project, channel, worker_handle, orch_handle, worker_num):
    name = project["name"]
    project_dir = get_project_dir(project)

    return f"""You are worker {worker_num} for project: {name}
Your walkie-talkie handle: {worker_handle}
Your orchestrator: @{orch_handle}
Project channel: #{channel}
Project directory: {project_dir}

STARTUP — do in order:
1. radio_join as "{worker_handle}"
2. radio_channel_join "{channel}"
3. radio_over @{orch_handle} on channel "#{channel}" "READY {worker_handle}: ready for task"
4. radio_standby on "{channel}" — wait for your first task
5. If no task arrives within 30 seconds, resend READY and radio_standby again
6. Keep repeating until you receive a TASK
7. If radio_join fails because "{worker_handle}" is already registered, stop and wait for a restart. Do not keep working under a fallback name.

WORKFLOW (repeat forever):
When you receive "TASK [ID]: [description]":
  1. radio_over @{orch_handle} on channel "#{channel}" "RECEIVED {worker_handle}: [id] starting now"
  2. Complete the task using your tools in {project_dir}
  3. Run relevant tests to verify your work
  4. radio_over @{orch_handle} on channel "#{channel}" "DONE {worker_handle}: [task_id] - [one sentence summary]"
  5. Immediately resend READY on "#{channel}" so the orchestrator knows you are free
  6. radio_standby on "{channel}" for your next task

If you cannot complete a task:
  radio_over @{orch_handle} on channel "#{channel}" "BLOCKED {worker_handle}: [task_id] - [reason]"
  Then immediately resend READY and radio_standby to wait.

If the orchestrator repeats a task that is already completed, no longer valid, or clearly assigned in error:
  1. radio_over @{orch_handle} on channel "#{channel}" "DECLINING REPEATED TASK - [reason]. {worker_handle} is standing by for a new task."
  2. Immediately resend READY and radio_standby again

TIMEOUT RECOVERY:
- If radio_standby times out, MCP times out, or walkie-talkie temporarily fails, do not ask the user what to do next
- Treat it as a temporary coordination problem
- Rejoin the channel if needed, resend READY on "#{channel}", and go straight back to radio_standby
- Your default behavior after any timeout is to keep trying to reconnect to @{orch_handle}, not to stop

If the user types to you directly: respond, then return to radio_standby.

RULES:
- NEVER start work without a task from @{orch_handle}
- NEVER edit files another worker is currently working on
- ALWAYS radio_standby after reporting — never stop between tasks
- NEVER ask the user to choose between options while you are in the worker role unless you hit a real blocker that prevents all progress
- Use channel "{channel}" for all communication, not #all
"""


def build_strategist_prompt(project):
    name = project["name"]
    project_dir = get_project_dir(project)
    task_board = find_task_board(project_dir)
    task_board_line = f"Task board: {task_board}" if task_board else "No task board found."
    goals = project.get("goals") or []
    goals_text = "\n".join(f"- {goal}" for goal in goals) if goals else "- Use the business outcome in projects.json as the planning guide."

    return f"""You are the strategist for project: {name}
Project directory: {project_dir}
Projects file: {PROJECTS_FILE}
{task_board_line}

Your role is not to duplicate the workers or the orchestrator.
Your job is to:
1. Read CLAUDE.md, AGENTS.md, the task board, and recent completion notes
2. Work out what is already done, what is blocked, and what matters most next
3. Keep the project aligned to the real end goal by creating or refining future tasks
4. Avoid interfering with active worker execution unless truly necessary

Project goals:
{goals_text}

STRATEGIST RULES:
- Audit completed work before inventing new work
- Prefer updating the task board, planning docs, or next-task definitions over doing random implementation
- Focus on bottlenecks, sequencing, missing systems, scaling constraints, and business impact
- If you do implement code, keep it tightly tied to unblocking the roadmap
- Do not take over the orchestrator role
- Do not spam summaries; leave the project with clearer priorities and better next tasks

Keep working autonomously until you hit a real blocker.
"""


def build_foreman_orchestrator_prompt(project, channel, worker_handles, orch_handle):
    name = project["name"]
    project_dir = get_project_dir(project)
    task_board = find_task_board(project_dir)
    task_board_line = (
        f"Task board: {task_board}"
        if task_board
        else "No task board found - read CLAUDE.md and AGENTS.md to understand what needs doing."
    )
    workers_str = ", ".join(f"@{h}" for h in worker_handles)

    return f"""You are the project orchestrator for: {name}
Your walkie-talkie handle: {orch_handle}
Project channel: #{channel}
Project directory: {project_dir}
Projects file: {PROJECTS_FILE}
{task_board_line}

YOUR WORKERS: {workers_str}

ROLE:
- You are a foreman, not a planner
- Your job is to keep workers active and force clear blocker reporting
- Workers should claim the next valid task from the task board themselves unless you name a specific task
- If a worker is working, leave them alone
- If a worker is idle, tell them to claim and start something
- If a worker is blocked, force them to report the exact hard blocker

STARTUP - do in order:
1. radio_join as "{orch_handle}"
2. radio_channel_create "{channel}"
3. radio_channel_invite each worker to "{channel}"
4. Read CLAUDE.md, AGENTS.md, and the task board in {project_dir}
5. radio_standby on #{channel} - wait for workers to announce themselves
6. If radio_join fails because "{orch_handle}" is already registered, stop and wait for a restart. Do not keep working under a fallback name.

HANDSHAKE:
- Workers announce on #{channel}: "READY [worker]: ready for task"
- When you receive READY: immediately reply telling that worker to claim the next valid task from the board and start it
- Worker should then report either:
  - "WORKING [worker]: [task_id] - [short description]"
  - "BLOCKED [worker]: [exact hard blocker]"
- Then return to radio_standby
- Do not wait for multiple workers before replying. Every READY requires an immediate response.

MAIN LOOP:
- Use channel #{channel} for all worker communication
- Use #all only when the global @orchestrator contacts you
- After every READY, WORKING, DONE, or BLOCKED message, decide the next action immediately and then return to radio_standby on #{channel}
- Long waits in radio_standby are normal
- Do not stop to summarize while workers are active

When you receive "WORKING [worker]: [task_id] - [summary]":
  1. Acknowledge briefly if needed
  2. Leave that worker alone unless they later report DONE or BLOCKED

When you receive "DONE [worker]: [task_id] - [summary]":
  1. Tell that worker to claim the next valid task from the board immediately
  2. If the board is empty or unclear, tell them to wait or report BLOCKED with the exact reason

When you receive "BLOCKED [worker]: [reason]":
  1. Make sure the blocker is concrete and specific
  2. If the blocker report is vague, tell the worker to restate the exact blocker
  3. If the blocker is real, tell the worker to wait and keep that blocker visible

When a worker says "DECLINING REPEATED TASK" or tells you a task is already done:
  1. Do not repeat the same assignment
  2. Tell the worker to claim the next valid task instead

When @orchestrator (global) asks for status:
  Report which workers are WORKING, which are DONE and ready, and which are BLOCKED.

STATUS CHECKS:
  If a worker has been silent too long, ask:
  "STATUS CHECK [worker]: reply WORKING, DONE, or BLOCKED right now."

TASK RULES:
  - Workers claim from the task board; you do not micromanage implementation
  - One task per worker at a time
  - Keep ownership clean and avoid conflicts
  - If there is no valid work, push the blocker upward instead of inventing random tasks

NEVER stop. Always monitoring, checking status, or parked in radio_standby.
"""


def build_foreman_worker_prompt(project, channel, worker_handle, orch_handle, worker_num):
    name = project["name"]
    project_dir = get_project_dir(project)

    return f"""You are worker {worker_num} for project: {name}
Your walkie-talkie handle: {worker_handle}
Your orchestrator: @{orch_handle}
Project channel: #{channel}
Project directory: {project_dir}

STARTUP - do in order:
1. radio_join as "{worker_handle}"
2. radio_channel_join "{channel}"
3. radio_over @{orch_handle} on channel "#{channel}" "READY {worker_handle}: ready for task"
4. radio_standby on "{channel}" - wait for your first instruction
5. If no reply arrives within 30 seconds, resend READY and radio_standby again
6. Keep repeating until you receive a work instruction
7. If radio_join fails because "{worker_handle}" is already registered, stop and wait for a restart. Do not keep working under a fallback name.

WORKFLOW (repeat forever):
When the orchestrator tells you to work or claim something:
  1. Re-read the task board and claim the next valid task you can own safely
  2. If you successfully claim one, send:
     "WORKING {worker_handle}: [task_id] - [short description]"
  3. Complete the task using your tools in {project_dir}
  4. Run relevant tests to verify your work
  5. Send:
     "DONE {worker_handle}: [task_id] - [one sentence summary]"
  6. Immediately resend READY on "#{channel}" so the orchestrator knows you are free
  7. radio_standby on "{channel}" for your next task

If you cannot find a valid task to claim:
  radio_over @{orch_handle} on channel "#{channel}" "BLOCKED {worker_handle}: no valid unclaimed task on board"
  Then radio_standby and wait.

If you hit a hard blocker on a claimed task:
  radio_over @{orch_handle} on channel "#{channel}" "BLOCKED {worker_handle}: [task_id] - [exact blocker]"
  Then radio_standby to wait.

If the orchestrator repeats a task that is already completed, no longer valid, or clearly assigned in error:
  1. radio_over @{orch_handle} on channel "#{channel}" "DECLINING REPEATED TASK - [reason]. {worker_handle} is standing by for a new task."
  2. Immediately resend READY and radio_standby again

TIMEOUT RECOVERY:
- If radio_standby times out, MCP times out, or walkie-talkie temporarily fails, do not ask the user what to do next
- Treat it as a temporary coordination problem
- Rejoin the channel if needed, resend READY on "#{channel}", and go straight back to radio_standby
- Your default behavior after any timeout is to keep trying to reconnect to @{orch_handle}, not to stop

If the user types to you directly: respond, then return to radio_standby.

RULES:
- Use the task board as the source of truth for what to claim next
- When the orchestrator tells you to work, that means claim the next valid task from the board unless it names a specific task
- NEVER edit files another worker is currently working on
- ALWAYS radio_standby after reporting - never stop between tasks
- NEVER ask the user to choose between options while you are in the worker role unless you hit a real blocker that prevents all progress
- Use channel "{channel}" for all communication, not #all
"""


# ---------------------------------------------------------------------------
# Tab launcher
# ---------------------------------------------------------------------------

def launch_tab(title, cwd, cmd):
    # npm tools (codex, opencode) are .cmd scripts on Windows — must run via cmd /k
    if cmd[0] in ("codex", "opencode"):
        cmd = ["cmd", "/k"] + cmd
    if shutil.which("wt"):
        subprocess.Popen([
            "wt", "-w", "0", "new-tab",
            "--title", title,
            "-d", cwd,
            "--", *cmd,
        ])
    else:
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        subprocess.Popen(f'start "{title}" {cmd_str}', shell=True, cwd=cwd)


def launch_tab_safe(title, cwd, cmd):
    runner_file = os.path.join(tempfile.gettempdir(), f"agentloop_cmd_{int(time.time() * 1000)}.json")
    with open(runner_file, "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "cwd": cwd}, f)
    wrapped_cmd = [
        "powershell",
        "-NoExit",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        os.path.join(AGENTLOOP_DIR, "run_saved_command.ps1"),
        runner_file,
    ]
    if shutil.which("wt"):
        subprocess.Popen([
            "wt", "-w", "0", "new-tab",
            "--title", title,
            "-d", cwd,
            "--", *wrapped_cmd,
        ])
    else:
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in wrapped_cmd)
        subprocess.Popen(f'start "{title}" {cmd_str}', shell=True, cwd=cwd)


def launch_orchestrator(project, channel, worker_handles, orch_handle, orch_tool, orch_model, orch_effort):
    project_dir = get_project_dir(project)
    prompt = build_foreman_orchestrator_prompt(project, channel, worker_handles, orch_handle)
    workers_str = ", ".join(worker_handles)
    initial_msg = (
        f"Start now: join walkie-talkie as '{orch_handle}', create channel '{channel}', "
        f"invite workers ({workers_str}), read the task board, then wait for READY messages on '#{channel}'. "
        f"Assign all worker tasks on '#{channel}', not '#all'. "
        f"If radio_join says '{orch_handle}' is already registered, stop instead of using any fallback handle."
    )

    cfg = TOOL_CONFIGS[orch_tool]

    if cfg["prompt_file_flag"]:
        # Claude: write system prompt to file, pass initial message separately
        prompt_file = os.path.join(tempfile.gettempdir(), f"agentloop_orch_{orch_handle}.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = build_cmd(orch_tool, orch_model, orch_effort, prompt_file, prompt_is_file=True)
        cmd.append(initial_msg)
    else:
        # codex/opencode: action first so it executes immediately, context after
        combined = initial_msg + "\n\n" + prompt
        cmd = build_cmd(orch_tool, orch_model, orch_effort, combined)

    launch_tab_safe(f"[ORCH-{orch_tool}] {project['name']}", project_dir, cmd)


def launch_worker(project, channel, worker_handle, orch_handle, worker_num, worker_tool, worker_model):
    project_dir = get_project_dir(project)
    prompt = build_foreman_worker_prompt(project, channel, worker_handle, orch_handle, worker_num)
    initial_msg = (
        f"DO THIS NOW: 1. Call radio_join as '{worker_handle}'. "
        f"2. Call radio_channel_join '{channel}'. "
        f"3. Call radio_over @{orch_handle} on channel '#{channel}' with 'READY {worker_handle}: ready for task'. "
        f"4. Call radio_standby on '{channel}'. "
        f"If no task arrives within 30 seconds, resend READY on '#{channel}' and radio_standby again. "
        f"Repeat until you receive a TASK. "
        f"If radio_join says '{worker_handle}' is already registered, stop instead of using any fallback handle."
    )

    cfg = TOOL_CONFIGS[worker_tool]

    if cfg["prompt_file_flag"]:
        prompt_file = os.path.join(tempfile.gettempdir(), f"agentloop_{worker_handle}.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = build_cmd(worker_tool, worker_model, None, prompt_file, prompt_is_file=True)
        cmd.append(initial_msg)
    else:
        # codex/opencode: action first so it executes immediately, context after
        combined = initial_msg + "\n\n" + prompt
        cmd = build_cmd(worker_tool, worker_model, None, combined)

    launch_tab_safe(f"[{worker_tool}] {project['name']} W{worker_num}", project_dir, cmd)


def launch_strategist(project, tool, model, effort, interval_minutes=0):
    project_dir = get_project_dir(project)
    if interval_minutes and interval_minutes > 0:
        cmd = [
            "python",
            os.path.join(AGENTLOOP_DIR, "run_strategy_loop.py"),
            project["name"],
            "--tool",
            tool,
            "--interval-minutes",
            str(interval_minutes),
        ]
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])
    else:
        prompt = build_strategist_prompt(project)
        initial_msg = (
            "Start by reading the project docs and task board, then audit completed work, identify the highest-leverage "
            "next steps, and update the project with clearer priorities. Do not duplicate the workers or act as the "
            "walkie-talkie orchestrator."
        )

        cfg = TOOL_CONFIGS[tool]
        if cfg["prompt_file_flag"]:
            prompt_file = os.path.join(tempfile.gettempdir(), f"agentloop_strategist_{make_handle(project['name'])}.txt")
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(prompt)
            cmd = build_cmd(tool, model, effort, prompt_file, prompt_is_file=True)
            cmd.append(initial_msg)
        else:
            cmd = build_cmd(tool, model, effort, initial_msg + "\n\n" + prompt)

    launch_tab_safe(f"[STRATEGIST-{tool}] {project['name']}", project_dir, cmd)


def launch_runtime_supervisor(project_name):
    cmd = [sys.executable, os.path.join(AGENTLOOP_DIR, "runtime_supervisor.py"), project_name]
    launch_tab_safe(f"[AUTO] {project_name}", AGENTLOOP_DIR, cmd)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_workers_arg(workers_arg, default_tool):
    """
    --workers 3                       -> 3 workers, all using default_tool
    --workers opencode opencode codex -> specific tools per worker
    """
    if len(workers_arg) == 1 and workers_arg[0].isdigit():
        n = int(workers_arg[0])
        return [default_tool] * n
    for t in workers_arg:
        if t not in TOOL_CONFIGS:
            print(f"Unknown tool '{t}'. Choose from: {', '.join(TOOL_CONFIGS)}")
            sys.exit(1)
    return list(workers_arg)


def main():
    parser = argparse.ArgumentParser(
        description="Launch a project team in autonomous or interactive mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python launch_team.py "my-project"
  python launch_team.py "my-project" --interactive
  python launch_team.py "my-project" --orch-tool codex --orch-model gpt-5.4 --workers 3 --worker-tool opencode --worker-model minimax/MiniMax-M2.7
  python launch_team.py "my-project" --workers opencode opencode codex --worker-models minimax/MiniMax-M2.7 minimax/MiniMax-M2.7 gpt-5.4
  python launch_team.py "my-project" --with-strategist --strategist-tool codex --strategist-model gpt-5.4
  python launch_team.py "my-project" --with-strategist --strategist-interval-minutes 30
  python launch_team.py "my-project" --orch-model sonnet --workers 2 --worker-tool claude --worker-model haiku
        """
    )
    parser.add_argument("project", help="Project name (partial match ok)")
    parser.add_argument("--interactive", dest="mode", action="store_const", const="interactive", help="Use the older walkie-talkie tabs")
    parser.add_argument("--autonomous", dest="mode", action="store_const", const="autonomous", help="Use the new headless runtime supervisor (default)")

    # Orchestrator
    orch = parser.add_argument_group("Orchestrator")
    orch.add_argument("--orch-tool",   default="claude", choices=list(TOOL_CONFIGS), help="Orchestrator tool (default: claude)")
    orch.add_argument("--orch-model",  default="opus",   help="Orchestrator model — shorthand (opus/sonnet/haiku) or full ID (default: opus)")
    orch.add_argument("--orch-effort", default="medium", choices=["low", "medium", "high"], help="Thinking effort, claude only (default: medium)")

    # Workers
    workers = parser.add_argument_group("Workers")
    workers.add_argument("--workers",      nargs="+", default=["3"],  help="Number of workers (3) or list of tools (opencode opencode codex)")
    workers.add_argument("--worker-tool",  default="opencode", choices=list(TOOL_CONFIGS), help="Default tool for all workers (default: opencode)")
    workers.add_argument("--worker-model", default=None, help="Model for all workers (optional)")
    workers.add_argument("--worker-models", nargs="+", default=None, help="Per-worker models (overrides --worker-model)")

    # Channel
    parser.add_argument("--channel", default=None, help="Walkie-talkie channel name (default: auto from project name)")

    # Strategist
    strategist = parser.add_argument_group("Strategist")
    strategist.add_argument("--with-strategist", action="store_true", default=None, help="Launch a separate strategist session for roadmap/task generation")
    strategist.add_argument("--strategist-tool", default="codex", choices=list(TOOL_CONFIGS), help="Strategist tool (default: codex)")
    strategist.add_argument("--strategist-model", default="gpt-5.4", help="Strategist model (default: gpt-5.4)")
    strategist.add_argument("--strategist-effort", default="high", choices=["low", "medium", "high"], help="Thinking effort for strategist when the tool supports it")
    strategist.add_argument("--strategist-interval-minutes", type=int, default=30, help="If >0, rerun the strategist in the same terminal every N minutes (default: 30)")
    parser.set_defaults(mode="autonomous")

    args = parser.parse_args()

    project = find_project(args.project)
    if not project:
        print(f"No project matching '{args.project}' found.")
        sys.exit(1)

    worker_tools = parse_workers_arg(args.workers, args.worker_tool)
    n = len(worker_tools)

    # Resolve per-worker models
    if args.worker_models:
        if len(args.worker_models) != n:
            print(f"--worker-models has {len(args.worker_models)} values but {n} workers specified.")
            sys.exit(1)
        worker_models = args.worker_models
    else:
        worker_models = [args.worker_model] * n  # None = tool default

    orch_handle = make_handle(project["name"])
    channel = args.channel or orch_handle
    base = orch_handle[:15]
    worker_handles = [f"{base}-w{i+1}" for i in range(n)]

    # Resolve claude model shorthand for display
    orch_model_display = CLAUDE_MODELS.get(args.orch_model, args.orch_model)

    # Print plan
    print(f"\n=== Team Launch: {project['name']} ===")
    print(f"  Mode:         {args.mode}")
    print(f"  Channel:      #{channel}")
    effort_note = f" ({args.orch_effort} thinking)" if args.orch_tool == "claude" else ""
    print(f"  Orchestrator: [{args.orch_tool}] {orch_model_display}{effort_note} @ {orch_handle}")
    for i, (h, tool, model) in enumerate(zip(worker_handles, worker_tools, worker_models)):
        model_note = f" ({model})" if model else ""
        print(f"  Worker {i+1}:     [{tool}]{model_note} @ {h}")
    if args.with_strategist:
        strategist_model_note = f" {args.strategist_model}" if args.strategist_model else ""
        print(f"  Strategist:   [{args.strategist_tool}]{strategist_model_note} every {args.strategist_interval_minutes}m")
    if args.mode == "interactive":
        print("\n  Walkie-talkie hub must be running first!")
    print("=" * 42 + "\n")

    if args.mode == "autonomous":
        merge_project_override(
            project["name"],
            {
                "workers": worker_tools,
                "worker_tool": args.worker_tool,
                "worker_model": args.worker_model,
                "worker_models": worker_models if args.worker_models else None,
                "orch_tool": args.orch_tool,
                "orch_model": args.orch_model,
                "orch_effort": args.orch_effort,
                "channel": channel,
                "strategist": args.with_strategist,
                "strategist_tool": args.strategist_tool,
                "strategist_model": args.strategist_model,
                "strategist_effort": args.strategist_effort,
                "strategist_interval_minutes": args.strategist_interval_minutes,
            },
        )
        print("Launching runtime supervisor...")
        launch_runtime_supervisor(project["name"])
        print("\nDone! 1 tab open.")
        print("Use the dashboard to watch runtime workers via logs/runtime_status.json.")
        return

    # Launch orchestrator first, wait for it to create the channel
    print(f"Launching orchestrator [{args.orch_tool}]...")
    launch_orchestrator(
        project, channel, worker_handles, orch_handle,
        args.orch_tool, args.orch_model, args.orch_effort
    )
    print(f"  Waiting 6s for orchestrator to join and create #{channel}...")
    time.sleep(6)

    # Launch workers
    for i, (handle, tool, model) in enumerate(zip(worker_handles, worker_tools, worker_models)):
        print(f"Launching worker {i+1} [{tool}]: {handle}" + (f" ({model})" if model else ""))
        launch_worker(project, channel, handle, orch_handle, i + 1, tool, model)
        if i < n - 1:
            time.sleep(2)

    if args.with_strategist:
        print(f"Launching strategist [{args.strategist_tool}]...")
        launch_strategist(
            project,
            args.strategist_tool,
            args.strategist_model,
            args.strategist_effort,
            args.strategist_interval_minutes,
        )

    total_tabs = 1 + n + (1 if args.with_strategist else 0)
    print(f"\nDone! {total_tabs} tabs open.")
    print(f"Monitor the #{channel} channel in the walkie-talkie dashboard.")


if __name__ == "__main__":
    main()
