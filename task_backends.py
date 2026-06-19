from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from project_runtime import ProjectRuntime, load_projects
from runtime_store import file_lock, iso_now, slugify, write_json_atomic


TASK_ID_RE = re.compile(r"\b[A-Za-z]\d+\b")
PROJECT_JSON_STALE_SECONDS = 45 * 60
MARKDOWN_HEARTBEAT_STALE_SECONDS = 15 * 60


@dataclass
class TaskItem:
    task_id: str
    title: str
    description: str
    source: str
    status: str
    owner: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskBackend:
    def __init__(self, runtime: ProjectRuntime):
        self.runtime = runtime

    def queue_depth(self) -> int:
        raise NotImplementedError

    def claim_next(self, worker_name: str) -> TaskItem | None:
        raise NotImplementedError

    def heartbeat(self, worker_name: str, task: TaskItem) -> None:
        return

    def mark_done(self, worker_name: str, task: TaskItem, summary: str) -> None:
        raise NotImplementedError

    def mark_blocked(self, worker_name: str, task: TaskItem, reason: str) -> None:
        raise NotImplementedError


class ProjectJsonTaskBackend(TaskBackend):
    def __init__(self, runtime: ProjectRuntime):
        super().__init__(runtime)
        self.claim_dir = Path(__file__).resolve().parent / "logs" / "runtime_claims"
        self.claim_dir.mkdir(parents=True, exist_ok=True)
        self.stale_lock_seconds = PROJECT_JSON_STALE_SECONDS

    def _lock_path(self) -> Path:
        return self.claim_dir / f"{self.runtime.slug}.lock"

    def _current_project_record(self) -> dict[str, Any]:
        for project in load_projects():
            if project.get("name") == self.runtime.name:
                return project
        return self.runtime.project_record

    def _reap_stale_lock(self) -> bool:
        lock_path = self._lock_path()
        if not lock_path.exists():
            return False
        try:
            age_seconds = time.time() - lock_path.stat().st_mtime
        except OSError:
            return False
        if age_seconds < self.stale_lock_seconds:
            return False
        try:
            lock_path.unlink()
            return True
        except FileNotFoundError:
            return True

    def _task(self) -> TaskItem | None:
        project = self._current_project_record()
        task_text = (project.get("next_task") or "").strip()
        if not task_text:
            return None
        return TaskItem(
            task_id=f"{self.runtime.slug}-next-task",
            title=task_text,
            description=task_text,
            source="projects.json",
            status="unclaimed",
        )

    def queue_depth(self) -> int:
        if self._lock_path().exists() and not self._reap_stale_lock():
            return 0
        return 1 if self._task() else 0

    def claim_next(self, worker_name: str) -> TaskItem | None:
        self._reap_stale_lock()
        task = self._task()
        if not task:
            return None
        try:
            fd = os.open(str(self._lock_path()), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
        except FileExistsError:
            return None
        try:
            os.utime(self._lock_path(), None)
        except OSError:
            pass
        task.owner = worker_name
        task.status = "in_progress"
        return task

    def heartbeat(self, worker_name: str, task: TaskItem) -> None:
        try:
            os.utime(self._lock_path(), None)
        except OSError:
            return

    def mark_done(self, worker_name: str, task: TaskItem, summary: str) -> None:
        try:
            self._lock_path().unlink()
        except FileNotFoundError:
            pass

    def mark_blocked(self, worker_name: str, task: TaskItem, reason: str) -> None:
        try:
            self._lock_path().unlink()
        except FileNotFoundError:
            pass


@dataclass
class ParsedBoardRow:
    line_index: int
    cells: list[str]
    id_index: int
    task_index: int
    status_index: int
    deps_index: int | None

    @property
    def task_id(self) -> str:
        return self.cells[self.id_index].strip()

    @property
    def title(self) -> str:
        return self.cells[self.task_index].strip()

    @property
    def deps(self) -> str:
        if self.deps_index is None:
            return ""
        return self.cells[self.deps_index].strip()

    @property
    def status_text(self) -> str:
        return self.cells[self.status_index].strip()

    def status_kind(self) -> str:
        text = self.status_text.upper()
        if re.fullmatch(r"\[\s*\]", self.status_text):
            return "unclaimed"
        if "DONE" in text:
            return "done"
        if "BLOCK" in text:
            return "blocked"
        if "IN PROGRESS" in text:
            return "in_progress"
        return "other"

    def owner(self) -> str | None:
        text = self.status_text.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        if "IN PROGRESS" in text.upper():
            prefix = re.split(r"IN PROGRESS", text, maxsplit=1, flags=re.IGNORECASE)[0]
            return prefix.rstrip(" -").strip() or None
        if text.startswith("DONE by "):
            return text.replace("DONE by ", "", 1).strip()
        if text.startswith("BLOCKED by "):
            return text.replace("BLOCKED by ", "", 1).strip()
        return None

    def as_task(self) -> TaskItem:
        return TaskItem(
            task_id=self.task_id,
            title=self.title,
            description=self.title,
            source="markdown_board",
            status=self.status_kind(),
            owner=self.owner(),
            metadata={"deps": self.deps},
        )

    def update_status(self, status_text: str) -> str:
        updated = self.cells[:]
        updated[self.status_index] = status_text
        return "| " + " | ".join(updated) + " |"


class MarkdownTaskBoardBackend(TaskBackend):
    def __init__(self, runtime: ProjectRuntime):
        super().__init__(runtime)
        if not runtime.task_board_path:
            raise ValueError(f"{runtime.name} does not have a task board path configured")
        self.board_path = runtime.task_board_path
        self.board_lock = self.board_path.with_suffix(self.board_path.suffix + ".lock")
        tasks_dir = self.board_path.parent
        self.locks_dir = tasks_dir / "locks"
        self.heartbeats_dir = tasks_dir / "heartbeats"
        self.done_dir = tasks_dir
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeats_dir.mkdir(parents=True, exist_ok=True)

    def _parse_board(self, text: str) -> tuple[list[str], list[ParsedBoardRow]]:
        lines = text.splitlines()
        rows: list[ParsedBoardRow] = []
        active_header: list[str] | None = None

        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("|"):
                active_header = None
                continue

            cells = [part.strip() for part in stripped.strip("|").split("|")]
            if any(cell.lower() == "status" for cell in cells) and any(cell.lower() == "id" for cell in cells):
                active_header = cells
                continue

            if active_header is None:
                continue
            if all(set(cell) <= {"-", ":"} for cell in cells):
                continue

            header_lower = [cell.lower() for cell in active_header]
            if "status" not in header_lower or "id" not in header_lower or "task" not in header_lower:
                continue

            id_index = header_lower.index("id")
            task_index = header_lower.index("task")
            status_index = header_lower.index("status")
            deps_index = None
            for candidate in ("deps", "depends", "dependency", "dependencies"):
                if candidate in header_lower:
                    deps_index = header_lower.index(candidate)
                    break
            max_index = max(id_index, task_index, status_index, deps_index or 0)
            if len(cells) <= max_index:
                # Skip malformed rows instead of crashing the whole runtime loop.
                continue

            row = ParsedBoardRow(
                line_index=index,
                cells=cells,
                id_index=id_index,
                task_index=task_index,
                status_index=status_index,
                deps_index=deps_index,
            )
            rows.append(row)

        return lines, rows

    def _load_rows(self) -> tuple[list[str], list[ParsedBoardRow]]:
        return self._parse_board(self.board_path.read_text(encoding="utf-8"))

    def _write_lines(self, lines: list[str]) -> None:
        self.board_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _lock_glob(self, task_id: str) -> list[Path]:
        pattern = f"{task_id}_*.lock"
        return list(self.locks_dir.glob(pattern))

    def _heartbeat_is_stale(self, worker_name: str | None) -> bool:
        if not worker_name:
            return True
        heartbeat_path = self.heartbeats_dir / f"{slugify(worker_name)}.json"
        if not heartbeat_path.exists():
            return True
        try:
            age_seconds = time.time() - heartbeat_path.stat().st_mtime
        except OSError:
            return True
        return age_seconds > MARKDOWN_HEARTBEAT_STALE_SECONDS

    def _recover_stale_claims(self, lines: list[str], rows: list[ParsedBoardRow]) -> bool:
        changed = False
        for row in rows:
            if row.status_kind() != "in_progress":
                continue
            if not self._heartbeat_is_stale(row.owner()):
                continue
            lines[row.line_index] = row.update_status("[ ]")
            self._remove_task_locks(row.task_id)
            changed = True
        return changed

    def _task_claimable(self, row: ParsedBoardRow, rows_by_id: dict[str, ParsedBoardRow]) -> bool:
        if row.status_kind() != "unclaimed":
            return False
        deps = TASK_ID_RE.findall(row.deps)
        for dep in deps:
            dep_row = rows_by_id.get(dep.upper())
            if not dep_row:
                return False
            if dep_row.status_kind() != "done":
                return False
        if self._lock_glob(row.task_id):
            return False
        return True

    def queue_depth(self) -> int:
        with file_lock(self.board_lock):
            lines, rows = self._load_rows()
            if self._recover_stale_claims(lines, rows):
                self._write_lines(lines)
                _, rows = self._load_rows()
            rows_by_id = {row.task_id.upper(): row for row in rows}
            return sum(1 for row in rows if self._task_claimable(row, rows_by_id))

    def claim_next(self, worker_name: str) -> TaskItem | None:
        with file_lock(self.board_lock):
            lines, rows = self._load_rows()
            if self._recover_stale_claims(lines, rows):
                self._write_lines(lines)
                lines, rows = self._load_rows()
            rows_by_id = {row.task_id.upper(): row for row in rows}
            for row in rows:
                if not self._task_claimable(row, rows_by_id):
                    continue
                lock_path = self.locks_dir / f"{row.task_id}_{slugify(worker_name)}.lock"
                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    os.close(fd)
                except FileExistsError:
                    continue

                lines[row.line_index] = row.update_status(f"[{worker_name} - IN PROGRESS]")
                self._write_lines(lines)
                task = row.as_task()
                task.status = "in_progress"
                task.owner = worker_name
                self.heartbeat(worker_name, task)
                return task
        return None

    def _remove_task_locks(self, task_id: str) -> None:
        for lock_path in self._lock_glob(task_id):
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def heartbeat(self, worker_name: str, task: TaskItem) -> None:
        heartbeat_path = self.heartbeats_dir / f"{slugify(worker_name)}.json"
        payload = {
            "worker": worker_name,
            "task_id": task.task_id,
            "project": self.runtime.name,
            "updated_at": iso_now(),
        }
        write_json_atomic(heartbeat_path, payload)

    def mark_done(self, worker_name: str, task: TaskItem, summary: str) -> None:
        with file_lock(self.board_lock):
            lines, rows = self._load_rows()
            for row in rows:
                if row.task_id != task.task_id:
                    continue
                lines[row.line_index] = row.update_status(f"[DONE by {worker_name}]")
                self._write_lines(lines)
                break
        self._remove_task_locks(task.task_id)
        note_path = self.done_dir / f"done_{task.task_id}.md"
        note_path.write_text(f"# {task.task_id}\n\n{summary.strip()}\n", encoding="utf-8")

    def mark_blocked(self, worker_name: str, task: TaskItem, reason: str) -> None:
        with file_lock(self.board_lock):
            lines, rows = self._load_rows()
            for row in rows:
                if row.task_id != task.task_id:
                    continue
                lines[row.line_index] = row.update_status(f"[BLOCKED by {worker_name}]")
                self._write_lines(lines)
                break
        self._remove_task_locks(task.task_id)
        note_path = self.done_dir / f"blocked_{task.task_id}.json"
        write_json_atomic(
            note_path,
            {
                "task_id": task.task_id,
                "worker": worker_name,
                "reason": reason,
                "updated_at": iso_now(),
            },
        )


def build_task_backend(runtime: ProjectRuntime) -> TaskBackend:
    if runtime.task_backend == "markdown_board":
        return MarkdownTaskBoardBackend(runtime)
    if runtime.task_backend == "projects_json":
        return ProjectJsonTaskBackend(runtime)
    raise ValueError(f"Unsupported task backend '{runtime.task_backend}' for {runtime.name}")
