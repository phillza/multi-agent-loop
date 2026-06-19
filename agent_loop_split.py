"""
AgentLoop Split - All active projects in one Windows Terminal tab as a grid of panes.
Each pane is a live interactive Claude session. Click any pane to type to it.

Layout:
  1 project  → full screen
  2 projects → 2 columns
  3 projects → 3 columns
  4 projects → 2x2 grid
  5 projects → 3 top / 2 bottom
  6 projects → 2x3 grid

Usage:
  python agent_loop_split.py              # All active projects
  python agent_loop_split.py 3            # First 3 active projects
  python agent_loop_split.py "my-project"     # One specific project
"""

import json
import os
import subprocess
import sys
from datetime import datetime

PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")
AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
def load_projects():
    with open(PROJECTS_FILE) as f:
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


def build_startup_prompt(project):
    name = project["name"]
    next_task = project.get("next_task", "Check CLAUDE.md and AGENTS.md for what to work on next")
    return (
        f"You are an autonomous coding agent for project: {name}. "
        f"Current task: {next_task}. "
        f"Start by reading CLAUDE.md and AGENTS.md in this folder for full context. "
        f"Complete the task, then update your entry in {PROJECTS_FILE} "
        f"(set completed_task, update next_task, append to log). "
        f"Then pick up the next task and keep going autonomously at high effort. "
        f"If blocked set blocked=true + blocker_description in projects.json. "
        f"The user may message you at any time to steer."
    )


def claude_cmd(project):
    """Build the claude command args for a single project pane."""
    return [
        "claude", "--model", "sonnet",
        "--add-dir", AGENTLOOP_DIR,
        build_startup_prompt(project),
    ]


def launch_split_panes(projects):
    """
    Build a single wt command that opens all projects as split panes.

    Layouts:
      1: [0]
      2: [0][1]          - 1 vertical split
      3: [0][1][2]        - 2 vertical splits
      4: [0][1]           - vertical split first, then horizontal on each column
         [2][3]
      5: [0][1][2]        - 3 columns on top, split bottom two columns
         [3][4]
      6: [0][1][2]        - 2 rows of 3 columns
         [3][4][5]
    """
    n = len(projects)
    cmd = ["wt", "--window", "0", "new-tab", "--title", "AgentLoop"]

    p = projects
    d = [get_project_dir(proj) for proj in p]
    c = [claude_cmd(proj) for proj in p]

    def new_tab(idx):
        return ["-d", d[idx], "--"] + c[idx]

    def split_v(idx, target=None, size=None):
        """Vertical split (side by side) — creates a new right column."""
        args = [";", "split-pane", "-V"]
        if target is not None:
            args += ["--target", str(target)]
        if size is not None:
            args += ["--size", str(size)]
        return args + ["-d", d[idx], "--"] + c[idx]

    def split_h(idx, target=None, size=None):
        """Horizontal split (top/bottom) — creates a new row within a column."""
        args = [";", "split-pane", "-H"]
        if target is not None:
            args += ["--target", str(target)]
        if size is not None:
            args += ["--size", str(size)]
        return args + ["-d", d[idx], "--"] + c[idx]

    if n == 1:
        cmd += new_tab(0)

    elif n == 2:
        cmd += new_tab(0)
        cmd += split_v(1, size=0.5)

    elif n == 3:
        cmd += new_tab(0)
        cmd += split_v(1, size=0.67)
        cmd += split_v(2, size=0.5)

    elif n == 4:
        # 2x2 grid: split into 2 columns first, then split each column
        cmd += new_tab(0)           # pane 0: top-left
        cmd += split_v(1, size=0.5) # pane 1: top-right
        cmd += split_h(2, target=0, size=0.5)  # pane 2: bottom-left (splits pane 0)
        cmd += split_h(3, target=1, size=0.5)  # pane 3: bottom-right (splits pane 1)

    elif n == 5:
        # 3 on top, 2 on bottom
        cmd += new_tab(0)
        cmd += split_v(1, size=0.67)
        cmd += split_v(2, size=0.5)
        cmd += split_h(3, target=0, size=0.5)  # bottom-left (splits pane 0)
        cmd += split_h(4, target=1, size=0.5)  # bottom-middle (splits pane 1)

    elif n == 6:
        # 2x3 grid: 3 columns, 2 rows
        cmd += new_tab(0)
        cmd += split_v(1, size=0.67)
        cmd += split_v(2, size=0.5)
        cmd += split_h(3, target=0, size=0.5)
        cmd += split_h(4, target=1, size=0.5)
        cmd += split_h(5, target=2, size=0.5)

    else:
        # More than 6: fall back to separate tabs
        print(f"Warning: {n} projects is too many for split panes. Opening first 6 as grid + rest as tabs.")
        launch_split_panes(projects[:6])
        for proj in projects[6:]:
            subprocess.Popen([
                "wt", "--window", "0", "new-tab",
                "--title", proj["name"],
                "-d", get_project_dir(proj),
                "--"] + claude_cmd(proj)
            )
        return

    subprocess.Popen(cmd)


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
            active = match if match else []

    if not active:
        print("No active projects. Set status to 'in_progress' in projects.json.")
        return

    print(f"\nOpening {len(active)} Claude session(s) in split panes...\n")
    for p in active:
        status = "WAITING" if p.get("wait_until") else p["status"].upper()
        print(f"  [{status}] {p['name']}")

    launch_split_panes(active)

    print("\nAll panes open in one Windows Terminal tab.")
    print("Click any pane to interact with that project's Claude.")


if __name__ == "__main__":
    main()
