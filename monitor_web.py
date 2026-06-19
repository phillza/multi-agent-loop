"""
AgentLoop Web Monitor.

Default mode is a passive browser dashboard for the new autonomous runtime:
- reads logs/runtime_status.json
- shows worker and strategist state across projects
- shows queue depth, restart counts, heartbeats, and log tails

Use --legacy-workers if you still want the older in-process Claude loop mode.

Requirements:
  pip install fastapi uvicorn

Usage:
  python monitor_web.py
  python monitor_web.py --legacy-workers
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from project_runtime import discover_project_runtime, update_project_record
from runtime_store import LOG_DIR, RUN_EVENTS_FILE, RUNTIME_STATUS_FILE, read_json, slugify
from task_backends import build_task_backend

PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")
AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
LOOP_DELAY = 5
IMPROVE_DELAY = 120
TAIL_LINES = 120
DEFAULT_SUPERVISOR_INTERVAL_SECONDS = 20
_UNSET = object()


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_projects():
    with open(PROJECTS_FILE) as f:
        return json.load(f)


def get_project_dir(project):
    return os.path.join(PROJECTS_BASE, project.get("path", project["name"])).replace("\\", "/")


def find_free_port(start=8600, end=8700):
    for port in range(start, end):
        with socket.socket() as s:
            if s.connect_ex(("localhost", port)) != 0:
                return port
    raise RuntimeError("No free port found")


def extract_json(text: str) -> dict | None:
    """Find last JSON object in text."""
    for line in reversed(text.strip().split("\n")):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return None


def apply_cli_filter(projects: list[dict]) -> list[dict]:
    if not _cli_filter:
        return projects
    try:
        count = int(_cli_filter)
        return projects[:count]
    except ValueError:
        matches = [project for project in projects if _cli_filter.lower() in project["name"].lower()]
        return matches


def load_runtime_status() -> dict:
    return read_json(Path(RUNTIME_STATUS_FILE), {"updated_at": None, "workers": {}, "projects": {}})


def parse_iso_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        normalized = ts.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def wait_until_is_future(wait_until: str | None) -> bool:
    dt = parse_iso_timestamp(wait_until)
    if not dt:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return dt > now


def seconds_since(ts: str | None) -> int | None:
    then = parse_iso_timestamp(ts)
    if not then:
        return None
    now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
    return max(0, int((now - then).total_seconds()))


def heartbeat_age_text(ts: str | None) -> str:
    seconds = seconds_since(ts)
    if seconds is None:
        return ts or ""
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def tail_text(path: Path, lines: int = TAIL_LINES) -> str:
    if not path.exists():
        return "(no log yet)"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(error reading log: {exc})"
    text_lines = raw.splitlines()
    if not text_lines:
        return "(no log yet)"
    return "\n".join(text_lines[-lines:])


def recent_run_events(project_name: str, limit: int = 12) -> list[dict]:
    path = Path(RUN_EVENTS_FILE)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for raw in reversed(lines[-800:]):
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if payload.get("project") != project_name:
            continue
        events.append(
            {
                "recorded_at": payload.get("recorded_at"),
                "pass_type": payload.get("pass_type"),
                "slot": payload.get("slot"),
                "task_id": payload.get("task_id"),
                "blocked": bool(payload.get("blocked")),
                "duration_seconds": payload.get("duration_seconds"),
                "completed": payload.get("completed") or "",
                "error": payload.get("error") or "",
            }
        )
        if len(events) >= limit:
            break
    return events


def runtime_entries_for_project(project_name: str, runtime_status: dict | None = None) -> list[dict]:
    runtime_status = runtime_status or load_runtime_status()
    entries = [
        entry
        for entry in (runtime_status.get("workers") or {}).values()
        if entry.get("project") == project_name
    ]
    return sorted(entries, key=lambda item: (item.get("role") != "strategist", item.get("slot") or ""))


def runtime_project_entry(project_name: str, runtime_status: dict | None = None) -> dict | None:
    runtime_status = runtime_status or load_runtime_status()
    return (runtime_status.get("projects") or {}).get(project_name)


def project_entry_timeout_seconds(project_entry: dict | None) -> int:
    interval = DEFAULT_SUPERVISOR_INTERVAL_SECONDS
    if project_entry:
        try:
            interval = int(project_entry.get("supervisor_interval_seconds") or DEFAULT_SUPERVISOR_INTERVAL_SECONDS)
        except (TypeError, ValueError):
            interval = DEFAULT_SUPERVISOR_INTERVAL_SECONDS
    return max(60, interval * 3)


def project_entry_is_fresh(project_entry: dict | None) -> bool:
    if not project_entry:
        return False
    seconds = seconds_since(project_entry.get("supervisor_checked_at") or project_entry.get("last_updated_at"))
    if seconds is None:
        return False
    return seconds <= project_entry_timeout_seconds(project_entry)


def worker_entry_is_fresh(worker_entry: dict, project_entry: dict | None = None) -> bool:
    seconds = seconds_since(
        worker_entry.get("last_heartbeat_at")
        or worker_entry.get("supervisor_checked_at")
        or worker_entry.get("last_updated_at")
    )
    if seconds is None:
        return False
    return seconds <= max(180, project_entry_timeout_seconds(project_entry))


def worker_counts(entries: list[dict]) -> tuple[int, int]:
    worker_entries = [entry for entry in entries if entry.get("role") != "strategist"]
    busy_slots = sum(
        1
        for entry in worker_entries
        if entry.get("status") == "working"
        or entry.get("supervisor_state") in ("running", "launched")
    )
    return len(worker_entries), busy_slots


def compute_project_health(
    project: dict,
    project_entry: dict | None,
    all_entries: list[dict],
    fresh_entries: list[dict],
    queue_depth: int | None,
    runtime_updated_at: str | None,
) -> tuple[int, list[str]]:
    score = 100
    flags: list[str] = []
    status = project.get("status", "unknown")
    is_active = status in ("in_progress", "improving")

    if project.get("blocked"):
        score -= 45
        flags.append("blocked")
    if wait_until_is_future(project.get("wait_until")):
        score -= 10
        flags.append("waiting")

    if is_active and not project_entry_is_fresh(project_entry):
        score -= 25
        flags.append("supervisor_stale_or_missing")
    if is_active and queue_depth is None:
        score -= 15
        flags.append("queue_depth_unknown")
    if is_active and queue_depth == 0 and not project.get("blocked") and not wait_until_is_future(project.get("wait_until")):
        score -= 12
        flags.append("active_but_empty_queue")

    launch_errors = [
        entry
        for entry in all_entries
        if entry.get("supervisor_state") == "launch_error" or (entry.get("last_error") and "launch" in str(entry.get("last_error")).lower())
    ]
    if launch_errors:
        score -= 20
        flags.append("recent_launch_error")

    stale_worker_entries = [entry for entry in all_entries if not worker_entry_is_fresh(entry, project_entry)]
    if all_entries and stale_worker_entries and not fresh_entries:
        score -= 10
        flags.append("all_worker_slots_stale")

    runtime_age = seconds_since(runtime_updated_at)
    if runtime_age is not None and runtime_age > 1800:
        score -= 8
        flags.append("runtime_status_stale")

    return max(0, min(100, score)), flags


def summarize_runtime_state(project: dict, project_entry: dict | None, fresh_entries: list[dict]) -> dict:
    config_status = project.get("status", "unknown")
    wait_until = project.get("wait_until")

    if project.get("blocked"):
        return {"status_dot": "blocked", "runtime_state": "blocked", "runtime_label": "blocked"}
    if wait_until_is_future(wait_until):
        return {"status_dot": "waiting", "runtime_state": "waiting", "runtime_label": "waiting"}
    if config_status == "paused":
        return {"status_dot": "paused", "runtime_state": "paused", "runtime_label": "paused"}
    if config_status == "complete":
        return {"status_dot": "complete", "runtime_state": "complete", "runtime_label": "complete"}

    if project_entry and project_entry_is_fresh(project_entry):
        state = project_entry.get("supervisor_state") or "supervising"
        if state == "worker_running":
            return {"status_dot": "running", "runtime_state": state, "runtime_label": "worker running"}
        if state == "empty_queue_refill":
            return {"status_dot": "supervising", "runtime_state": state, "runtime_label": "refilling queue"}
        if state == "idle_no_queue":
            return {"status_dot": "idle_no_queue", "runtime_state": state, "runtime_label": "idle, no queue"}
        if state == "supervising":
            return {"status_dot": "supervising", "runtime_state": state, "runtime_label": "supervisor running"}
        if state in ("blocked", "waiting", "paused", "complete"):
            return {"status_dot": state, "runtime_state": state, "runtime_label": state.replace("_", " ")}
        return {"status_dot": "supervising", "runtime_state": state, "runtime_label": state.replace("_", " ")}

    worker_slots, busy_slots = worker_counts(fresh_entries)
    if worker_slots:
        if busy_slots:
            return {
                "status_dot": "running",
                "runtime_state": "worker_only",
                "runtime_label": "worker active, supervisor missing",
            }
        return {
            "status_dot": "idle",
            "runtime_state": "worker_idle_no_supervisor",
            "runtime_label": "worker report only",
        }

    if config_status in ("in_progress", "improving"):
        return {"status_dot": "not_launched", "runtime_state": "not_launched", "runtime_label": "not launched"}
    return {"status_dot": "idle", "runtime_state": "idle", "runtime_label": config_status.replace("_", " ")}


def safe_queue_depth(project_name: str) -> int | None:
    if _monitor_mode != "runtime":
        return None
    try:
        runtime = discover_project_runtime(project_name)
        backend = build_task_backend(runtime)
        return backend.queue_depth()
    except Exception:
        return None


def build_project_list_payload() -> list[dict]:
    projects = apply_cli_filter(load_projects())
    runtime_status = load_runtime_status() if _monitor_mode == "runtime" else {"updated_at": None, "workers": {}, "projects": {}}
    payload = []
    for project in projects:
        project_entry = runtime_project_entry(project["name"], runtime_status)
        all_entries = runtime_entries_for_project(project["name"], runtime_status)
        annotated_entries = [
            {
                **entry,
                "is_fresh": worker_entry_is_fresh(entry, project_entry),
            }
            for entry in all_entries
        ]
        fresh_entries = [entry for entry in annotated_entries if entry.get("is_fresh")]
        queue_depths = [entry.get("queue_depth") for entry in fresh_entries if entry.get("queue_depth") is not None]
        if project_entry and project_entry.get("queue_depth") is not None:
            queue_depth = project_entry.get("queue_depth")
        else:
            queue_depth = max(queue_depths) if queue_depths else safe_queue_depth(project["name"])
        strategist_entries = [entry for entry in fresh_entries if entry.get("role") == "strategist"]
        worker_slot_count, busy_slots = worker_counts(fresh_entries)
        state = summarize_runtime_state(project, project_entry, fresh_entries)
        supervisor_running = project_entry_is_fresh(project_entry)
        health_score, anomaly_flags = compute_project_health(
            project=project,
            project_entry=project_entry,
            all_entries=all_entries,
            fresh_entries=fresh_entries,
            queue_depth=queue_depth,
            runtime_updated_at=runtime_status.get("updated_at"),
        )
        payload.append(
            {
                "name": project["name"],
                "status": state["runtime_state"],
                "status_dot": state["status_dot"],
                "runtime_state": state["runtime_state"],
                "runtime_state_label": state["runtime_label"],
                "project_status": project.get("status", "unknown"),
                "blocked": project.get("blocked", False),
                "wait_until": project.get("wait_until"),
                "next_task": project.get("next_task") or "",
                "has_worker": bool(fresh_entries) if _monitor_mode == "runtime" else project["name"] in workers,
                "supervisor_running": supervisor_running,
                "supervisor_checked_at": project_entry.get("supervisor_checked_at") if project_entry else None,
                "runtime_slots": project_entry.get("worker_slots") if supervisor_running and project_entry else worker_slot_count,
                "strategist_slots": len(strategist_entries),
                "active_slots": project_entry.get("busy_slots") if supervisor_running and project_entry else busy_slots,
                "queue_depth": queue_depth,
                "restart_count": sum(int(entry.get("launch_count") or 0) for entry in all_entries),
                "runtime_updated_at": runtime_status.get("updated_at"),
                "health_score": health_score,
                "anomaly_flags": anomaly_flags,
            }
        )
    return payload


def invalidate_project_list_cache() -> None:
    _project_list_cache["expires_at"] = 0.0
    _project_list_cache["payload"] = None


def get_project_list_payload_cached() -> list[dict]:
    if _monitor_mode != "runtime":
        return build_project_list_payload()
    now = time.monotonic()
    cached_payload = _project_list_cache.get("payload")
    expires_at = float(_project_list_cache.get("expires_at") or 0.0)
    if cached_payload is not None and now < expires_at:
        return cached_payload
    payload = build_project_list_payload()
    _project_list_cache["payload"] = payload
    _project_list_cache["expires_at"] = now + 2.0
    return payload


def build_project_detail_payload(project_name: str) -> dict:
    projects = apply_cli_filter(load_projects())
    project = next((item for item in projects if item["name"] == project_name), None)
    if not project:
        raise KeyError(project_name)

    runtime_status = load_runtime_status() if _monitor_mode == "runtime" else {"updated_at": None, "workers": {}, "projects": {}}
    project_entry = runtime_project_entry(project_name, runtime_status)
    entries = runtime_entries_for_project(project_name, runtime_status)
    workers_payload = []
    for entry in entries:
        worker_id = entry.get("worker_id") or ""
        log_path = Path(LOG_DIR) / f"{slugify(worker_id)}.log"
        workers_payload.append(
            {
                **entry,
                "is_fresh": worker_entry_is_fresh(entry, project_entry),
                "heartbeat_age": heartbeat_age_text(entry.get("last_heartbeat_at")),
                "updated_age": heartbeat_age_text(entry.get("last_updated_at")),
                "log_tail": tail_text(log_path),
                "log_path": str(log_path),
            }
        )

    fresh_entries = [entry for entry in workers_payload if entry.get("is_fresh")]
    queue_depths = [entry.get("queue_depth") for entry in fresh_entries if entry.get("queue_depth") is not None]
    if project_entry and project_entry.get("queue_depth") is not None:
        queue_depth = project_entry.get("queue_depth")
    else:
        queue_depth = max(queue_depths) if queue_depths else safe_queue_depth(project_name)
    state = summarize_runtime_state(project, project_entry, fresh_entries)
    health_score, anomaly_flags = compute_project_health(
        project=project,
        project_entry=project_entry,
        all_entries=entries,
        fresh_entries=fresh_entries,
        queue_depth=queue_depth,
        runtime_updated_at=runtime_status.get("updated_at"),
    )
    return {
        "name": project["name"],
        "project_status": project.get("status", "unknown"),
        "display_status": state["runtime_state"],
        "status_dot": state["status_dot"],
        "runtime_state": state["runtime_state"],
        "runtime_state_label": state["runtime_label"],
        "blocked": project.get("blocked", False),
        "blocker_description": project.get("blocker_description"),
        "wait_until": project.get("wait_until"),
        "next_task": project.get("next_task") or "",
        "completed_task": project.get("completed_task") or "",
        "log": project.get("log", [])[-8:],
        "queue_depth": queue_depth,
        "health_score": health_score,
        "anomaly_flags": anomaly_flags,
        "restart_count": sum(int(entry.get("launch_count") or 0) for entry in entries),
        "runtime_updated_at": runtime_status.get("updated_at"),
        "supervisor_running": project_entry_is_fresh(project_entry),
        "supervisor_checked_at": project_entry.get("supervisor_checked_at") if project_entry else None,
        "supervisor_interval_seconds": project_entry.get("supervisor_interval_seconds") if project_entry else None,
        "worker_slots": project_entry.get("worker_slots") if project_entry else len([entry for entry in fresh_entries if entry.get("role") != "strategist"]),
        "busy_slots": project_entry.get("busy_slots") if project_entry else worker_counts(fresh_entries)[1],
        "strategist_process_running": bool(project_entry.get("strategist_process_running")) if project_entry else False,
        "runtime_workers": workers_payload,
        "recent_pass_events": recent_run_events(project_name),
    }


def set_project_status(
    project_name: str,
    *,
    status: str | None = None,
    blocked: bool | None = None,
    blocker_description: str | None = None,
    wait_until: Any = _UNSET,
) -> bool:
    updates: dict[str, object] = {}
    if status is not None:
        updates["status"] = status
    if blocked is not None:
        updates["blocked"] = blocked
    if blocker_description is not None or blocked is False:
        updates["blocker_description"] = blocker_description
    if wait_until is not _UNSET:
        updates["wait_until"] = wait_until
    try:
        update_project_record(project_name, updates)
    except ValueError:
        return False
    return True


# ── Project worker ──────────────────────────────────────────────────────────────

class ProjectWorker:
    def __init__(self, project: dict):
        self.project = project
        self.name = project["name"]
        self.clients: Set[WebSocket] = set()
        self.status = "idle"
        self.session_id: str | None = None
        self.pending_steer: str | None = None

    def project_dir(self):
        return get_project_dir(self.project)

    def current_task(self) -> str:
        with open(PROJECTS_FILE) as f:
            projects = json.load(f)
        for p in projects:
            if p["name"] == self.name:
                return p.get("next_task") or ""
        return ""

    def build_prompt(self, task: str) -> str:
        return (
            f"You are an autonomous coding agent for project: {self.name}. "
            f"Task: {task}. "
            f"Read CLAUDE.md and AGENTS.md in your project folder first for full context. "
            f"Complete the task thoroughly, then output ONLY this JSON as your final line:\n"
            f'{{"completed": "one line summary", "next_task": "next task description", '
            f'"blocked": false, "blocker_description": null, '
            f'"status": "in_progress", "wait_until": null}}'
        )

    async def broadcast(self, msg: dict):
        dead = set()
        for ws in self.clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    async def set_status(self, status: str, **extra):
        self.status = status
        await self.broadcast({"type": "status", "status": status, **extra})

    async def run_loop(self):
        """Main autonomous loop."""
        while True:
            try:
                with open(PROJECTS_FILE) as f:
                    projects = json.load(f)
                proj = next((p for p in projects if p["name"] == self.name), None)
                if not proj:
                    break

                proj_status = proj.get("status", "paused")

                if proj_status == "paused":
                    await self.set_status("paused")
                    await asyncio.sleep(15)
                    continue

                if proj.get("blocked"):
                    await self.set_status("blocked")
                    await asyncio.sleep(30)
                    continue

                wait_until = proj.get("wait_until")
                if wait_until_is_future(wait_until):
                    await self.set_status("waiting", until=wait_until)
                    await asyncio.sleep(60)
                    continue

                task = self.pending_steer or proj.get("next_task", "")
                self.pending_steer = None

                if not task:
                    await asyncio.sleep(LOOP_DELAY)
                    continue

                await self.run_task(task)
                delay = IMPROVE_DELAY if proj_status == "improving" else LOOP_DELAY
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                break
            except Exception as e:
                await self.broadcast({"type": "error", "text": str(e)})
                await asyncio.sleep(10)

    async def run_task(self, task: str):
        await self.set_status("running")
        await self.broadcast({"type": "task_start", "task": task})

        cmd = [
            "claude", "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--model", "sonnet",
            "--effort", "high",
            "--add-dir", AGENTLOOP_DIR,
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        cmd.append(self.build_prompt(task))

        full_output = []

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.project_dir(),
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            )

            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    await self._handle_event(event, full_output)
                except json.JSONDecodeError:
                    pass  # non-JSON lines (warnings etc) ignored

            await proc.wait()

        except Exception as e:
            await self.broadcast({"type": "error", "text": f"Subprocess error: {e}"})

        # Parse Claude's JSON result and update projects.json
        result_json = extract_json("\n".join(full_output))
        if result_json:
            self._save_result(result_json)

        await self.set_status("idle")

    async def _handle_event(self, event: dict, output_buf: list):
        etype = event.get("type", "")

        if etype == "system":
            self.session_id = event.get("session_id") or self.session_id

        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        output_buf.append(text)
                        await self.broadcast({"type": "claude_text", "text": text})
                elif btype == "tool_use":
                    inp = block.get("input", {})
                    # Show a concise summary of the tool input
                    summary = _tool_summary(block.get("name", ""), inp)
                    await self.broadcast({
                        "type": "tool_call",
                        "tool": block.get("name", ""),
                        "summary": summary,
                    })

        elif etype == "tool_result":
            content = event.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            preview = str(content).strip()[:300]
            await self.broadcast({"type": "tool_result", "preview": preview})

        elif etype == "result":
            self.session_id = event.get("session_id") or self.session_id
            result_text = event.get("result", "")
            output_buf.append(result_text)
            await self.broadcast({
                "type": "task_done",
                "cost": event.get("cost_usd", 0),
                "duration_ms": event.get("duration_ms", 0),
                "tokens_out": (event.get("usage") or {}).get("output_tokens", 0),
            })

    def _save_result(self, result: dict):
        try:
            updates = {key: result[key] for key in ("next_task", "blocked", "blocker_description", "status", "wait_until") if key in result}
            completed = (result.get("completed") or "").strip()
            update_project_record(self.name, updates, log_entry=completed if completed else None)
        except Exception:
            pass


def _tool_summary(tool: str, inp: dict) -> str:
    if tool in ("Read", "Write", "Edit"):
        return inp.get("file_path", "")
    if tool == "Bash":
        cmd = inp.get("command", "")
        return cmd[:120]
    if tool in ("Glob", "Grep"):
        return inp.get("pattern", inp.get("query", ""))
    return json.dumps(inp)[:120]


# ── FastAPI app ─────────────────────────────────────────────────────────────────

app = FastAPI()
workers: dict[str, ProjectWorker] = {}
all_projects: list[dict] = []  # all projects from projects.json
_worker_tasks: dict[str, asyncio.Task] = {}
_cli_filter: str | None = None
_monitor_mode = "runtime"
_project_list_cache: dict[str, Any] = {"expires_at": 0.0, "payload": None}


@app.get("/")
async def index():
    project_list = get_project_list_payload_cached()
    template = RUNTIME_HTML_TEMPLATE if _monitor_mode == "runtime" else LEGACY_HTML_TEMPLATE
    html = template.replace("__PROJECTS__", json.dumps(project_list)).replace("__MODE__", json.dumps(_monitor_mode))
    return HTMLResponse(html)


@app.get("/api/projects")
async def api_projects():
    return get_project_list_payload_cached()


@app.get("/api/project/{project_name:path}")
async def api_project(project_name: str):
    try:
        return build_project_detail_payload(project_name)
    except KeyError:
        return {"ok": False, "error": "Project not found"}


@app.post("/api/start/{project_name:path}")
async def api_start(project_name: str, force: int = 0, unblock: int = 0):
    """Resume a project in runtime mode, or start a legacy in-process worker."""
    projects = load_projects()
    project = next((p for p in projects if p["name"] == project_name), None)
    if not project:
        return {"ok": False, "error": "Project not found"}

    if project.get("status") == "complete" and not force:
        return {"ok": False, "error": "Project is complete. Use force=1 to reopen it."}

    if _monitor_mode == "runtime":
        if not set_project_status(project_name, status="in_progress", wait_until=None):
            return {"ok": False, "error": "Project not found"}
        if unblock:
            set_project_status(project_name, blocked=False, blocker_description=None)
        invalidate_project_list_cache()
        runtime_status = load_runtime_status()
        project_entry = runtime_project_entry(project_name, runtime_status)
        supervisor_running = project_entry_is_fresh(project_entry)
        blocked_note = ""
        if project.get("blocked") and not unblock:
            blocked_note = " Project remains blocked until manually unblocked."
        message = (
            f"Project resumed. A running runtime supervisor will pick it up on the next pass.{blocked_note}"
            if supervisor_running
            else f"Project resumed, but no live runtime supervisor heartbeat was detected. Run python easy_agentloop.py start.{blocked_note}"
        )
        return {
            "ok": True,
            "mode": "runtime",
            "supervisor_running": supervisor_running,
            "message": message,
        }

    if project_name in workers:
        return {"ok": False, "error": "Already running"}

    # Set status to in_progress in projects.json
    set_project_status(project_name, status="in_progress", blocked=False, blocker_description=None, wait_until=None)
    invalidate_project_list_cache()

    w = ProjectWorker(project)
    workers[project_name] = w
    _worker_tasks[project_name] = asyncio.create_task(w.run_loop())
    return {"ok": True}


@app.post("/api/stop/{project_name:path}")
async def api_stop(project_name: str):
    """Pause a project in runtime mode, or stop a legacy in-process worker."""
    if _monitor_mode == "runtime":
        if not set_project_status(project_name, status="paused"):
            return {"ok": False, "error": "Project not found"}
        invalidate_project_list_cache()
        return {
            "ok": True,
            "mode": "runtime",
            "message": "Project paused. Running passes will finish, then the runtime supervisor will stop relaunching it.",
        }

    if project_name not in workers:
        return {"ok": False, "error": "Not running"}

    # Cancel the asyncio task
    task = _worker_tasks.get(project_name)
    if task:
        task.cancel()
        del _worker_tasks[project_name]

    del workers[project_name]

    # Set status to paused in projects.json
    set_project_status(project_name, status="paused")
    invalidate_project_list_cache()

    return {"ok": True}


@app.websocket("/ws/{project_name:path}")
async def ws_endpoint(websocket: WebSocket, project_name: str):
    await websocket.accept()
    if _monitor_mode != "legacy":
        await websocket.send_json({"type": "error", "text": "WebSocket live feed is only available in --legacy-workers mode."})
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return

    worker = workers.get(project_name)
    if not worker:
        # Still accept but just wait — worker might start later
        try:
            while True:
                data = await websocket.receive_text()
                # Check if worker exists now
                worker = workers.get(project_name)
                if worker:
                    break
        except WebSocketDisconnect:
            return

    worker.clients.add(websocket)
    await websocket.send_json({"type": "status", "status": worker.status})

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "steer":
                    text = msg.get("text", "").strip()
                    if text:
                        worker = workers.get(project_name)
                        if worker:
                            worker.pending_steer = text
                            await worker.broadcast({"type": "user_message", "text": text})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        worker = workers.get(project_name)
        if worker:
            worker.clients.discard(websocket)


@app.on_event("startup")
async def on_startup():
    if _monitor_mode != "legacy":
        return

    projects = load_projects()
    active = [
        p for p in projects
        if p["status"] in ("in_progress", "improving")
        and not p.get("blocked")
        and not wait_until_is_future(p.get("wait_until"))
    ]

    # Apply CLI filter if set
    if _cli_filter:
        try:
            count = int(_cli_filter)
            active = active[:count]
        except ValueError:
            matches = [p for p in projects if _cli_filter.lower() in p["name"].lower()]
            if matches:
                active = matches

    for p in active:
        w = ProjectWorker(p)
        workers[p["name"]] = w
        _worker_tasks[p["name"]] = asyncio.create_task(w.run_loop())


# ── HTML ────────────────────────────────────────────────────────────────────────

LEGACY_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>AgentLoop Monitor</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', 'Consolas', sans-serif; background: #0d1117; color: #e6edf3; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

header { background: #161b22; border-bottom: 1px solid #30363d; padding: 14px 20px; display: flex; align-items: center; gap: 20px; flex-shrink: 0; }
header h1 { font-size: 20px; color: #58a6ff; }
#stats { font-size: 14px; color: #8b949e; }

.main { display: flex; flex: 1; overflow: hidden; }

/* Sidebar */
.sidebar { width: 280px; background: #161b22; border-right: 1px solid #30363d; overflow-y: auto; flex-shrink: 0; }
.sidebar-title { padding: 12px 16px; font-size: 12px; text-transform: uppercase; color: #484f58; letter-spacing: 1px; border-bottom: 1px solid #21262d; }
.proj-item { padding: 10px 14px; cursor: pointer; border-bottom: 1px solid #21262d; display: flex; align-items: center; gap: 10px; transition: background 0.1s; }
.proj-item:hover { background: #1c2128; }
.proj-item.active { background: #1f3352; border-left: 3px solid #388bfd; padding-left: 11px; }
.dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.dot.running { background: #3fb950; animation: pulse 1.5s infinite; }
.dot.in_progress { background: #3fb950; }
.dot.idle { background: #30363d; }
.dot.paused { background: #d29922; }
.dot.blocked { background: #f85149; }
.dot.waiting { background: #a371f7; }
.dot.improving { background: #58a6ff; }
.dot.complete { background: #484f58; }
.dot.error { background: #f85149; }
.proj-name { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.proj-status { font-size: 11px; color: #484f58; flex-shrink: 0; }
.proj-btn { font-size: 11px; padding: 2px 8px; border-radius: 4px; border: 1px solid #30363d; cursor: pointer; flex-shrink: 0; }
.proj-btn.start { background: #238636; color: #fff; border-color: #238636; }
.proj-btn.start:hover { background: #2ea043; }
.proj-btn.stop { background: #da3633; color: #fff; border-color: #da3633; }
.proj-btn.stop:hover { background: #f85149; }

/* Feed */
.feed-container { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.feed-header { padding: 12px 20px; background: #161b22; border-bottom: 1px solid #30363d; font-size: 16px; color: #e6edf3; flex-shrink: 0; display: flex; align-items: center; gap: 12px; }
.feed-header .hdr-status { font-size: 13px; color: #8b949e; }
.feed { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 8px; }
.empty-state { flex: 1; display: flex; align-items: center; justify-content: center; color: #484f58; font-size: 16px; }

/* Message bubbles */
.msg { font-size: 15px; line-height: 1.6; border-radius: 6px; padding: 10px 14px; word-break: break-word; }
.msg.claude { background: #161b22; border: 1px solid #30363d; white-space: pre-wrap; }
.msg.tool { background: #0c1929; border-left: 3px solid #388bfd; padding: 8px 12px; color: #79c0ff; font-size: 14px; font-family: 'Consolas', monospace; }
.msg.tool .tool-name { font-weight: bold; margin-right: 8px; color: #58a6ff; }
.msg.tool .tool-args { color: #79c0ff; opacity: 0.8; }
.msg.tool-result { background: #0c1f0c; border-left: 3px solid #2ea043; color: #56d364; font-size: 13px; white-space: pre-wrap; padding: 8px 12px; font-family: 'Consolas', monospace; max-height: 120px; overflow-y: auto; }
.msg.user { background: #1f3352; border: 1px solid #388bfd; color: #a5d6ff; align-self: flex-end; max-width: 80%; font-size: 15px; }
.msg.task-start { text-align: center; color: #8b949e; font-size: 13px; padding: 6px; border-top: 1px solid #21262d; margin-top: 8px; }
.msg.task-done { background: #0c2016; border: 1px solid #2ea043; color: #56d364; font-size: 14px; }
.msg.error { background: #1f0c0c; border: 1px solid #f85149; color: #ffa198; font-size: 14px; }
.msg.system { color: #8b949e; font-size: 13px; font-style: italic; padding: 4px 0; }

/* Input */
.input-area { padding: 12px 20px; background: #161b22; border-top: 1px solid #30363d; display: flex; gap: 10px; flex-shrink: 0; }
.input-area input { flex: 1; background: #1c2128; border: 1px solid #30363d; color: #e6edf3; padding: 10px 14px; border-radius: 6px; font-family: inherit; font-size: 15px; outline: none; }
.input-area input:focus { border-color: #388bfd; box-shadow: 0 0 0 1px #388bfd; }
.input-area button { background: #238636; border: none; color: #fff; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-size: 15px; }
.input-area button:hover { background: #2ea043; }

@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
</style>
</head>
<body>
<header>
  <h1>AgentLoop Monitor</h1>
  <span id="stats"></span>
</header>
<div class="main">
  <div class="sidebar">
    <div class="sidebar-title">Projects</div>
    <div id="proj-list"></div>
  </div>
  <div class="feed-container" id="feed-container">
    <div class="empty-state">Select a project from the sidebar</div>
  </div>
</div>

<script>
const PROJECTS = __PROJECTS__;
const sockets = {};
const buffers = {};
const projData = {};
let active = null;

function sid(name) { return btoa(unescape(encodeURIComponent(name))); }

function buildSidebar() {
  const list = document.getElementById('proj-list');
  list.innerHTML = '';
  PROJECTS.forEach(p => {
    if (!buffers[p.name]) buffers[p.name] = [];
    projData[p.name] = p;
    const item = document.createElement('div');
    item.className = 'proj-item';
    item.id = 'item-' + sid(p.name);

    const dot = document.createElement('div');
    dot.className = 'dot ' + (p.has_worker ? 'running' : p.status);
    dot.id = 'dot-' + sid(p.name);

    const name = document.createElement('span');
    name.className = 'proj-name';
    name.textContent = p.name;

    const lbl = document.createElement('span');
    lbl.className = 'proj-status';
    lbl.id = 'lbl-' + sid(p.name);
    lbl.textContent = p.has_worker ? 'running' : p.status;

    const btn = document.createElement('button');
    btn.className = 'proj-btn ' + (p.has_worker ? 'stop' : 'start');
    btn.textContent = p.has_worker ? 'Stop' : 'Start';
    btn.id = 'btn-' + sid(p.name);
    btn.onclick = (e) => { e.stopPropagation(); toggleWorker(p.name); };

    item.appendChild(dot);
    item.appendChild(name);
    item.appendChild(lbl);
    item.appendChild(btn);
    item.onclick = () => selectProject(p.name);
    list.appendChild(item);

    if (p.has_worker) connectWS(p.name);
  });
  updateStats();
}

async function toggleWorker(name) {
  const p = projData[name];
  const isRunning = p.has_worker;
  const endpoint = isRunning ? '/api/stop/' : '/api/start/';
  const resp = await fetch(endpoint + encodeURIComponent(name), { method: 'POST' });
  const result = await resp.json();
  if (result.ok) {
    p.has_worker = !isRunning;
    p.status = isRunning ? 'paused' : 'in_progress';
    const d = document.getElementById('dot-' + sid(name));
    const l = document.getElementById('lbl-' + sid(name));
    const b = document.getElementById('btn-' + sid(name));
    if (d) d.className = 'dot ' + (p.has_worker ? 'running' : p.status);
    if (l) l.textContent = p.has_worker ? 'running' : p.status;
    if (b) { b.textContent = p.has_worker ? 'Stop' : 'Start'; b.className = 'proj-btn ' + (p.has_worker ? 'stop' : 'start'); }
    if (p.has_worker) connectWS(name);
    updateStats();
  }
}

function selectProject(name) {
  active = name;
  document.querySelectorAll('.proj-item').forEach(el => el.classList.remove('active'));
  document.getElementById('item-' + sid(name))?.classList.add('active');

  const container = document.getElementById('feed-container');
  container.innerHTML = `
    <div class="feed-header">
      <span>${esc(name)}</span>
      <span class="hdr-status" id="hdr-status"></span>
    </div>
    <div class="feed" id="feed"></div>
    <div class="input-area">
      <input id="steer-input" placeholder="Type a message to steer this Claude... (Enter to send)" />
      <button onclick="sendSteer()">Send</button>
    </div>`;

  document.getElementById('steer-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') sendSteer();
  });

  const feed = document.getElementById('feed');
  buffers[name].forEach(html => {
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    if (tmp.firstChild) feed.appendChild(tmp.firstChild);
  });
  feed.scrollTop = feed.scrollHeight;

  connectWS(name);
}

function connectWS(name) {
  if (sockets[name]?.readyState === WebSocket.OPEN) return;
  const ws = new WebSocket('ws://' + location.host + '/ws/' + encodeURIComponent(name));
  sockets[name] = ws;
  ws.onmessage = e => handleMsg(name, JSON.parse(e.data));
  ws.onclose = () => setTimeout(() => connectWS(name), 5000);
}

function handleMsg(name, msg) {
  // Update sidebar dot/label
  if (msg.type === 'status') {
    const d = document.getElementById('dot-' + sid(name));
    const l = document.getElementById('lbl-' + sid(name));
    if (d) d.className = 'dot ' + msg.status;
    if (l) l.textContent = msg.status;
    if (active === name) {
      const hs = document.getElementById('hdr-status');
      if (hs) hs.textContent = msg.status.toUpperCase();
    }
  }

  const el = buildEl(msg);
  if (!el) return;

  buffers[name].push(el.outerHTML);
  if (buffers[name].length > 600) buffers[name].shift();

  if (active === name) {
    const feed = document.getElementById('feed');
    if (feed) {
      feed.appendChild(el);
      feed.scrollTop = feed.scrollHeight;
    }
  }

  updateStats();
}

function buildEl(msg) {
  const d = document.createElement('div');
  d.className = 'msg';

  if (msg.type === 'status') {
    d.classList.add('system');
    d.textContent = '-- ' + msg.status.toUpperCase() + ' --';
  } else if (msg.type === 'task_start') {
    d.classList.add('task-start');
    d.textContent = 'Task: ' + msg.task.substring(0, 200);
  } else if (msg.type === 'claude_text') {
    d.classList.add('claude');
    d.textContent = msg.text;
  } else if (msg.type === 'tool_call') {
    d.classList.add('tool');
    d.innerHTML = '<span class="tool-name">[' + esc(msg.tool) + ']</span><span class="tool-args">' + esc(msg.summary) + '</span>';
  } else if (msg.type === 'tool_result') {
    d.classList.add('tool-result');
    d.textContent = '-> ' + msg.preview;
  } else if (msg.type === 'task_done') {
    d.classList.add('task-done');
    const parts = [];
    if (msg.duration_ms) parts.push((msg.duration_ms/1000).toFixed(1) + 's');
    if (msg.tokens_out) parts.push(msg.tokens_out + ' tokens');
    if (msg.cost) parts.push('$' + msg.cost.toFixed(4));
    d.textContent = 'Done  ' + parts.join('  |  ');
  } else if (msg.type === 'user_message') {
    d.classList.add('user');
    d.textContent = 'You: ' + msg.text;
  } else if (msg.type === 'error') {
    d.classList.add('error');
    d.textContent = 'Error: ' + msg.text;
  } else {
    return null;
  }
  return d;
}

function sendSteer() {
  if (!active) return;
  const input = document.getElementById('steer-input');
  const text = input?.value.trim();
  if (!text) return;
  const ws = sockets[active];
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'steer', text }));
    input.value = '';
  }
}

function updateStats() {
  const running = PROJECTS.filter(p => p.has_worker).length;
  document.getElementById('stats').textContent = running + ' running  |  ' + PROJECTS.length + ' total';
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

buildSidebar();
</script>
</body>
</html>"""


# ── Entry point ─────────────────────────────────────────────────────────────────

RUNTIME_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>AgentLoop Runtime Monitor</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', 'Consolas', sans-serif; background: #0d1117; color: #e6edf3; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
header { background: #161b22; border-bottom: 1px solid #30363d; padding: 14px 20px; display: flex; align-items: center; gap: 18px; flex-shrink: 0; }
header h1 { font-size: 20px; color: #58a6ff; }
#stats, #mode-label { font-size: 14px; color: #8b949e; }
#action-msg { font-size: 13px; color: #79c0ff; }
.main { display: flex; flex: 1; overflow: hidden; }
.sidebar { width: 320px; background: #161b22; border-right: 1px solid #30363d; overflow-y: auto; flex-shrink: 0; }
.sidebar-title { padding: 12px 16px; font-size: 12px; text-transform: uppercase; color: #6e7681; letter-spacing: 1px; border-bottom: 1px solid #21262d; }
.proj-item { padding: 12px 14px; cursor: pointer; border-bottom: 1px solid #21262d; display: flex; align-items: flex-start; gap: 10px; transition: background 0.1s; }
.proj-item:hover { background: #1c2128; }
.proj-item.active { background: #1f3352; border-left: 3px solid #388bfd; padding-left: 11px; }
.dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; margin-top: 4px; }
.dot.running, .dot.in_progress { background: #3fb950; }
.dot.improving { background: #58a6ff; }
.dot.supervising, .dot.empty_queue_refill { background: #58a6ff; }
.dot.paused { background: #d29922; }
.dot.blocked { background: #f85149; }
.dot.waiting { background: #a371f7; }
.dot.complete { background: #6e7681; }
.dot.idle, .dot.unknown, .dot.idle_no_queue, .dot.not_launched { background: #30363d; }
.proj-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.proj-name { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.proj-meta { font-size: 11px; color: #8b949e; line-height: 1.4; }
.proj-btn { font-size: 11px; padding: 4px 8px; border-radius: 4px; border: 1px solid #30363d; cursor: pointer; flex-shrink: 0; margin-top: 2px; }
.proj-btn.start { background: #238636; color: #fff; border-color: #238636; }
.proj-btn.start:hover { background: #2ea043; }
.proj-btn.stop { background: #da3633; color: #fff; border-color: #da3633; }
.proj-btn.stop:hover { background: #f85149; }
.proj-btn.disabled { background: #21262d; color: #6e7681; border-color: #30363d; cursor: default; }
.content { flex: 1; overflow-y: auto; }
.empty-state { height: 100%; display: flex; align-items: center; justify-content: center; color: #6e7681; font-size: 16px; }
.detail { padding: 18px 20px 28px; display: flex; flex-direction: column; gap: 16px; }
.detail-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.detail-title { font-size: 22px; }
.detail-subtitle { font-size: 13px; color: #8b949e; margin-top: 4px; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.summary-card, .worker-card, .note-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 14px; }
.label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px; color: #8b949e; margin-bottom: 6px; }
.value { font-size: 18px; color: #e6edf3; }
.worker-list { display: flex; flex-direction: column; gap: 12px; }
.worker-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.worker-slot { font-size: 16px; color: #58a6ff; font-weight: 600; }
.pill { font-size: 11px; padding: 3px 7px; border-radius: 999px; background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
.pill.blocked { background: #351717; color: #ffa198; border-color: #f85149; }
.pill.working, .pill.running { background: #0f2418; color: #56d364; border-color: #2ea043; }
.pill.stale { background: #332701; color: #ffd866; border-color: #d29922; }
.worker-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-bottom: 10px; }
.worker-value { font-size: 13px; color: #e6edf3; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.log-box { margin-top: 10px; background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 10px; font-size: 12px; line-height: 1.45; overflow-x: auto; max-height: 260px; white-space: pre-wrap; color: #c9d1d9; }
.list { display: flex; flex-direction: column; gap: 8px; }
.list-item { padding-left: 14px; position: relative; color: #c9d1d9; line-height: 1.5; }
.list-item::before { content: '-'; position: absolute; left: 0; color: #8b949e; }
.top-actions { display: flex; gap: 8px; }
.top-actions button { background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 8px 12px; border-radius: 6px; cursor: pointer; }
.top-actions button:hover { background: #30363d; }
</style>
</head>
<body>
<header>
  <h1>AgentLoop Runtime Monitor</h1>
  <span id="mode-label"></span>
  <span id="stats"></span>
  <span id="action-msg"></span>
</header>
<div class="main">
  <div class="sidebar">
    <div class="sidebar-title">Projects</div>
    <div id="proj-list"></div>
  </div>
  <div class="content" id="content">
    <div class="empty-state">Select a project from the sidebar</div>
  </div>
</div>

<script>
const MODE = __MODE__;
let PROJECTS = __PROJECTS__;
let active = null;

function sid(name) { return btoa(unescape(encodeURIComponent(name))); }

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function actionSpec(project) {
  if (project.project_status === 'complete') {
    return { label: 'Complete', action: null, klass: 'disabled' };
  }
  const paused = project.project_status === 'paused';
  return {
    label: paused ? 'Resume' : 'Pause',
    action: paused ? 'start' : 'stop',
    klass: paused ? 'start' : 'stop',
  };
}

function showActionMessage(text, ttlMs = 6000) {
  const el = document.getElementById('action-msg');
  if (!el) return;
  el.textContent = text || '';
  if (!text) return;
  setTimeout(() => {
    if (el.textContent === text) {
      el.textContent = '';
    }
  }, ttlMs);
}

function buildSidebar() {
  const list = document.getElementById('proj-list');
  list.innerHTML = '';
  PROJECTS.forEach(project => {
    const item = document.createElement('div');
    item.className = 'proj-item' + (active === project.name ? ' active' : '');

    const dot = document.createElement('div');
    dot.className = 'dot ' + (project.status_dot || project.status || 'idle');

    const main = document.createElement('div');
    main.className = 'proj-main';

    const name = document.createElement('div');
    name.className = 'proj-name';
    name.textContent = project.name;

    const meta = document.createElement('div');
    meta.className = 'proj-meta';
    const metaBits = [];
    metaBits.push('config ' + project.project_status);
    metaBits.push('runtime ' + (project.runtime_state_label || project.runtime_state || 'unknown'));
    metaBits.push((project.active_slots || 0) + '/' + (project.runtime_slots || 0) + ' busy');
    if (project.health_score !== null && project.health_score !== undefined) metaBits.push('health ' + project.health_score);
    if (project.strategist_slots) metaBits.push(project.strategist_slots + ' strategist');
    if (project.queue_depth !== null && project.queue_depth !== undefined) metaBits.push('queue ' + project.queue_depth);
    if (project.anomaly_flags && project.anomaly_flags.length) metaBits.push(project.anomaly_flags.length + ' anomalies');
    if (project.restart_count) metaBits.push('restarts ' + project.restart_count);
    meta.textContent = metaBits.join(' | ');

    main.appendChild(name);
    main.appendChild(meta);

    const spec = actionSpec(project);
    const btn = document.createElement('button');
    btn.className = 'proj-btn ' + spec.klass;
    btn.textContent = spec.label;
    btn.disabled = !spec.action;
    btn.onclick = async (event) => {
      event.stopPropagation();
      if (!spec.action) return;
      await toggleProject(project.name, spec.action);
    };

    item.appendChild(dot);
    item.appendChild(main);
    item.appendChild(btn);
    item.onclick = () => selectProject(project.name);
    list.appendChild(item);
  });
  updateStats();
}

async function toggleProject(name, action) {
  const resp = await fetch('/api/' + action + '/' + encodeURIComponent(name), { method: 'POST' });
  const result = await resp.json();
  if (!result.ok) {
    showActionMessage(result.error || 'Request failed');
    return;
  }
  if (result.message) showActionMessage(result.message);
  await refreshProjects();
  if (active === name) {
    await refreshDetail();
  }
}

function selectProject(name) {
  active = name;
  buildSidebar();
  refreshDetail();
}

async function refreshProjects() {
  const resp = await fetch('/api/projects');
  PROJECTS = await resp.json();
  if (!active && PROJECTS.length) {
    active = PROJECTS[0].name;
  }
  buildSidebar();
}

function renderList(items) {
  if (!items || !items.length) {
    return '<div class="worker-value">None</div>';
  }
  return '<div class="list">' + items.map(item => '<div class="list-item">' + esc(item) + '</div>').join('') + '</div>';
}

function renderPassEvents(events) {
  if (!events || !events.length) {
    return '<div class="worker-value">No pass events yet.</div>';
  }
  return '<div class="list">' + events.map(evt => {
    const when = evt.recorded_at ? esc(evt.recorded_at) : '';
    const passType = evt.pass_type ? esc(evt.pass_type) : '';
    const slot = evt.slot ? esc(evt.slot) : '';
    const task = evt.task_id ? esc(evt.task_id) : '';
    const duration = evt.duration_seconds !== undefined && evt.duration_seconds !== null ? esc(String(evt.duration_seconds) + 's') : '';
    const status = evt.blocked ? 'blocked' : 'ok';
    const summary = evt.error || evt.completed || '';
    const bits = [when, passType, slot, task, duration, status].filter(Boolean).join(' | ');
    return '<div class="list-item"><strong>' + bits + '</strong><br>' + esc(summary) + '</div>';
  }).join('') + '</div>';
}

function renderWorker(worker) {
  const pills = [];
  pills.push('<span class="pill ' + (worker.is_fresh ? '' : 'stale') + '">' + (worker.is_fresh ? 'fresh' : 'stale') + '</span>');
  if (worker.role) pills.push('<span class="pill">' + esc(worker.role) + '</span>');
  if (worker.tool) pills.push('<span class="pill">' + esc(worker.tool) + '</span>');
  if (worker.model) pills.push('<span class="pill">' + esc(worker.model) + '</span>');
  if (worker.status) pills.push('<span class="pill ' + esc(worker.status) + '">' + esc(worker.status) + '</span>');
  if (worker.supervisor_state) pills.push('<span class="pill">' + esc(worker.supervisor_state) + '</span>');
  return [
    '<div class="worker-card">',
      '<div class="worker-header">',
        '<div class="worker-slot">' + esc(worker.slot || worker.worker_id || 'worker') + '</div>',
        pills.join(''),
      '</div>',
      '<div class="worker-grid">',
        '<div><div class="label">Current task</div><div class="worker-value">' + esc(worker.current_task || '') + '</div></div>',
        '<div><div class="label">Task ID</div><div class="worker-value">' + esc(worker.task_id || '') + '</div></div>',
        '<div><div class="label">Queue depth</div><div class="worker-value">' + esc(worker.queue_depth ?? '') + '</div></div>',
        '<div><div class="label">Launch count</div><div class="worker-value">' + esc(worker.launch_count ?? '') + '</div></div>',
        '<div><div class="label">Heartbeat</div><div class="worker-value">' + esc(worker.heartbeat_age || '') + '</div></div>',
        '<div><div class="label">Updated</div><div class="worker-value">' + esc(worker.updated_age || worker.last_updated_at || '') + '</div></div>',
      '</div>',
      '<div class="worker-grid">',
        '<div><div class="label">Last result</div><div class="worker-value">' + esc(worker.last_result || '') + '</div></div>',
        '<div><div class="label">Last error</div><div class="worker-value">' + esc(worker.last_error || '') + '</div></div>',
        '<div><div class="label">Started</div><div class="worker-value">' + esc(worker.started_at || '') + '</div></div>',
        '<div><div class="label">Finished</div><div class="worker-value">' + esc(worker.finished_at || '') + '</div></div>',
        '<div><div class="label">Log path</div><div class="worker-value">' + esc(worker.log_path || '') + '</div></div>',
      '</div>',
      '<div class="label">Recent log</div>',
      '<pre class="log-box">' + esc(worker.log_tail || '(no log yet)') + '</pre>',
    '</div>'
  ].join('');
}

function renderDetail(detail) {
  const content = document.getElementById('content');
  const workersHtml = detail.runtime_workers && detail.runtime_workers.length
    ? '<div class="worker-list">' + detail.runtime_workers.map(renderWorker).join('') + '</div>'
    : '<div class="note-card"><div class="label">Runtime workers</div><div class="worker-value">No runtime slot records yet for this project.</div></div>';

  content.innerHTML = [
    '<div class="detail">',
      '<div class="detail-header">',
        '<div>',
          '<div class="detail-title">' + esc(detail.name) + '</div>',
          '<div class="detail-subtitle">Runtime: ' + esc(detail.runtime_state_label || detail.display_status) + ' | Config: ' + esc(detail.project_status) + '</div>',
        '</div>',
        '<div class="top-actions"><button onclick="refreshDetail()">Refresh now</button></div>',
      '</div>',
      '<div class="summary-grid">',
        '<div class="summary-card"><div class="label">Runtime state</div><div class="value">' + esc(detail.runtime_state_label || detail.display_status || '') + '</div></div>',
        '<div class="summary-card"><div class="label">Supervisor</div><div class="worker-value">' + esc(detail.supervisor_running ? 'live' : 'not live') + (detail.supervisor_checked_at ? '<br>' + esc(detail.supervisor_checked_at) : '') + '</div></div>',
        '<div class="summary-card"><div class="label">Health score</div><div class="value">' + esc(detail.health_score ?? '') + '</div></div>',
        '<div class="summary-card"><div class="label">Queue depth</div><div class="value">' + esc(detail.queue_depth ?? '') + '</div></div>',
        '<div class="summary-card"><div class="label">Busy slots</div><div class="value">' + esc((detail.busy_slots ?? 0) + '/' + (detail.worker_slots ?? 0)) + '</div></div>',
        '<div class="summary-card"><div class="label">Next task</div><div class="worker-value">' + esc(detail.next_task || '') + '</div></div>',
        '<div class="summary-card"><div class="label">Wait until</div><div class="worker-value">' + esc(detail.wait_until || '') + '</div></div>',
      '</div>',
      '<div class="note-card"><div class="label">Anomaly flags</div>' + renderList(detail.anomaly_flags || []) + '</div>',
      '<div class="note-card"><div class="label">Recent pass events</div>' + renderPassEvents(detail.recent_pass_events || []) + '</div>',
      '<div class="note-card"><div class="label">Restarts</div><div class="worker-value">' + esc(detail.restart_count ?? 0) + '</div></div>',
      '<div class="note-card"><div class="label">Current blocker</div><div class="worker-value">' + esc(detail.blocker_description || '') + '</div></div>',
      '<div class="note-card"><div class="label">Recent project log</div>' + renderList(detail.log || []) + '</div>',
      '<div class="note-card"><div class="label">Runtime status updated</div><div class="worker-value">' + esc(detail.runtime_updated_at || '') + '</div></div>',
      workersHtml,
    '</div>'
  ].join('');
}

async function refreshDetail() {
  if (!active) {
    document.getElementById('content').innerHTML = '<div class="empty-state">Select a project from the sidebar</div>';
    return;
  }
  const resp = await fetch('/api/project/' + encodeURIComponent(active));
  const detail = await resp.json();
  if (detail.ok === false) {
    document.getElementById('content').innerHTML = '<div class="empty-state">' + esc(detail.error || 'Project not found') + '</div>';
    return;
  }
  renderDetail(detail);
}

function updateStats() {
  const configActive = PROJECTS.filter(project => ['in_progress', 'improving'].includes(project.project_status)).length;
  const supervised = PROJECTS.filter(project => project.supervisor_running).length;
  const busySlots = PROJECTS.reduce((sum, project) => sum + (project.active_slots || 0), 0);
  const waiting = PROJECTS.filter(project => project.runtime_state === 'waiting').length;
  const blocked = PROJECTS.filter(project => project.blocked || project.status === 'blocked').length;
  const unhealthy = PROJECTS.filter(project => (project.health_score ?? 100) < 70).length;
  document.getElementById('mode-label').textContent = 'Passive runtime status view';
  document.getElementById('stats').textContent = configActive + ' config-active | ' + supervised + ' supervised | ' + busySlots + ' busy slots | ' + waiting + ' waiting | ' + blocked + ' blocked | ' + unhealthy + ' unhealthy';
}

async function boot() {
  await refreshProjects();
  if (active) {
    await refreshDetail();
  }
  setInterval(async () => {
    await refreshProjects();
    if (active) {
      await refreshDetail();
    }
  }, 5000);
}

boot();
</script>
</body>
</html>"""


def main():
    try:
        import fastapi  # noqa
        import uvicorn  # noqa
    except ImportError:
        print("Missing dependencies. Install with:")
        print("  pip install fastapi uvicorn")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="AgentLoop browser dashboard.")
    parser.add_argument("filter", nargs="?", help="Optional project count or name filter")
    parser.add_argument("--legacy-workers", action="store_true", help="Start the older in-process Claude worker loops instead of passive runtime monitoring")
    args = parser.parse_args()

    projects = load_projects()
    active_count = sum(
        1 for p in projects
        if p["status"] in ("in_progress", "improving")
        and not p.get("blocked")
        and not wait_until_is_future(p.get("wait_until"))
    )

    global _cli_filter, _monitor_mode
    _cli_filter = args.filter
    _monitor_mode = "legacy" if args.legacy_workers else "runtime"

    port = find_free_port()
    url = f"http://localhost:{port}"
    print("\nAgentLoop Web Monitor")
    if _monitor_mode == "legacy":
        print("Mode: legacy in-process workers")
        print(f"Starting {active_count} worker(s)...")
    else:
        runtime_status = load_runtime_status()
        runtime_slots = len((runtime_status.get('workers') or {}).values())
        print("Mode: passive runtime status view")
        print(f"Watching {active_count} active project(s) and {runtime_slots} runtime slot record(s)...")
    print(f"Dashboard: {url}\n")

    webbrowser.open(url)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
