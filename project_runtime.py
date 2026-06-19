from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime_store import file_lock, read_json, write_json_atomic


AGENTLOOP_DIR = Path(__file__).resolve().parent
PROJECTS_FILE = AGENTLOOP_DIR / "projects.json"
PROJECTS_LOCK_FILE = PROJECTS_FILE.with_suffix(".lock")
TEAMS_FILE = AGENTLOOP_DIR / "teams.json"
RUNTIME_OVERRIDES_FILE = AGENTLOOP_DIR / "runtime_overrides.json"
PROJECTS_BASE = Path(os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd())
TEAM_DEFAULTS = {
    "orch_tool": "claude",
    "orch_model": "opus",
    "orch_effort": "medium",
    "worker_tool": "opencode",
    "worker_model": None,
    "workers": 3,
    "channel": None,
    "strategist": False,
    "strategist_tool": "codex",
    "strategist_model": "gpt-5.4",
    "strategist_effort": "high",
    "strategist_interval_minutes": 30,
}

SINGLE_WORKER_DEFAULTS = {
    "tool": "claude",
    "model": "sonnet",
    "effort": "high",
}

STRATEGIST_DEFAULTS = {
    "tool": "codex",
    "model": "gpt-5.4",
    "effort": "high",
    "interval_minutes": 30,
}

DOC_CANDIDATES = [
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "README.txt",
    "docs/README.md",
]


@dataclass
class WorkerProfile:
    slot: str
    role: str
    tool: str
    model: str | None = None
    effort: str | None = None


@dataclass
class StrategistProfile:
    enabled: bool
    tool: str | None = None
    model: str | None = None
    effort: str | None = None
    interval_minutes: int = 30
    trigger_on_empty_queue: bool = False


@dataclass
class ProjectRuntime:
    name: str
    project_dir: Path
    project_record: dict[str, Any]
    task_backend: str
    task_board_path: Path | None = None
    docs: list[Path] = field(default_factory=list)
    worker_profiles: list[WorkerProfile] = field(default_factory=list)
    strategist: StrategistProfile | None = None
    orchestrator_enabled: bool = False
    orchestrator_tool: str | None = None
    orchestrator_model: str | None = None
    orchestrator_effort: str | None = None
    team_channel: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        chars = []
        for char in self.name.lower():
            chars.append(char if char.isalnum() else "-")
        text = "".join(chars).strip("-")
        while "--" in text:
            text = text.replace("--", "-")
        return text or "project"


def load_projects() -> list[dict[str, Any]]:
    with PROJECTS_FILE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_teams() -> dict[str, Any]:
    data = read_json(TEAMS_FILE, {})
    return {key: value for key, value in data.items() if not key.startswith("_")}


def load_runtime_overrides() -> dict[str, Any]:
    raw = read_json(RUNTIME_OVERRIDES_FILE, {})
    if "projects" in raw:
        return raw
    return {"defaults": {}, "projects": raw}


def find_project(name_query: str) -> dict[str, Any]:
    projects = load_projects()
    exact = next((project for project in projects if project["name"].lower() == name_query.lower()), None)
    if exact:
        return exact
    partial = [project for project in projects if name_query.lower() in project["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        matches = ", ".join(project["name"] for project in partial)
        raise ValueError(f"Ambiguous project name '{name_query}'. Matches: {matches}")
    raise ValueError(f"No project matching '{name_query}' found.")


def get_project_dir(project: dict[str, Any]) -> Path:
    return (PROJECTS_BASE / project.get("path", project["name"])).resolve()


def find_task_board(project_dir: Path) -> Path | None:
    for relative in ("TASK_BOARD.md", "tasks/TASK_BOARD.md", "tasks/AGENT_STARTUP.md"):
        candidate = project_dir / relative
        if candidate.exists():
            return candidate
    return None


def find_docs(project_dir: Path, override_docs: list[str] | None = None) -> list[Path]:
    docs: list[Path] = []
    candidates = override_docs or DOC_CANDIDATES
    for relative in candidates:
        candidate = (project_dir / relative).resolve()
        if candidate.exists():
            docs.append(candidate)
    return docs


def update_project_record(project_name: str, updates: dict[str, Any], log_entry: str | None = None) -> dict[str, Any]:
    with file_lock(PROJECTS_LOCK_FILE):
        projects = load_projects()
        updated: dict[str, Any] | None = None
        for project in projects:
            if project["name"] != project_name:
                continue
            for key, value in updates.items():
                project[key] = value
            if log_entry:
                log = project.setdefault("log", [])
                log.append(log_entry)
                project["log"] = log[-30:]
                project["completed_task"] = log_entry
            updated = project
            break
        if updated is None:
            raise ValueError(f"Project '{project_name}' not found in projects.json")
        write_json_atomic(PROJECTS_FILE, projects)
        return updated


def _team_worker_profiles(cfg: dict[str, Any]) -> list[WorkerProfile]:
    raw_workers = cfg["workers"]
    if isinstance(raw_workers, list):
        worker_tools = raw_workers
    else:
        worker_tools = [cfg["worker_tool"]] * int(raw_workers)
    worker_models = cfg.get("worker_models")
    if worker_models and len(worker_models) != len(worker_tools):
        raise ValueError(
            f"Configured worker_models has {len(worker_models)} values but {len(worker_tools)} workers are defined."
        )

    profiles: list[WorkerProfile] = []
    for index, tool in enumerate(worker_tools, start=1):
        profiles.append(
            WorkerProfile(
                slot=f"worker-{index}",
                role="worker",
                tool=tool,
                model=worker_models[index - 1] if worker_models else cfg.get("worker_model"),
                effort=None,
            )
        )
    return profiles


def _single_worker_profiles(project: dict[str, Any]) -> list[WorkerProfile]:
    cfg = {**SINGLE_WORKER_DEFAULTS, **project.get("worker_config", {})}
    return [
        WorkerProfile(
            slot="builder",
            role="worker",
            tool=cfg["tool"],
            model=cfg.get("model"),
            effort=cfg.get("effort"),
        )
    ]


def discover_project_runtime(project_name: str) -> ProjectRuntime:
    project = find_project(project_name)
    project_dir = get_project_dir(project)
    teams = load_teams()
    overrides_blob = load_runtime_overrides()
    override_defaults = overrides_blob.get("defaults", {})
    overrides = overrides_blob.get("projects", {}).get(project["name"], {})

    team_cfg = teams.get(project["name"])
    runtime_cfg = {**override_defaults, **overrides}
    task_board = None
    if runtime_cfg.get("task_board"):
        override_board = (project_dir / runtime_cfg["task_board"]).resolve()
        if override_board.exists():
            task_board = override_board
    if task_board is None:
        task_board = find_task_board(project_dir)

    if runtime_cfg.get("task_backend"):
        task_backend = runtime_cfg["task_backend"]
    elif task_board:
        task_backend = "markdown_board"
    else:
        task_backend = "projects_json"

    docs = find_docs(project_dir, runtime_cfg.get("docs"))

    if team_cfg:
        cfg = {**TEAM_DEFAULTS, **team_cfg, **{key: value for key, value in runtime_cfg.items() if key in TEAM_DEFAULTS or key == "worker_models"}}
        team_channel = runtime_cfg.get("channel") or cfg.get("channel") or project["name"]
        strategist_enabled = bool(runtime_cfg.get("strategist", cfg.get("strategist")))
        strategist = StrategistProfile(
            enabled=strategist_enabled,
            tool=runtime_cfg.get("strategist_tool", cfg.get("strategist_tool")),
            model=runtime_cfg.get("strategist_model", cfg.get("strategist_model")),
            effort=runtime_cfg.get("strategist_effort", cfg.get("strategist_effort")),
            interval_minutes=int(runtime_cfg.get("strategist_interval_minutes", cfg.get("strategist_interval_minutes", 30))),
            trigger_on_empty_queue=bool(runtime_cfg.get("auto_strategist_on_empty_queue", True)),
        )
        return ProjectRuntime(
            name=project["name"],
            project_dir=project_dir,
            project_record=project,
            task_backend=task_backend,
            task_board_path=task_board,
            docs=docs,
            worker_profiles=_team_worker_profiles(cfg),
            strategist=strategist,
            orchestrator_enabled=True,
            orchestrator_tool=runtime_cfg.get("orch_tool", cfg.get("orch_tool")),
            orchestrator_model=runtime_cfg.get("orch_model", cfg.get("orch_model")),
            orchestrator_effort=runtime_cfg.get("orch_effort", cfg.get("orch_effort")),
            team_channel=team_channel,
            metadata=runtime_cfg,
        )

    single_worker_cfg = {**project.get("worker_config", {}), **{key: value for key, value in runtime_cfg.items() if key in SINGLE_WORKER_DEFAULTS}}
    auto_strategist_on_empty_queue = bool(runtime_cfg.get("auto_strategist_on_empty_queue", True))
    strategist_enabled = bool(runtime_cfg.get("strategist", False))

    return ProjectRuntime(
        name=project["name"],
        project_dir=project_dir,
        project_record=project,
        task_backend=task_backend,
        task_board_path=task_board,
        docs=docs,
        worker_profiles=_single_worker_profiles({**project, "worker_config": single_worker_cfg}),
        strategist=StrategistProfile(
            enabled=strategist_enabled,
            tool=runtime_cfg.get("strategist_tool") or STRATEGIST_DEFAULTS["tool"],
            model=runtime_cfg.get("strategist_model") or STRATEGIST_DEFAULTS["model"],
            effort=runtime_cfg.get("strategist_effort") or STRATEGIST_DEFAULTS["effort"],
            interval_minutes=int(runtime_cfg.get("strategist_interval_minutes", STRATEGIST_DEFAULTS["interval_minutes"])),
            trigger_on_empty_queue=auto_strategist_on_empty_queue,
        ),
        orchestrator_enabled=False,
        metadata=runtime_cfg,
    )
