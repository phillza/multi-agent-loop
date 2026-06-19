"""
launch_direct_session.py - Launch one direct autonomous session in a project folder.

This is for "just work on this project" sessions that are not part of the
walkie-talkie worker/orchestrator loop.

Usage:
  python launch_direct_session.py "my-project"
  python launch_direct_session.py "my-project" --tool codex --goal "Improve exporter reliability"
  python launch_direct_session.py "my-project" --tool codex --goal "Audit completed work and plan the next priorities"
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile


AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_FILE = os.path.join(AGENTLOOP_DIR, "projects.json")
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
TOOL_CONFIGS = {
    "claude": {
        "base": ["claude", "--dangerously-skip-permissions"],
        "model_flag": "--model",
        "effort_flag": "--effort",
        "prompt_file_flag": "--append-system-prompt-file",
        "default_model": "claude-sonnet-4-6",
    },
    "codex": {
        "base": ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        "model_flag": "--model",
        "effort_flag": None,
        "prompt_file_flag": None,
        "default_model": "gpt-5.4",
    },
    "opencode": {
        "base": ["opencode"],
        "model_flag": "--model",
        "effort_flag": None,
        "prompt_file_flag": None,
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


def resolve_model(tool, model):
    if not model:
        return model
    if tool == "claude":
        return CLAUDE_MODELS.get(model, model)
    return model


def build_prompt(project, goal):
    project_dir = get_project_dir(project)
    goals = project.get("goals") or []
    goals_text = "\n".join(f"- {item}" for item in goals) if goals else "- No explicit goals listed in projects.json."
    goal_line = goal or project.get("next_task") or "Identify the highest-impact improvement and keep pushing the project forward."


    return f"""You are working autonomously on project: {project['name']}
Project directory: {project_dir}
Projects file: {PROJECTS_FILE}

Primary goal for this session:
{goal_line}

Project goals:
{goals_text}

WORK STYLE:
- Start by reading AGENTS.md and CLAUDE.md if they exist
- Inspect the current code, entrypoints, tests, and recent artifacts before choosing what to do
- Choose the highest-impact next step and execute it
- After each completed chunk of work, reassess the project state and immediately continue with the next best step
- Do not stop after one task just to give a summary
- Keep moving until you hit a real blocker, the project reaches a good stopping point, or the user interrupts you
- Run relevant verification commands as you go
- Prefer practical reliability and product-impact improvements over cosmetic work
- If you learn something important, update the project's existing docs or task board when appropriate

STOP RULE:
- Only stop and wait if you are truly blocked, need a decision from the user, or have reached a clear natural stopping point after multiple meaningful improvements
"""


def build_cmd(tool, model, effort, prompt):
    cfg = TOOL_CONFIGS[tool]
    cmd = cfg["base"].copy()
    resolved_model = resolve_model(tool, model)
    if resolved_model and cfg.get("model_flag"):
        cmd += [cfg["model_flag"], resolved_model]
    if effort and cfg.get("effort_flag"):
        cmd += [cfg["effort_flag"], effort]

    if cfg.get("prompt_file_flag"):
        prompt_file = os.path.join(tempfile.gettempdir(), f"agentloop_direct_{os.getpid()}.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd += [cfg["prompt_file_flag"], prompt_file, "Start now and keep going until blocked or interrupted."]
    elif cfg.get("prompt_flag"):
        cmd += [cfg["prompt_flag"], "Start now and keep going until blocked or interrupted.\n\n" + prompt]
    else:
        cmd += ["Start now and keep going until blocked or interrupted.\n\n" + prompt]

    return cmd


def open_tab(title, cwd, cmd):
    runner_file = os.path.join(tempfile.gettempdir(), f"agentloop_cmd_{os.getpid()}_{abs(hash(title))}.json")
    with open(runner_file, "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "cwd": cwd}, f)
    cmd = ["cmd", "/k", "python", os.path.join(AGENTLOOP_DIR, "run_saved_command.py"), runner_file]
    if shutil.which("wt"):
        subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", title, "-d", cwd, "--", *cmd])
    else:
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        subprocess.Popen(f'start "{title}" {cmd_str}', shell=True, cwd=cwd)


def main():
    parser = argparse.ArgumentParser(description="Launch one direct autonomous project session.")
    parser.add_argument("project", help="Project name (partial match ok)")
    parser.add_argument("--tool", choices=list(TOOL_CONFIGS), default="codex", help="Tool to launch (default: codex)")
    parser.add_argument("--model", default=None, help="Model override")
    parser.add_argument("--effort", choices=["low", "medium", "high"], default="high", help="Thinking effort when supported")
    parser.add_argument("--goal", default=None, help="Session goal override")
    args = parser.parse_args()

    project = find_project(args.project)
    project_dir = get_project_dir(project)
    prompt = build_prompt(project, args.goal)
    cmd = build_cmd(args.tool, args.model or TOOL_CONFIGS[args.tool]["default_model"], args.effort, prompt)
    open_tab(f"[{args.tool}] {project['name']}", project_dir, cmd)
    print(f"Launched [{args.tool}] direct session for {project['name']}")


if __name__ == "__main__":
    main()
