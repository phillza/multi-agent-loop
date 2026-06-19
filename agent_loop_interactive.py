"""
AgentLoop Interactive - One interactive session per active project.
Supports Claude, Codex, and OpenCode. Sessions loop forever via walkie-talkie.

Usage:
  python agent_loop_interactive.py                          # all active, claude
  python agent_loop_interactive.py --tool codex             # all active, codex
  python agent_loop_interactive.py --tool opencode "my-project" # one project, opencode
  python agent_loop_interactive.py 3                        # first 3, claude
  python agent_loop_interactive.py "my-project"                 # one project, claude

Run launch_orchestrator.py alongside to drive the workers via walkie-talkie.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")
AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
STAGGER_SECONDS = 3

# How each tool is launched.
# For claude: supports --append-system-prompt-file so prompt and message are separate.
# For codex/opencode: no system prompt file support, so we combine them into one message.
TOOL_CONFIGS = {
    "claude": {
        "cmd": ["claude", "--model", "sonnet", "--dangerously-skip-permissions"],
        "system_prompt_flag": "--append-system-prompt-file",  # None = not supported
    },
    "codex": {
        "cmd": ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        "system_prompt_flag": None,
    },
    "opencode": {
        "cmd": ["opencode", "run"],
        "system_prompt_flag": None,
    },
}


def load_projects():
    with open(PROJECTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_active_projects(projects):
    now = datetime.now().isoformat()
    return [
        p for p in projects
        if p["status"] in ("in_progress", "improving")
        and not p.get("blocked")
        and (not p.get("wait_until") or p["wait_until"] < now)
    ]


def get_project_dir(project):
    path = project.get("path", project["name"])
    return os.path.join(PROJECTS_BASE, path).replace("\\", "/")


def make_handle(name):
    handle = name.lower()
    handle = re.sub(r"[^a-z0-9]+", "-", handle)
    handle = handle.strip("-")[:20]
    return handle


def build_worker_prompt(project):
    handle = make_handle(project["name"])
    name = project["name"]
    project_dir = get_project_dir(project)
    next_task = project.get("next_task", "Check CLAUDE.md and AGENTS.md for what to work on")

    return f"""You are an autonomous coding agent for project: {name}
Radio handle: {handle}
Working directory: {project_dir}
Projects file: {PROJECTS_FILE}

STARTUP — do this FIRST before anything else:
1. Call radio_join as "{handle}"
2. Call radio_over targeting @orchestrator on channel "#all": "READY {handle}: ready for task"
3. Call radio_standby to wait for your task on #all
4. If no task arrives within 30 seconds, send READY again on #all and radio_standby again
5. Keep repeating until you receive a TASK

WORKFLOW (repeat forever):
- When you receive "TASK: [description]" from the orchestrator:
  1. Reply on #all: radio_over @orchestrator "RECEIVED {handle}: starting now"
  2. Read CLAUDE.md and AGENTS.md in {project_dir} for project context
  3. Complete the task using your tools
  4. Update {PROJECTS_FILE}: set completed_task, update next_task, append to log
  5. Report on #all: radio_over @orchestrator "DONE {handle}: [one sentence summary]"
  6. radio_standby to wait for your next task on #all

- If blocked:
  1. Update {PROJECTS_FILE}: set blocked=true, blocker_description
  2. Report on #all: radio_over @orchestrator "BLOCKED {handle}: [reason]"
  3. radio_standby to wait on #all

- If the user types to you directly: respond and help them, then return to radio_standby

NEVER stop between tasks. Always either working or waiting at radio_standby.
Current task (for reference): {next_task}
"""


def launch_tab(project, tool="claude"):
    project_dir = get_project_dir(project)
    handle = make_handle(project["name"])
    config = TOOL_CONFIGS.get(tool, TOOL_CONFIGS["claude"])

    prompt = build_worker_prompt(project)
    initial_msg = f"DO THIS NOW: 1. Call radio_join as '{handle}'. 2. Call radio_over @orchestrator on channel '#all' with 'READY {handle}: ready for task'. 3. Call radio_standby. If no task arrives within 30 seconds, resend READY on '#all' and radio_standby again. Repeat until you receive a TASK."

    base_cmd = config["cmd"].copy()

    # Add project dir for claude (--add-dir flag)
    if tool == "claude":
        base_cmd.extend(["--add-dir", AGENTLOOP_DIR])

    if config["system_prompt_flag"]:
        # Claude: write system prompt to file, pass initial message separately
        prompt_file = os.path.join(tempfile.gettempdir(), f"agentloop_{handle}.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        full_cmd = base_cmd + [config["system_prompt_flag"], prompt_file, initial_msg]
    else:
        # Codex/OpenCode: action first so it executes immediately, context after
        combined = initial_msg + "\n\n" + prompt
        full_cmd = base_cmd + [combined]

    # npm tools (codex, opencode) are .cmd scripts on Windows — must run via cmd /k
    if full_cmd[0] in ("codex", "opencode"):
        full_cmd = ["cmd", "/k"] + full_cmd

    if shutil.which("wt"):
        subprocess.Popen([
            "wt", "-w", "0", "new-tab",
            "--title", f"[{tool}] {project['name']}",
            "-d", project_dir,
            "--", *full_cmd,
        ])
    else:
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in full_cmd)
        subprocess.Popen(
            f'start "[{tool}] {project["name"]}" {cmd_str}',
            shell=True,
            cwd=project_dir,
        )


def print_status(projects, tool):
    print(f"\n=== AgentLoop Interactive [{tool}] ===")
    for p in projects:
        icon = {
            "in_progress": "[ACTIVE]",
            "improving":   "[IMPROVE]",
            "paused":      "[PAUSED]",
            "complete":    "[DONE]",
            "blocked":     "[BLOCKED]",
        }.get(p["status"], "[?]")
        blocked = " BLOCKED" if p.get("blocked") else ""
        waiting = f" (waiting until {p['wait_until']})" if p.get("wait_until") else ""
        print(f"  {icon}{blocked}{waiting} {p['name']}")
    print("=" * 40 + "\n")


def parse_args():
    """Parse --tool flag and remaining args. Returns (tool, remaining_args)."""
    args = sys.argv[1:]
    tool = "claude"
    if "--tool" in args:
        idx = args.index("--tool")
        if idx + 1 < len(args):
            tool = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("--tool requires a value: claude, codex, or opencode")
            sys.exit(1)
    if tool not in TOOL_CONFIGS:
        print(f"Unknown tool '{tool}'. Choose from: {', '.join(TOOL_CONFIGS)}")
        sys.exit(1)
    return tool, args


def main():
    tool, args = parse_args()
    projects = load_projects()
    print_status(projects, tool)

    active = get_active_projects(projects)

    if args:
        arg = args[0]
        try:
            count = int(arg)
            active = active[:count]
        except ValueError:
            match = [p for p in projects if p["name"].lower() == arg.lower()]
            if not match:
                match = [p for p in projects if arg.lower() in p["name"].lower()]
            if match:
                active = match
            else:
                print(f"No project matching '{arg}' found.")
                return

    if not active:
        print("No active projects to launch.")
        return

    print(f"Launching {len(active)} {tool} session(s)...\n")

    for i, project in enumerate(active):
        print(f"  Opening [{tool}] tab: {project['name']}")
        launch_tab(project, tool)
        if i < len(active) - 1:
            time.sleep(STAGGER_SECONDS)

    print(f"\nAll {tool} sessions launched!")
    print("Switch between tabs to watch. Type in any tab to steer.")


if __name__ == "__main__":
    main()
