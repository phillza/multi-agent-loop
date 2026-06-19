"""
AgentLoop - Launches workers in separate terminal tabs.
Each worker runs in its own Windows Terminal tab so you can:
  - Watch Claude's thinking and tool calls in real-time
  - Press Ctrl+C to interrupt and take over interactively
  - See all workers at a glance by switching tabs

Usage:
  python agent_loop.py              # Launch all active projects
  python agent_loop.py 3            # Launch first 3 active projects
  python agent_loop.py "my-project"     # Launch just one project by name
"""

import subprocess
import json
import sys
import os
import time
import shutil

PROJECTS_FILE = "projects.json"
WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
STAGGER_SECONDS = 3  # delay between launching tabs to avoid rate limits


def load_projects():
    with open(PROJECTS_FILE) as f:
        return json.load(f)


def get_active_projects(projects):
    return [p for p in projects if p["status"] in ("in_progress", "improving")
            and not p.get("blocked")]


def launch_worker_tab(project_name):
    """Open a new Windows Terminal tab running the worker for this project."""
    # Escape quotes in project name for the command
    safe_name = project_name.replace('"', '\\"')
    python_exe = sys.executable

    # Try Windows Terminal first (wt), fall back to start cmd
    if shutil.which("wt"):
        # Windows Terminal: new tab with title
        subprocess.Popen([
            "wt", "-w", "0", "new-tab",
            "--title", project_name,
            python_exe, WORKER_SCRIPT, project_name
        ])
    else:
        # Fallback: new cmd window
        subprocess.Popen(
            f'start "{project_name}" "{python_exe}" "{WORKER_SCRIPT}" "{safe_name}"',
            shell=True
        )


def print_status(projects):
    print("\n=== AgentLoop Status ===")
    for p in projects:
        status_icon = {
            "in_progress": "[ACTIVE]",
            "improving": "[IMPROVE]",
            "paused": "[PAUSED]",
            "complete": "[DONE]",
            "blocked": "[BLOCKED]",
        }.get(p["status"], "[?]")
        blocked = " BLOCKED" if p.get("blocked") else ""
        waiting = f" (waiting until {p['wait_until']})" if p.get("wait_until") else ""
        print(f"  {status_icon}{blocked}{waiting} {p['name']}")
    print("========================\n")


def main():
    projects = load_projects()
    print_status(projects)

    active = get_active_projects(projects)

    # Handle command line args
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        # If it's a number, launch that many projects
        try:
            count = int(arg)
            active = active[:count]
        except ValueError:
            # It's a project name - find it
            match = [p for p in projects if p["name"].lower() == arg.lower()]
            if not match:
                # Fuzzy match
                match = [p for p in projects if arg.lower() in p["name"].lower()]
            if match:
                active = match
            else:
                print(f"No project matching '{arg}' found.")
                return

    if not active:
        print("No active projects to launch.")
        print("Set projects to 'in_progress' in projects.json or the dashboard.")
        return

    print(f"Launching {len(active)} worker(s) in terminal tabs...\n")

    for i, project in enumerate(active):
        print(f"  Opening tab: {project['name']}")
        launch_worker_tab(project["name"])
        if i < len(active) - 1:
            time.sleep(STAGGER_SECONDS)

    print(f"\nAll workers launched!")
    print(f"Switch between tabs to watch each worker.")
    print(f"Press Ctrl+C in any tab to take over that session.")


if __name__ == "__main__":
    main()
