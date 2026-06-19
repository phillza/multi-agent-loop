"""
run_strategy_loop.py - Re-run a strategist session in the same terminal on a timer.

This is meant for project strategist sessions that should periodically reassess
the board instead of running once and idling forever.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_FILE = os.path.join(AGENTLOOP_DIR, "projects.json")
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
TOOL_CONFIGS = {
    "claude": {
        "base": ["claude", "--dangerously-skip-permissions"],
        "model_flag": "--model",
        "effort_flag": "--effort",
        "default_model": "claude-sonnet-4-6",
    },
    "codex": {
        "base": ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        "model_flag": "--model",
        "effort_flag": None,
        "default_model": "gpt-5.4",
    },
    "opencode": {
        "base": ["opencode"],
        "model_flag": "--model",
        "effort_flag": None,
        "prompt_flag": "--prompt",
        "default_model": None,
    },
}

CLAUDE_MODELS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def load_projects():
    with open(PROJECTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def find_project(name_query):
    projects = load_projects()
    exact = next((p for p in projects if p["name"].lower() == name_query.lower()), None)
    if exact:
        return exact
    partial = [p for p in projects if name_query.lower() in p["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        print(f"Ambiguous name '{name_query}'. Matches:")
        for project in partial:
            print(f"  - {project['name']}")
        sys.exit(1)
    print(f"No project matching '{name_query}' found.")
    sys.exit(1)


def get_project_dir(project):
    path = project.get("path", project["name"])
    return os.path.join(PROJECTS_BASE, path).replace("\\", "/")


def find_task_board(project_dir):
    candidates = [
        os.path.join(project_dir, "TASK_BOARD.md"),
        os.path.join(project_dir, "tasks", "TASK_BOARD.md"),
        os.path.join(project_dir, "tasks", "AGENT_STARTUP.md"),
    ]
    found = [path for path in candidates if os.path.exists(path)]
    return found[0] if found else None


def resolve_model(tool, model):
    if not model:
        return model
    if tool == "claude":
        return CLAUDE_MODELS.get(model, model)
    return model


def resolve_windows_command(executable):
    appdata_cmd = os.path.join(os.environ.get("APPDATA", ""), "npm", f"{executable}.cmd")
    if os.path.exists(appdata_cmd):
        return appdata_cmd

    for suffix in (".cmd", ".exe"):
        resolved = shutil.which(f"{executable}{suffix}")
        if resolved:
            return resolved

    return shutil.which(executable) or executable


def open_ephemeral_tab_safe(title, cwd, cmd):
    runner_file = os.path.join(tempfile.gettempdir(), f"agentloop_cmd_{int(time.time() * 1000)}.json")
    with open(runner_file, "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "cwd": cwd}, f)

    wrapped_cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        os.path.join(AGENTLOOP_DIR, "run_saved_command.ps1"),
        runner_file,
    ]

    if shutil.which("wt"):
        subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", title, "-d", cwd, "--", *wrapped_cmd])
        return

    cmd_str = " ".join(f'"{part}"' if " " in part else part for part in wrapped_cmd)
    subprocess.Popen(f'start "{title}" {cmd_str}', shell=True, cwd=cwd)


def build_strategist_prompt(project):
    project_dir = get_project_dir(project)
    task_board = find_task_board(project_dir)
    task_board_line = f"Task board: {task_board}" if task_board else "No task board found."
    goals = project.get("goals") or []
    goals_text = "\n".join(f"- {goal}" for goal in goals) if goals else "- Use the business outcome in projects.json as the planning guide."

    return f"""You are the strategist for project: {project['name']}
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
- Run one meaningful planning/execution pass, then stop naturally so this loop can revisit the project later

Keep working autonomously until you hit a real blocker or reach a clear stopping point for this pass.
"""


def build_cmd(tool, model, effort, prompt):
    cfg = TOOL_CONFIGS[tool]
    cmd = cfg["base"].copy()
    cmd[0] = resolve_windows_command(cmd[0])
    resolved_model = resolve_model(tool, model)
    if resolved_model and cfg.get("model_flag"):
        cmd += [cfg["model_flag"], resolved_model]
    if effort and cfg.get("effort_flag"):
        cmd += [cfg["effort_flag"], effort]

    start_msg = "Start now. Run one high-value strategist pass, then stop when you reach a clear stopping point for this pass.\n\n"
    if cfg.get("prompt_flag"):
        cmd += [cfg["prompt_flag"], start_msg + prompt]
    else:
        cmd += [start_msg + prompt]
    return cmd


def main():
    parser = argparse.ArgumentParser(description="Run a strategist session in a timed loop.")
    parser.add_argument("project", help="Project name (partial match ok)")
    parser.add_argument("--tool", choices=list(TOOL_CONFIGS), default="codex")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", choices=["low", "medium", "high"], default="high")
    parser.add_argument("--interval-minutes", type=int, default=30)
    args = parser.parse_args()

    project = find_project(args.project)
    project_dir = get_project_dir(project)
    prompt = build_strategist_prompt(project)
    cmd = build_cmd(args.tool, args.model or TOOL_CONFIGS[args.tool]["default_model"], args.effort, prompt)

    print(f"Starting strategist loop for {project['name']} every {args.interval_minutes} minute(s).", flush=True)

    tab_title = f"[STRATEGIST-{args.tool}] {project['name']}"

    while True:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting strategist pass...", flush=True)
        try:
            open_ephemeral_tab_safe(tab_title, project_dir, cmd)
        except KeyboardInterrupt:
            print("Strategist loop interrupted by user.", flush=True)
            return

        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Strategist pass launched in a new tab. "
            f"Sleeping {args.interval_minutes} minute(s) before the next pass.",
            flush=True,
        )
        try:
            time.sleep(max(1, args.interval_minutes) * 60)
        except KeyboardInterrupt:
            print("Strategist loop interrupted by user.", flush=True)
            return


if __name__ == "__main__":
    main()
