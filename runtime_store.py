from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


AGENTLOOP_DIR = Path(__file__).resolve().parent
LOG_DIR = AGENTLOOP_DIR / "logs"
RUNTIME_STATUS_FILE = LOG_DIR / "runtime_status.json"
RUN_EVENTS_FILE = LOG_DIR / "run_events.jsonl"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def slugify(value: str) -> str:
    chars = []
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
        else:
            chars.append("-")
    text = "".join(chars).strip("-")
    while "--" in text:
        text = text.replace("--", "-")
    return text or "item"


@contextmanager
def file_lock(lock_path: Path, timeout_seconds: float = 10.0, poll_seconds: float = 0.1) -> Iterator[None]:
    ensure_dir(lock_path.parent)
    deadline = time.time() + timeout_seconds
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_path}")
            time.sleep(poll_seconds)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    fd, temp_path = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def append_run_event(event: dict[str, Any]) -> None:
    ensure_dir(RUN_EVENTS_FILE.parent)
    lock_path = RUN_EVENTS_FILE.with_suffix(".lock")
    record = dict(event)
    record.setdefault("recorded_at", iso_now())
    line = json.dumps(record, ensure_ascii=True)
    with file_lock(lock_path):
        with RUN_EVENTS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


class RuntimeStatusStore:
    def __init__(self, path: Path | None = None):
        self.path = path or RUNTIME_STATUS_FILE
        self.lock_path = self.path.with_suffix(".lock")

    def load(self) -> dict[str, Any]:
        return read_json(self.path, {"updated_at": None, "workers": {}, "projects": {}})

    def update_worker(self, worker_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with file_lock(self.lock_path):
            payload = self.load()
            workers = payload.setdefault("workers", {})
            current = workers.get(worker_id, {})
            current.update(updates)
            current["worker_id"] = worker_id
            current["last_updated_at"] = iso_now()
            workers[worker_id] = current
            payload["updated_at"] = current["last_updated_at"]
            write_json_atomic(self.path, payload)
            return current

    def remove_worker(self, worker_id: str) -> None:
        with file_lock(self.lock_path):
            payload = self.load()
            payload.setdefault("workers", {}).pop(worker_id, None)
            payload["updated_at"] = iso_now()
            write_json_atomic(self.path, payload)

    def heartbeat(self, worker_id: str, **updates: Any) -> dict[str, Any]:
        updates["last_heartbeat_at"] = iso_now()
        return self.update_worker(worker_id, updates)

    def update_project(self, project_name: str, updates: dict[str, Any]) -> dict[str, Any]:
        with file_lock(self.lock_path):
            payload = self.load()
            projects = payload.setdefault("projects", {})
            current = projects.get(project_name, {})
            current.update(updates)
            current["project"] = project_name
            current["last_updated_at"] = iso_now()
            projects[project_name] = current
            payload["updated_at"] = current["last_updated_at"]
            write_json_atomic(self.path, payload)
            return current

    def remove_project(self, project_name: str) -> None:
        with file_lock(self.lock_path):
            payload = self.load()
            payload.setdefault("projects", {}).pop(project_name, None)
            payload["updated_at"] = iso_now()
            write_json_atomic(self.path, payload)
