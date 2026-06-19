from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime_store import read_json, write_json_atomic


RUNTIME_OVERRIDES_FILE = Path(__file__).resolve().parent / "runtime_overrides.json"


def load_runtime_overrides() -> dict[str, Any]:
    raw = read_json(RUNTIME_OVERRIDES_FILE, {})
    if "projects" in raw:
        raw.setdefault("defaults", {})
        return raw
    return {"defaults": {}, "projects": raw}


def save_runtime_overrides(payload: dict[str, Any]) -> None:
    write_json_atomic(RUNTIME_OVERRIDES_FILE, payload)


def merge_project_override(project_name: str, updates: dict[str, Any]) -> dict[str, Any]:
    payload = load_runtime_overrides()
    projects = payload.setdefault("projects", {})
    current = projects.get(project_name, {})
    current.update({key: value for key, value in updates.items() if value is not None})
    projects[project_name] = current
    save_runtime_overrides(payload)
    return current

