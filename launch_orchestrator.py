"""
launch_orchestrator.py - Launch the single global orchestrator.

Run this ONCE. It coordinates all active project workers via walkie-talkie.
Workers are launched separately via launch_all.py or agent_loop_interactive.py.

Usage:
  python launch_orchestrator.py                    # all active projects, sonnet
  python launch_orchestrator.py --model opus       # use opus for better reasoning
  python launch_orchestrator.py "my-project" "my-project"  # only watch specific projects
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_FILE = os.path.join(AGENTLOOP_DIR, "projects.json")
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
CLAUDE_MODELS = {
    "opus":   "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
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


def build_prompt(active_projects):
    worker_list = "\n".join(
        f"  - Handle: {make_handle(p['name'])} | Project: {p['name']} | Dir: {get_project_dir(p)} | Task: {p.get('next_task', 'check CLAUDE.md')[:80]}"
        for p in active_projects
    )
    handles_str = ", ".join(make_handle(p["name"]) for p in active_projects)

    return f"""You are the AgentLoop Global Orchestrator — the single coordinator for all active projects.
There is only ONE instance of you. Do not launch or suggest launching another orchestrator.

ACTIVE PROJECTS:
{worker_list}

Expected worker handles: {handles_str}
PROJECTS FILE: {PROJECTS_FILE}
PROJECTS BASE: {PROJECTS_BASE}

YOUR JOB:
- Keep every worker productive at all times
- Read each project's CLAUDE.md and AGENTS.md to understand what needs doing
- When next_task is empty or project status is "improving", brainstorm what to work on next
- Track what each worker reports and what still needs doing

STARTUP:
1. radio_join as "orchestrator"
2. radio_standby on #all — wait for workers to announce themselves

HANDSHAKE (never send a task until you receive READY):
- Workers announce: "READY [handle]: ready for task"
- When you receive READY from a worker:
  1. Read their project entry in {PROJECTS_FILE}
  2. radio_over @[handle] on channel "#all" "TASK: [next_task from projects.json]"
- Worker confirms: "RECEIVED [handle]: starting now"
- Then radio_standby again

WHEN A WORKER REPORTS "DONE [handle]: [summary]":
1. Read {PROJECTS_FILE} — check next_task for that project
2. If next_task is specific: radio_over @handle on channel "#all" "TASK: [next_task]"
3. If next_task is empty or vague or status is "improving":
   - radio_over @handle on channel "#all" "TASK: Read your CLAUDE.md and AGENTS.md, brainstorm the most impactful improvement you can make, implement it, and update next_task in {PROJECTS_FILE} when done."
4. radio_standby for next response

WHEN A WORKER REPORTS "BLOCKED [handle]: [reason]":
- Note the blocker in your output so the user can see it
- Skip that project for now, keep other workers going

QUIET WORKER (15+ min with no response):
- radio_over @handle on channel "#all" "TASK: [their current task from {PROJECTS_FILE}]" to resend

Note: some handles are project orchestrators managing their own internal teams.
Just send tasks to the project handle — you don't manage their internal workers.

NEVER stop. You are always: polling for workers, reading project files, sending tasks, or at radio_standby.
"""


def main():
    parser = argparse.ArgumentParser(description="Launch the global AgentLoop orchestrator.")
    parser.add_argument("projects", nargs="*", help="Project names to manage (default: all active)")
    parser.add_argument("--model", default="sonnet", help="Claude model: opus, sonnet, haiku (default: sonnet)")
    args = parser.parse_args()

    all_projects = load_projects()
    active = get_active_projects(all_projects)

    if args.projects:
        active = filter_by_names(all_projects, args.projects)

    if not active:
        print("No active projects found.")
        return

    model = CLAUDE_MODELS.get(args.model, args.model)
    handles = [make_handle(p["name"]) for p in active]

    print("\n=== Global Orchestrator ===")
    print(f"  Model:    claude {model}")
    print(f"  Projects: {', '.join(p['name'] for p in active)}")
    print(f"  Handles:  {', '.join(handles)}")
    print("  Walkie-talkie hub must be running at http://localhost:9559\n")

    prompt = build_prompt(active)
    pf = os.path.join(tempfile.gettempdir(), "agentloop_global_orchestrator.txt")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(prompt)

    initial_msg = (
        "You are the global orchestrator. Join walkie-talkie as 'orchestrator', "
        "then radio_standby on #all. Workers will announce themselves with 'READY [handle]: ready for task'. "
        "When you receive a READY message, read their project entry in projects.json and send them their task. "
        "Do not send any tasks until a worker announces they are ready."
    )

    cmd = [
        "claude", "--dangerously-skip-permissions",
        "--model", model,
        "--add-dir", AGENTLOOP_DIR,
        "--append-system-prompt-file", pf,
        initial_msg,
    ]

    runner_file = os.path.join(tempfile.gettempdir(), "agentloop_global_orchestrator_cmd.json")
    with open(runner_file, "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "cwd": AGENTLOOP_DIR}, f)
    wrapped_cmd = ["cmd", "/k", "python", os.path.join(AGENTLOOP_DIR, "run_saved_command.py"), runner_file]

    if shutil.which("wt"):
        subprocess.Popen([
            "wt", "-w", "0", "new-tab",
            "--title", f"[GLOBAL ORCH] {model}",
            "-d", AGENTLOOP_DIR,
            "--", *wrapped_cmd,
        ])
    else:
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in wrapped_cmd)
        subprocess.Popen(f'start "[GLOBAL ORCH]" {cmd_str}', shell=True, cwd=AGENTLOOP_DIR)

    print("Global orchestrator launched!")
    print("Monitor at http://localhost:9559")


if __name__ == "__main__":
    main()
