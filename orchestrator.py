"""
AgentLoop Orchestrator - Drives interactive workers via walkie-talkie.

Launches a full interactive Claude session that:
- Joins walkie-talkie as "orchestrator"
- Waits for READY messages from workers
- Listens for DONE/BLOCKED messages
- Sends the next task only after the worker is waiting

Run this AFTER agent_loop_interactive.py (or alongside it).
Workers must be running and connected to walkie-talkie first.

Usage:
  python orchestrator.py              # Drive all active projects
  python orchestrator.py 3            # First 3 active projects
  python orchestrator.py "my-project"     # One specific project
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")
AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
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


def make_handle(name):
    handle = name.lower()
    handle = re.sub(r"[^a-z0-9]+", "-", handle)
    handle = handle.strip("-")[:20]
    return handle


def build_orchestrator_prompt(active_projects):
    worker_list = "\n".join(
        f"  - Handle: {make_handle(p['name'])} | Project: {p['name']} | Task: {p.get('next_task', 'check CLAUDE.md')[:100]}"
        for p in active_projects
    )

    return f"""You are the AgentLoop Orchestrator. You coordinate autonomous Claude workers via walkie-talkie.

ACTIVE WORKERS:
{worker_list}

PROJECTS FILE: {PROJECTS_FILE}

STARTUP:
1. Call radio_join as "orchestrator"
2. Call radio_standby on #all to wait for READY messages from workers

MAIN LOOP (repeat forever):
When you receive a message:

If it starts with "READY [handle]:":
  1. Read {PROJECTS_FILE} to get that project's current next_task
  2. Call radio_over targeting @[handle] on channel "#all" with: "TASK: [their current next_task from projects.json]"
  3. Call radio_standby again

If it starts with "DONE [handle]:":
  1. Read {PROJECTS_FILE} to get that project's updated next_task
  2. Call radio_over targeting @[handle] on channel "#all" with: "TASK: [new next_task]"
  3. Call radio_standby again

If it starts with "BLOCKED [handle]:":
  1. Note the blocker - tell the user in your response
  2. Call radio_standby to keep listening to other workers
  3. Do NOT send that worker another task until the user resolves it

If a worker hasn't reported in a long time (15+ minutes):
  1. Re-send their last task on #all: radio_over @handle "TASK: [their current next_task from projects.json]"
  2. Return to radio_standby

NEVER stop. You are always either sending tasks or waiting at radio_standby.
The user can type to you at any time to check status or manually steer a worker.
"""


def launch_orchestrator(active_projects):
    prompt = build_orchestrator_prompt(active_projects)
    prompt_file = os.path.join(tempfile.gettempdir(), "agentloop_orchestrator.txt")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt)

    handles = [make_handle(p["name"]) for p in active_projects]
    initial_msg = (
        f"Start now. Join walkie-talkie as 'orchestrator', then radio_standby on #all. "
        f"Wait for READY messages from workers before sending any TASK messages. "
        f"Workers: {', '.join(handles)}"
    )

    if shutil.which("wt"):
        subprocess.Popen([
            "wt", "-w", "0", "new-tab",
            "--title", "Orchestrator",
            "-d", AGENTLOOP_DIR,
            "--", "claude",
            "--model", "sonnet",
            "--dangerously-skip-permissions",
            "--add-dir", AGENTLOOP_DIR,
            "--append-system-prompt-file", prompt_file,
            initial_msg,
        ])
    else:
        subprocess.Popen(
            f'start "Orchestrator" claude --model sonnet '
            f'--dangerously-skip-permissions '
            f'--add-dir "{AGENTLOOP_DIR}" '
            f'--append-system-prompt-file "{prompt_file}" '
            f'"{initial_msg}"',
            shell=True,
            cwd=AGENTLOOP_DIR,
        )


def print_plan(active_projects):
    print("\n=== AgentLoop Orchestrator ===")
    print(f"Driving {len(active_projects)} worker(s) via walkie-talkie:\n")
    for p in active_projects:
        handle = make_handle(p["name"])
        task = p.get("next_task", "no task set")[:80]
        print(f"  [{handle}] {p['name']}")
        print(f"    Task: {task}")
    print("\nMAKE SURE:")
    print("  1. Walkie-talkie hub is running (cd walkie-talkie && npm start)")
    print("  2. Workers are launched (python agent_loop_interactive.py)")
    print("  3. Workers have joined their radio channels")
    print("==============================\n")


def main():
    projects = load_projects()
    active = get_active_projects(projects)

    if len(sys.argv) > 1:
        arg = sys.argv[1]
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
        print("No active projects found.")
        return

    print_plan(active)
    print("Launching orchestrator tab...")
    launch_orchestrator(active)
    print("Orchestrator launched. Check the new terminal tab.")


if __name__ == "__main__":
    main()
