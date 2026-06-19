"""
launch_all.py - Launch all active projects in autonomous or interactive mode.

Default mode opens one runtime supervisor tab, which manages headless worker
and strategist passes across the selected projects. Use --interactive to get
the older walkie-talkie tabs and optional global orchestrator.

Usage:
  # Launch all active projects in autonomous mode
  python launch_all.py

  # Launch specific projects only
  python launch_all.py "my-project" "my-project" "my-project"

  # Force the older interactive walkie-talkie flow
  python launch_all.py --interactive

  # Interactive mode: workers/teams only, no global orchestrator
  python launch_all.py --interactive --no-global-orch

  # Interactive mode: launch global orchestrator only
  python launch_all.py --interactive --global-orch-only
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
from datetime import datetime

from runtime_overrides import merge_project_override

AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_FILE = os.path.join(AGENTLOOP_DIR, "projects.json")
TEAMS_FILE = os.path.join(AGENTLOOP_DIR, "teams.json")
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
# How long to wait after all workers launch before starting global orchestrator.
# Gives workers time to join walkie-talkie and their channels.
GLOBAL_ORCH_DELAY = 10

TOOL_CONFIGS = {
    "claude": {
        "base": ["claude", "--dangerously-skip-permissions"],
        "model_flag": "--model",
        "effort_flag": "--effort",
        "prompt_file_flag": "--append-system-prompt-file",
        "add_dir_flag": "--add-dir",
        "default_model": "claude-sonnet-4-6",
    },
    "codex": {
        "base": ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        "model_flag": "--model",
        "effort_flag": None,
        "prompt_file_flag": None,
        "add_dir_flag": None,
        "default_model": "gpt-5.4",
    },
    "opencode": {
        "base": ["opencode"],
        "model_flag": "--model",
        "effort_flag": None,
        "prompt_file_flag": None,
        "prompt_flag": "--prompt",
        "add_dir_flag": None,
        "default_model": None,
    },
}

CLAUDE_MODELS = {
    "opus":   "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}

OPENCODE_MINIMAX_RE = re.compile(r"^minimax-(\d+(?:\.\d+)?(?:-highspeed)?)$", re.IGNORECASE)

TEAM_DEFAULTS = {
    "orch_tool":    "claude",
    "orch_model":   "opus",
    "orch_effort":  "medium",
    "worker_tool":  "opencode",
    "worker_model": None,
    "workers":      3,
    "channel":      None,
    "strategist":   False,
    "strategist_tool": "codex",
    "strategist_model": "gpt-5.4",
    "strategist_effort": "high",
    "strategist_interval_minutes": 30,
}

SINGLE_WORKER_DEFAULTS = {
    "tool":   "claude",
    "model":  "sonnet",
    "effort": "high",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_projects():
    with open(PROJECTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_teams():
    if not os.path.exists(TEAMS_FILE):
        return {}
    with open(TEAMS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    # Strip meta keys
    return {k: v for k, v in data.items() if not k.startswith("_")}


def get_active_projects(projects):
    now = datetime.now().isoformat()
    return [
        p for p in projects
        if p["status"] in ("in_progress", "improving")
        and not p.get("blocked")
        and (not p.get("wait_until") or p["wait_until"] < now)
    ]


def filter_by_names(projects, names):
    result = []
    for name in names:
        match = next((p for p in projects if p["name"].lower() == name.lower()), None)
        if not match:
            match = next((p for p in projects if name.lower() in p["name"].lower()), None)
        if match:
            result.append(match)
        else:
            print(f"  Warning: no project matching '{name}' found, skipping.")
    return result


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


def resolve_model(tool, model):
    if not model:
        return model
    if tool == "claude":
        return CLAUDE_MODELS.get(model, model)
    if tool == "opencode":
        match = OPENCODE_MINIMAX_RE.match(model)
        if match:
            return f"minimax/MiniMax-M{match.group(1)}"
    return model


def open_tab(title, cwd, cmd):
    # npm tools (codex, opencode) are .cmd scripts on Windows — must run via cmd /k
    if cmd[0] in ("codex", "opencode"):
        cmd = ["cmd", "/k"] + cmd
    if shutil.which("wt"):
        subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", title, "-d", cwd, "--", *cmd])
    else:
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        subprocess.Popen(f'start "{title}" {cmd_str}', shell=True, cwd=cwd)


def open_tab_safe(title, cwd, cmd):
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
        subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", title, "-d", cwd, "--", *wrapped_cmd])
    else:
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in wrapped_cmd)
        subprocess.Popen(f'start "{title}" {cmd_str}', shell=True, cwd=cwd)


def build_cmd(tool, model, effort, message_or_file, is_file=False):
    cfg = TOOL_CONFIGS[tool]
    cmd = cfg["base"].copy()
    model = resolve_model(tool, model) if model else None
    if model and cfg["model_flag"]:
        cmd += [cfg["model_flag"], model]
    if effort and cfg["effort_flag"]:
        cmd += [cfg["effort_flag"], effort]
    if tool == "claude":
        cmd += ["--add-dir", AGENTLOOP_DIR]
    if is_file and cfg["prompt_file_flag"]:
        cmd += [cfg["prompt_file_flag"], message_or_file]
    elif cfg.get("prompt_flag"):
        cmd += [cfg["prompt_flag"], message_or_file]
    else:
        cmd += [message_or_file]
    return cmd


# ---------------------------------------------------------------------------
# Prompt builders (inline so launch_all.py is self-contained)
# ---------------------------------------------------------------------------

def build_single_worker_prompt(project):
    handle = make_handle(project["name"])
    name = project["name"]
    project_dir = get_project_dir(project)
    next_task = project.get("next_task", "Check CLAUDE.md and AGENTS.md for what to work on")
    return f"""You are an autonomous coding agent for project: {name}
Radio handle: {handle}
Working directory: {project_dir}
Projects file: {PROJECTS_FILE}

STARTUP — do this FIRST before anything else:
1. radio_join as "{handle}"
2. radio_over @orchestrator on channel "#all" "READY {handle}: ready for task"
3. radio_standby to wait for your task on #all
4. If no task arrives within 30 seconds, send READY again on #all and radio_standby again
5. Keep repeating until you receive a TASK

WORKFLOW (repeat forever):
- When you receive "TASK: [description]":
  1. radio_over @orchestrator on channel "#all" "RECEIVED {handle}: starting now"
  2. Read CLAUDE.md and AGENTS.md for project context
  3. Complete the task using your tools
  4. Update {PROJECTS_FILE}: set completed_task, update next_task, append to log
  5. radio_over @orchestrator on channel "#all" "DONE {handle}: [one sentence summary]"
  6. radio_standby for your next task on #all

- If blocked: radio_over @orchestrator on channel "#all" "BLOCKED {handle}: [reason]" then radio_standby

NEVER stop. Always working or waiting at radio_standby.
Current task (for reference): {next_task}
"""


def build_team_orchestrator_prompt(project, channel, worker_handles, orch_handle):
    name = project["name"]
    project_dir = get_project_dir(project)
    task_board = find_task_board(project_dir)
    task_board_line = f"Task board: {task_board}" if task_board else "No task board — read CLAUDE.md/AGENTS.md."
    workers_str = ", ".join(f"@{h}" for h in worker_handles)
    return f"""You are the project orchestrator for: {name}
Handle: {orch_handle} | Channel: #{channel}
Project directory: {project_dir}
{task_board_line}
Workers: {workers_str}

STARTUP:
1. radio_join as "{orch_handle}"
2. radio_channel_create "{channel}"
3. radio_channel_invite each worker to "{channel}"
4. Read CLAUDE.md, AGENTS.md, task board
5. radio_over @orchestrator on channel "#all" "READY {orch_handle}: ready for task"
6. radio_standby — listen on both #all and #{channel}
7. If radio_join fails because "{orch_handle}" is already registered, stop and wait for a restart. Do not keep working under a fallback name.

GLOBAL LOOP:
- If the global orchestrator on #all sends "TASK: [description]":
  1. Reply on #all: radio_over @orchestrator "RECEIVED {orch_handle}: coordinating now"
  2. Break the project task into smaller worker tasks on #{channel}
  3. When the overall project task is done, reply on #all: radio_over @orchestrator "DONE {orch_handle}: [one sentence summary]"
- If the global orchestrator sends a status check on #all, answer it on #all
- If no TASK arrives from @orchestrator within 30 seconds while idle, resend READY on #all and keep listening

TEAM HANDSHAKE (do not send worker tasks until you receive READY on #{channel}):
- Workers will send on #{channel}: "READY [worker]: ready for task"
- When you receive READY from a worker: assign them one task via radio_over @[worker] on channel "#{channel}" "TASK [ID]: [description]"
- Worker will confirm: "RECEIVED [worker]: [id] starting now"
- Do not wait for multiple workers before assigning. Every READY requires an immediate task reply.

MAIN LOOP:
- radio_standby for messages from both #all and #{channel}
- "DONE [worker]: [id] - [summary]" on #{channel} -> mark done, assign next task, update {PROJECTS_FILE}
- "BLOCKED [worker]: [reason]" on #{channel} -> reassign or skip, continue others
- "DECLINING REPEATED TASK" on #{channel} -> do not repeat the same assignment; immediately send a different valid task or tell the worker to stay ready
- If @orchestrator (global) asks status -> report worker activity and tasks remaining
- Stuck worker (15+ min silent) -> re-send their task on #{channel}
- After every READY, RECEIVED, DONE, or BLOCKED message, decide the next action immediately and then return to radio_standby on #{channel}
- Long waits in radio_standby are normal
- Do not stop to summarize while workers are active

NEVER stop. One task per worker at a time. Prefer tasks on different files.
"""


def build_team_worker_prompt(project, channel, worker_handle, orch_handle, worker_num):
    name = project["name"]
    project_dir = get_project_dir(project)
    return f"""You are worker {worker_num} for: {name}
Handle: {worker_handle} | Channel: #{channel} | Orchestrator: @{orch_handle}
Project directory: {project_dir}

STARTUP:
1. radio_join as "{worker_handle}"
2. radio_channel_join "{channel}"
3. radio_over @{orch_handle} on channel "#{channel}" "READY {worker_handle}: ready for task"
4. radio_standby for your first task on #{channel}
5. If no task arrives within 30 seconds, resend READY on #{channel} and radio_standby again
6. Keep repeating until you receive a TASK
7. If radio_join fails because "{worker_handle}" is already registered, stop and wait for a restart. Do not keep working under a fallback name.

WORKFLOW (repeat forever):
- Receive "TASK [ID]: [desc]":
  1. radio_over @{orch_handle} on channel "#{channel}" "RECEIVED {worker_handle}: [id] starting now"
  2. Complete the task, test it
  3. radio_over @{orch_handle} on channel "#{channel}" "DONE {worker_handle}: [id] - [summary]"
  4. Immediately resend READY on "#{channel}"
  5. radio_standby on #{channel}
- Can't complete: radio_over @{orch_handle} on channel "#{channel}" "BLOCKED {worker_handle}: [id] - [reason]" -> resend READY -> radio_standby
- If a task is already done or clearly invalid: radio_over @{orch_handle} on channel "#{channel}" "DECLINING REPEATED TASK - [reason]. {worker_handle} is standing by for a new task." -> resend READY -> radio_standby
- If radio_standby or walkie-talkie times out: do not ask the user what to do next; resend READY on "#{channel}" and go back to radio_standby

RULES: Never start without a task. Never edit files another worker owns. Always radio_standby after reporting. Never ask the user to choose between options while you are in the worker role unless you hit a real blocker that prevents all progress.
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


def build_foreman_team_orchestrator_prompt(project, channel, worker_handles, orch_handle):
    name = project["name"]
    project_dir = get_project_dir(project)
    task_board = find_task_board(project_dir)
    task_board_line = f"Task board: {task_board}" if task_board else "No task board - read CLAUDE.md/AGENTS.md."
    workers_str = ", ".join(f"@{h}" for h in worker_handles)
    return f"""You are the project orchestrator for: {name}
Handle: {orch_handle} | Channel: #{channel}
Project directory: {project_dir}
{task_board_line}
Workers: {workers_str}

ROLE:
- You are a foreman, not a planner
- Keep workers active
- If a worker is idle, tell them to claim the next valid task from the board
- If a worker is blocked, make them report the exact hard blocker
- If a worker is working, leave them alone

STARTUP:
1. radio_join as "{orch_handle}"
2. radio_channel_create "{channel}"
3. radio_channel_invite each worker to "{channel}"
4. Read CLAUDE.md, AGENTS.md, task board
5. radio_over @orchestrator on channel "#all" "READY {orch_handle}: ready for task"
6. radio_standby - listen on both #all and #{channel}
7. If radio_join fails because "{orch_handle}" is already registered, stop and wait for a restart. Do not keep working under a fallback name.

GLOBAL LOOP:
- If the global orchestrator on #all sends "TASK: [description]":
  1. Reply on #all: radio_over @orchestrator "RECEIVED {orch_handle}: coordinating now"
  2. Keep your workers active against the task board and the project goal
  3. When the overall project task is done, reply on #all: radio_over @orchestrator "DONE {orch_handle}: [one sentence summary]"
- If the global orchestrator sends a status check on #all, answer it on #all
- If no TASK arrives from @orchestrator within 30 seconds while idle, resend READY on #all and keep listening

TEAM HANDSHAKE:
- Workers will send on #{channel}: "READY [worker]: ready for task"
- When you receive READY from a worker: tell them to claim the next valid task from the board and start it
- Workers should then report:
  - "WORKING [worker]: [task_id] - [short description]"
  - "DONE [worker]: [task_id] - [summary]"
  - "BLOCKED [worker]: [exact blocker]"
- Do not wait for multiple workers before replying. Every READY requires an immediate reply.

MAIN LOOP:
- radio_standby for messages from both #all and #{channel}
- "WORKING [worker]" on #{channel} -> acknowledge briefly if needed, then leave them alone
- "DONE [worker]" on #{channel} -> tell them to claim the next valid task
- "BLOCKED [worker]" on #{channel} -> force a precise blocker if vague, otherwise leave it recorded
- "DECLINING REPEATED TASK" on #{channel} -> tell the worker to claim the next valid task instead
- If @orchestrator (global) asks status -> report who is WORKING, DONE and ready, or BLOCKED
- Stuck worker (15+ min silent) -> send: "STATUS CHECK [worker]: reply WORKING, DONE, or BLOCKED right now."
- After every READY, WORKING, DONE, or BLOCKED message, decide the next action immediately and then return to radio_standby on #{channel}
- Long waits in radio_standby are normal
- Do not stop to summarize while workers are active

NEVER stop. Keep checking worker state and keeping them moving.
"""


def build_foreman_team_worker_prompt(project, channel, worker_handle, orch_handle, worker_num):
    name = project["name"]
    project_dir = get_project_dir(project)
    return f"""You are worker {worker_num} for: {name}
Handle: {worker_handle} | Channel: #{channel} | Orchestrator: @{orch_handle}
Project directory: {project_dir}

STARTUP:
1. radio_join as "{worker_handle}"
2. radio_channel_join "{channel}"
3. radio_over @{orch_handle} on channel "#{channel}" "READY {worker_handle}: ready for task"
4. radio_standby for your first instruction on #{channel}
5. If no task arrives within 30 seconds, resend READY on #{channel} and radio_standby again
6. Keep repeating until you receive a work instruction
7. If radio_join fails because "{worker_handle}" is already registered, stop and wait for a restart. Do not keep working under a fallback name.

WORKFLOW (repeat forever):
- When told to work:
  1. Re-read the task board and claim the next valid task you can safely own
  2. If you claimed one, send: "WORKING {worker_handle}: [task_id] - [short description]"
  3. Complete the task and test it
  4. Send: "DONE {worker_handle}: [task_id] - [summary]"
  5. Immediately resend READY on "#{channel}"
  6. radio_standby on #{channel}
- If no valid task is available: send "BLOCKED {worker_handle}: no valid unclaimed task on board" -> radio_standby
- If a hard blocker appears: send "BLOCKED {worker_handle}: [task_id] - [exact blocker]" -> radio_standby
- If a repeated task is already done or invalid: send "DECLINING REPEATED TASK - [reason]. {worker_handle} is standing by for a new task." -> resend READY -> radio_standby
- If radio_standby or walkie-talkie times out: do not ask the user what to do next; resend READY on "#{channel}" and go back to radio_standby

RULES: Use the task board as the source of truth. Never edit files another worker owns. Always radio_standby after reporting. Never ask the user to choose between options while you are in the worker role unless you hit a real blocker that prevents all progress.
"""


def build_global_orchestrator_prompt(active_projects):
    worker_list = "\n".join(
        f"  - Handle: {make_handle(p['name'])} | Project: {p['name']} | Dir: {get_project_dir(p)} | Task: {p.get('next_task', 'check CLAUDE.md')[:80]}"
        for p in active_projects
    )
    handles = [make_handle(p["name"]) for p in active_projects]
    handles_str = ", ".join(handles)
    return f"""You are the AgentLoop Global Orchestrator — the single coordinator for all active projects.
You are the ONLY orchestrator. There is one instance of you running at a time.

ACTIVE PROJECTS:
{worker_list}

Expected worker handles: {handles_str}
PROJECTS FILE: {PROJECTS_FILE}
PROJECTS BASE: {PROJECTS_BASE}

YOUR JOB:
- Keep every worker productive at all times
- Read each project's CLAUDE.md and AGENTS.md to understand what needs doing
- When next_task is empty or the project status is "improving", brainstorm and define what to work on next
- Track what each worker has done and what still needs doing

STARTUP:
1. radio_join as "orchestrator"
2. radio_standby on #all — wait for workers to announce themselves

HANDSHAKE (never send a task until you receive READY):
- Workers announce: "READY [handle]: ready for task"
- When you receive READY from a worker:
  1. Read their project entry in {PROJECTS_FILE}
  2. radio_over @[handle] on channel "#all" "TASK: [next_task from projects.json]"
- Worker confirms: "RECEIVED [handle]: starting now"
- Then radio_standby again for the next message

WHEN A WORKER REPORTS "DONE [handle]: [summary]":
1. Read {PROJECTS_FILE} — check next_task for that project
2. If next_task is specific: radio_over @handle on channel "#all" "TASK: [next_task]"
3. If next_task is empty or vague or status is "improving":
   - radio_over @handle on channel "#all" "TASK: Read your CLAUDE.md and AGENTS.md, then brainstorm and implement the most impactful improvement you can find. Update next_task in {PROJECTS_FILE} when done."
4. radio_standby for next response

WHEN A WORKER REPORTS "BLOCKED [handle]: [reason]":
- Note the blocker in your output so the user can see it
- Skip that project for now, keep other workers going

QUIET WORKER (15+ min with no response):
- radio_over @handle on channel "#all" "TASK: [their current task from {PROJECTS_FILE}]" to resend

Note: some handles are project orchestrators managing their own teams internally.
Just send tasks to the project handle — you don't manage their internal workers.

NEVER stop. You are always: polling for workers, sending tasks, or listening at radio_standby.
"""


# ---------------------------------------------------------------------------
# Launchers
# ---------------------------------------------------------------------------

def launch_single_worker(project, tool, model, effort):
    handle = make_handle(project["name"])
    project_dir = get_project_dir(project)
    prompt = build_single_worker_prompt(project)
    initial_msg = f"DO THIS NOW: 1. Call radio_join as '{handle}'. 2. Call radio_over @orchestrator on channel '#all' with 'READY {handle}: ready for task'. 3. Call radio_standby. If no task arrives within 30 seconds, resend READY on '#all' and radio_standby again. Repeat until you receive a TASK."

    cfg = TOOL_CONFIGS[tool]
    if cfg["prompt_file_flag"]:
        pf = os.path.join(tempfile.gettempdir(), f"agentloop_{handle}.txt")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = build_cmd(tool, model, effort, pf, is_file=True)
        cmd.append(initial_msg)
    else:
        # For codex/opencode: action first so it executes immediately, context after
        cmd = build_cmd(tool, model, effort, initial_msg + "\n\n" + prompt)

    open_tab_safe(f"[{tool}] {project['name']}", project_dir, cmd)


def launch_team_orchestrator(project, channel, worker_handles, orch_handle, tool, model, effort):
    project_dir = get_project_dir(project)
    prompt = build_foreman_team_orchestrator_prompt(project, channel, worker_handles, orch_handle)
    workers_str = ", ".join(worker_handles)
    initial_msg = (
        f"Join walkie-talkie as '{orch_handle}', create channel '{channel}', "
        f"invite workers ({workers_str}), read task board, send READY to @orchestrator on '#all', "
        f"then wait for worker READY messages on '#{channel}' and TASK messages from @orchestrator on '#all'. "
        f"If radio_join says '{orch_handle}' is already registered, stop instead of using any fallback handle."
    )
    cfg = TOOL_CONFIGS[tool]
    if cfg["prompt_file_flag"]:
        pf = os.path.join(tempfile.gettempdir(), f"agentloop_orch_{orch_handle}.txt")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = build_cmd(tool, model, effort, pf, is_file=True)
        cmd.append(initial_msg)
    else:
        cmd = build_cmd(tool, model, effort, prompt + "\n\n" + initial_msg)
    open_tab_safe(f"[ORCH-{tool}] {project['name']}", project_dir, cmd)


def launch_team_worker(project, channel, worker_handle, orch_handle, worker_num, tool, model):
    project_dir = get_project_dir(project)
    prompt = build_foreman_team_worker_prompt(project, channel, worker_handle, orch_handle, worker_num)
    initial_msg = (
        f"Join walkie-talkie as '{worker_handle}', join channel '{channel}', "
        f"send READY to @{orch_handle} on '#{channel}', then radio_standby on '#{channel}'. "
        f"If no task arrives within 30 seconds, resend READY on '#{channel}'. Don't start until you get a task. "
        f"If radio_join says '{worker_handle}' is already registered, stop instead of using any fallback handle."
    )
    cfg = TOOL_CONFIGS[tool]
    if cfg["prompt_file_flag"]:
        pf = os.path.join(tempfile.gettempdir(), f"agentloop_{worker_handle}.txt")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = build_cmd(tool, model, None, pf, is_file=True)
        cmd.append(initial_msg)
    else:
        cmd = build_cmd(tool, model, None, prompt + "\n\n" + initial_msg)
    open_tab_safe(f"[{tool}] {project['name']} W{worker_num}", project_dir, cmd)


def launch_global_orchestrator(active_projects, model="sonnet", effort="high"):
    prompt = build_global_orchestrator_prompt(active_projects)
    handles = [make_handle(p["name"]) for p in active_projects]
    initial_msg = (
        f"Join walkie-talkie as 'orchestrator', then radio_standby on '#all'. "
        f"Do not send any TASK messages until a worker sends READY. "
        f"Projects: {', '.join(handles)}"
    )
    pf = os.path.join(tempfile.gettempdir(), "agentloop_global_orchestrator.txt")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(prompt)
    cmd = build_cmd("claude", model, effort, pf, is_file=True)
    cmd.append(initial_msg)
    open_tab_safe("[GLOBAL ORCH]", AGENTLOOP_DIR, cmd)


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
            pf = os.path.join(tempfile.gettempdir(), f"agentloop_strategist_{make_handle(project['name'])}.txt")
            with open(pf, "w", encoding="utf-8") as f:
                f.write(prompt)
            cmd = build_cmd(tool, model, effort, pf, is_file=True)
            cmd.append(initial_msg)
        else:
            cmd = build_cmd(tool, model, effort, initial_msg + "\n\n" + prompt)
    open_tab_safe(f"[STRATEGIST-{tool}] {project['name']}", project_dir, cmd)


def launch_runtime_supervisor(project_names):
    cmd = [sys.executable, os.path.join(AGENTLOOP_DIR, "runtime_supervisor.py"), *project_names]
    open_tab_safe("[AUTO SUPERVISOR]", AGENTLOOP_DIR, cmd)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Launch all projects in autonomous or interactive mode.")
    parser.add_argument("projects", nargs="*", help="Project names to launch (default: all active)")
    parser.add_argument("--interactive", dest="mode", action="store_const", const="interactive", help="Use the older walkie-talkie launch flow")
    parser.add_argument("--autonomous", dest="mode", action="store_const", const="autonomous", help="Use the new headless runtime supervisor (default)")
    parser.add_argument("--with-orch", dest="with_orch", action="store_true", help="Launch the global orchestrator (default)")
    parser.add_argument("--no-global-orch", dest="with_orch", action="store_false", help="Launch workers only, no global orchestrator")
    parser.add_argument("--orch-only", "--global-orch-only", dest="orch_only", action="store_true", help="Only launch the global orchestrator, no workers")
    parser.add_argument("--orch-model", default="sonnet", help="Global orchestrator model (default: sonnet)")
    parser.add_argument("--tool", default=None, choices=list(TOOL_CONFIGS), help="Override tool for single workers (default: claude)")
    parser.add_argument("--model", default=None, help="Override model for single workers")
    parser.set_defaults(with_orch=True, orch_only=False, mode="autonomous")
    args = parser.parse_args()

    all_projects = load_projects()
    teams = load_teams()
    active = get_active_projects(all_projects)

    if args.projects:
        active = filter_by_names(all_projects, args.projects)

    if not active:
        print("No active projects found.")
        return

    print(f"\n=== AgentLoop Launch All ===")
    print(f"  Mode: {args.mode}")
    if args.mode == "interactive":
        print(f"  Walkie-talkie hub must be running first!\n")
    else:
        print()

    if args.mode == "autonomous":
        for project in active:
            if args.tool or args.model:
                merge_project_override(
                    project["name"],
                    {
                        "tool": args.tool,
                        "model": args.model,
                    },
                )
        if args.orch_only or args.with_orch is False:
            print("Note: global orchestrator flags are ignored in autonomous mode.")
        launch_runtime_supervisor([project["name"] for project in active])
        print("Done! 1 autonomous supervisor tab opened.")
        print("Use the dashboard to watch runtime workers via logs/runtime_status.json.")
        return

    total_tabs = 0

    if not args.orch_only:
        for project in active:
            name = project["name"]
            team_cfg = teams.get(name, {})

            if team_cfg:
                # --- Team project ---
                cfg = {**TEAM_DEFAULTS, **team_cfg}
                orch_handle = make_handle(name)
                channel = cfg["channel"] or orch_handle
                n_workers = cfg["workers"] if isinstance(cfg["workers"], int) else len(cfg["workers"])
                worker_tools = (
                    cfg["workers"] if isinstance(cfg["workers"], list)
                    else [cfg["worker_tool"]] * n_workers
                )
                base = orch_handle[:15]
                worker_handles = [f"{base}-w{i+1}" for i in range(n_workers)]

                model_display = resolve_model(cfg["orch_tool"], cfg["orch_model"])
                print(f"  [TEAM] {name}")
                print(f"    Orch:    [{cfg['orch_tool']}] {model_display} ({cfg['orch_effort']}) @ {orch_handle}")
                for i, (h, t) in enumerate(zip(worker_handles, worker_tools)):
                    m = cfg.get("worker_model") or ""
                    print(f"    Worker {i+1}: [{t}]{' ' + m if m else ''} @ {h}")
                if cfg.get("strategist"):
                    strategist_model = resolve_model(cfg["strategist_tool"], cfg["strategist_model"])
                    print(f"    Strategist: [{cfg['strategist_tool']}] {strategist_model} every {cfg['strategist_interval_minutes']}m")
                print(f"    Channel: #{channel}")

                launch_team_orchestrator(
                    project, channel, worker_handles, orch_handle,
                    cfg["orch_tool"], cfg["orch_model"], cfg["orch_effort"]
                )
                time.sleep(6)  # Let orchestrator create the channel

                for i, (handle, tool) in enumerate(zip(worker_handles, worker_tools)):
                    launch_team_worker(
                        project, channel, handle, orch_handle, i + 1,
                        tool, cfg.get("worker_model")
                    )
                    time.sleep(2)

                if cfg.get("strategist"):
                    launch_strategist(
                        project,
                        cfg["strategist_tool"],
                        cfg["strategist_model"],
                        cfg["strategist_effort"],
                        cfg["strategist_interval_minutes"],
                    )
                    time.sleep(2)

                total_tabs += 1 + n_workers + (1 if cfg.get("strategist") else 0)

            else:
                # --- Single worker ---
                cfg = {**SINGLE_WORKER_DEFAULTS, **project.get("worker_config", {})}
                if args.tool:
                    cfg["tool"] = args.tool
                if args.model:
                    cfg["model"] = args.model
                print(f"  [SINGLE] {name} - [{cfg['tool']}] {cfg['model']}")
                launch_single_worker(project, cfg["tool"], cfg["model"], cfg["effort"])
                time.sleep(3)
                total_tabs += 1

        print()

    if args.with_orch or args.orch_only:
        model_display = resolve_model("claude", args.orch_model)
        if not args.orch_only:
            print(f"  Waiting {GLOBAL_ORCH_DELAY}s for all workers to join walkie-talkie...")
            time.sleep(GLOBAL_ORCH_DELAY)
        print(f"  [GLOBAL ORCH] claude {model_display} @ orchestrator")
        launch_global_orchestrator(active, args.orch_model, "high")
        total_tabs += 1

    print(f"\nDone! {total_tabs} tabs opened.")
    print("Monitor all channels at http://localhost:9559")


if __name__ == "__main__":
    main()
