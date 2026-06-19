from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from project_runtime import discover_project_runtime, load_projects
from runtime_store import RuntimeStatusStore, iso_now
from task_backends import build_task_backend


DEFAULT_INTERVAL_SECONDS = 20


def log(message: str) -> None:
    print(f"[{iso_now()}] {message}", flush=True)


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


@dataclass
class ManagedWorker:
    worker_id: str
    project_name: str
    slot: str
    process: subprocess.Popen
    launch_count: int = 1
    last_launch_at: str | None = None


@dataclass
class ManagedStrategist:
    worker_id: str
    project_name: str
    process: subprocess.Popen | None = None
    launch_count: int = 0
    last_launch_monotonic: float = 0.0


def active_projects(project_names: list[str] | None = None) -> list[str]:
    projects = load_projects()
    active = []
    for project in projects:
        if project_names and project["name"] not in project_names and all(
            query.lower() not in project["name"].lower() for query in project_names
        ):
            continue
        if project["status"] not in ("in_progress", "improving"):
            continue
        if project.get("blocked"):
            continue
        if wait_until_is_future(project.get("wait_until")):
            continue
        active.append(project["name"])
    return active


def selected_projects(project_names: list[str] | None = None) -> list[dict]:
    projects = load_projects()
    if not project_names:
        return projects

    selected: list[dict] = []
    seen: set[str] = set()
    for query in project_names:
        matches = [project for project in projects if project["name"].lower() == query.lower()]
        if not matches:
            matches = [project for project in projects if query.lower() in project["name"].lower()]
        for project in matches:
            if project["name"] in seen:
                continue
            selected.append(project)
            seen.add(project["name"])
    return selected


def base_project_state(project: dict) -> str:
    if project.get("blocked"):
        return "blocked"
    if wait_until_is_future(project.get("wait_until")):
        return "waiting"
    status = project.get("status", "unknown")
    if status == "paused":
        return "paused"
    if status == "complete":
        return "complete"
    if status in ("in_progress", "improving"):
        return "active"
    return status or "inactive"


def build_worker_command(runtime, profile) -> list[str]:
    cmd = [
        sys.executable,
        str((Path(__file__).resolve().parent / "auto_worker_pass.py")),
        runtime.name,
        "--slot",
        profile.slot,
    ]
    if profile.tool:
        cmd += ["--tool", profile.tool]
    if profile.model:
        cmd += ["--model", profile.model]
    if profile.effort:
        cmd += ["--effort", profile.effort]
    return cmd


def build_strategist_command(runtime) -> list[str]:
    cmd = [
        sys.executable,
        str((Path(__file__).resolve().parent / "auto_strategist_pass.py")),
        runtime.name,
    ]
    strategist = runtime.strategist
    if strategist and strategist.tool:
        cmd += ["--tool", strategist.tool]
    if strategist and strategist.model:
        cmd += ["--model", strategist.model]
    if strategist and strategist.effort:
        cmd += ["--effort", strategist.effort]
    return cmd


def strategist_interval_seconds(runtime, queue_depth: int) -> tuple[bool, int, str]:
    strategist = runtime.strategist
    if not strategist:
        return False, 0, "disabled"

    low_queue = queue_depth <= max(1, len(runtime.worker_profiles))
    empty_queue = queue_depth <= 0

    if empty_queue and strategist.trigger_on_empty_queue:
        # Empty-queue refills should happen quickly so projects.json style projects do not stall.
        return True, 60, "scheduled_empty_queue"

    if not strategist.enabled:
        return False, 0, "disabled"

    interval_seconds = max(300, strategist.interval_minutes * 60)
    if low_queue:
        interval_seconds = min(interval_seconds, 300)
        return True, interval_seconds, "scheduled_low_queue"
    return True, interval_seconds, "scheduled"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic AgentLoop runtime supervisor.")
    parser.add_argument("projects", nargs="*", help="Optional project names to limit supervision")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Run one supervisor pass and exit")
    args = parser.parse_args()

    managed: dict[str, ManagedWorker] = {}
    managed_strategists: dict[str, ManagedStrategist] = {}
    status_store = RuntimeStatusStore()
    target_text = ", ".join(args.projects) if args.projects else "all active projects"
    log(f"Runtime supervisor starting for {target_text} (interval {max(5, args.interval_seconds)}s)")

    while True:
        current_selected_projects = selected_projects(args.projects)
        active_project_names = active_projects(args.projects)
        if active_project_names:
            log(f"Checking {len(active_project_names)} runnable project(s): {', '.join(active_project_names)}")
        else:
            log("No runnable projects right now.")

        for project in current_selected_projects:
            project_name = project["name"]
            try:
                runtime = discover_project_runtime(project_name)
                backend = build_task_backend(runtime)
                queue_depth = backend.queue_depth()
            except Exception as exc:
                now = iso_now()
                log(f"{project_name}: supervisor error while loading runtime ({exc})")
                status_store.update_project(
                    project_name,
                    {
                        "project": project_name,
                        "config_status": project.get("status", "unknown"),
                        "blocked": bool(project.get("blocked")),
                        "wait_until": project.get("wait_until"),
                        "queue_depth": 0,
                        "supervisor_pid": os.getpid(),
                        "supervisor_interval_seconds": max(5, args.interval_seconds),
                        "supervisor_checked_at": now,
                        "supervisor_state": "error",
                        "last_error": str(exc),
                        "worker_slots": 0,
                        "busy_slots": 0,
                        "active_slot_processes": 0,
                        "strategist_process_running": False,
                    },
                )
                continue
            project_state = base_project_state(project)
            status_store.update_project(
                project_name,
                {
                    "project": project_name,
                    "config_status": project.get("status", "unknown"),
                    "blocked": bool(project.get("blocked")),
                    "wait_until": project.get("wait_until"),
                    "queue_depth": queue_depth,
                    "supervisor_pid": os.getpid(),
                    "supervisor_interval_seconds": max(5, args.interval_seconds),
                    "supervisor_checked_at": iso_now(),
                    "supervisor_state": project_state,
                    "last_error": None,
                    "worker_slots": len(runtime.worker_profiles),
                    "busy_slots": 0,
                    "active_slot_processes": 0,
                    "strategist_process_running": False,
                },
            )

            if project_state != "active":
                if project_state == "waiting":
                    log(f"{project_name}: waiting until {project.get('wait_until')}")
                elif project_state == "paused":
                    log(f"{project_name}: paused")
                elif project_state == "blocked":
                    log(f"{project_name}: blocked")
                elif project_state == "complete":
                    log(f"{project_name}: complete")
                else:
                    log(f"{project_name}: skipped ({project_state})")
                continue

            log(f"{runtime.name}: queue_depth={queue_depth}")
            active_slot_processes = 0
            busy_slots = 0
            remaining_queue = queue_depth

            for profile in runtime.worker_profiles:
                worker_id = f"{runtime.slug}:{profile.slot}"
                managed_worker = managed.get(worker_id)
                if managed_worker and managed_worker.process.poll() is None:
                    active_slot_processes += 1
                    busy_slots += 1
                    status_store.update_worker(
                        worker_id,
                        {
                            "project": runtime.name,
                            "slot": profile.slot,
                            "tool": profile.tool,
                            "model": profile.model,
                            "queue_depth": queue_depth,
                            "supervisor_state": "running",
                            "supervisor_checked_at": iso_now(),
                        },
                    )
                    continue

                if managed_worker and managed_worker.process.poll() is not None:
                    status_store.update_worker(
                        worker_id,
                        {
                            "last_exit_code": managed_worker.process.returncode,
                            "supervisor_state": "exited",
                            "supervisor_checked_at": iso_now(),
                        },
                    )

                if remaining_queue <= 0:
                    status_store.update_worker(
                        worker_id,
                        {
                            "project": runtime.name,
                            "slot": profile.slot,
                            "tool": profile.tool,
                            "model": profile.model,
                            "queue_depth": queue_depth,
                            "supervisor_state": "idle_no_queue",
                            "supervisor_checked_at": iso_now(),
                        },
                    )
                    log(f"{runtime.name} [{profile.slot}]: idle, no queue")
                    continue

                command = build_worker_command(runtime, profile)
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=str(runtime.project_dir),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception as exc:
                    status_store.update_worker(
                        worker_id,
                        {
                            "project": runtime.name,
                            "slot": profile.slot,
                            "tool": profile.tool,
                            "model": profile.model,
                            "queue_depth": queue_depth,
                            "supervisor_state": "launch_error",
                            "last_error": str(exc),
                            "supervisor_checked_at": iso_now(),
                        },
                    )
                    log(f"{runtime.name} [{profile.slot}]: launch failed ({exc})")
                    continue

                launch_count = managed_worker.launch_count + 1 if managed_worker else 1
                managed[worker_id] = ManagedWorker(
                    worker_id=worker_id,
                    project_name=runtime.name,
                    slot=profile.slot,
                    process=process,
                    launch_count=launch_count,
                    last_launch_at=iso_now(),
                )
                active_slot_processes += 1
                busy_slots += 1
                status_store.update_worker(
                    worker_id,
                    {
                        "project": runtime.name,
                        "slot": profile.slot,
                        "tool": profile.tool,
                        "model": profile.model,
                        "queue_depth": queue_depth,
                        "supervisor_state": "launched",
                        "launch_count": launch_count,
                        "last_error": None,
                        "supervisor_checked_at": iso_now(),
                    },
                )
                log(f"{runtime.name} [{profile.slot}]: launched {profile.tool}{f' ({profile.model})' if profile.model else ''}")
                remaining_queue = max(0, remaining_queue - 1)

            strategist = runtime.strategist
            should_manage_strategist, interval_seconds, scheduled_state = strategist_interval_seconds(runtime, queue_depth)
            strategist_process_running = False
            if strategist and should_manage_strategist:
                strategist_id = f"{runtime.slug}:strategist"
                managed_strategist = managed_strategists.get(strategist_id)
                if managed_strategist and managed_strategist.process and managed_strategist.process.poll() is None:
                    strategist_process_running = True
                    status_store.update_worker(
                        strategist_id,
                        {
                            "project": runtime.name,
                            "slot": "strategist",
                            "role": "strategist",
                            "tool": strategist.tool,
                            "model": strategist.model,
                            "queue_depth": queue_depth,
                            "supervisor_state": "running",
                            "supervisor_checked_at": iso_now(),
                        },
                    )
                else:
                    if managed_strategist is None:
                        managed_strategist = ManagedStrategist(worker_id=strategist_id, project_name=runtime.name)
                        managed_strategists[strategist_id] = managed_strategist

                    if time.monotonic() - managed_strategist.last_launch_monotonic < interval_seconds:
                        status_store.update_worker(
                            strategist_id,
                            {
                                "project": runtime.name,
                                "slot": "strategist",
                                "role": "strategist",
                                "tool": strategist.tool,
                                "model": strategist.model,
                                "queue_depth": queue_depth,
                                "supervisor_state": scheduled_state,
                                "supervisor_checked_at": iso_now(),
                            },
                        )
                        if scheduled_state == "scheduled_empty_queue":
                            log(f"{runtime.name} [strategist]: waiting for empty-queue refill window")
                    else:
                        command = build_strategist_command(runtime)
                        try:
                            process = subprocess.Popen(
                                command,
                                cwd=str(runtime.project_dir),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                        except Exception as exc:
                            status_store.update_worker(
                                strategist_id,
                                {
                                    "project": runtime.name,
                                    "slot": "strategist",
                                    "role": "strategist",
                                    "tool": strategist.tool,
                                    "model": strategist.model,
                                    "queue_depth": queue_depth,
                                    "supervisor_state": "launch_error",
                                    "last_error": str(exc),
                                    "supervisor_checked_at": iso_now(),
                                },
                            )
                            log(f"{runtime.name} [strategist]: launch failed ({exc})")
                        else:
                            managed_strategist.process = process
                            managed_strategist.launch_count += 1
                            managed_strategist.last_launch_monotonic = time.monotonic()
                            strategist_process_running = True
                            status_store.update_worker(
                                strategist_id,
                                {
                                    "project": runtime.name,
                                    "slot": "strategist",
                                    "role": "strategist",
                                    "tool": strategist.tool,
                                    "model": strategist.model,
                                    "queue_depth": queue_depth,
                                    "supervisor_state": "launched",
                                    "launch_count": managed_strategist.launch_count,
                                    "last_error": None,
                                    "supervisor_checked_at": iso_now(),
                                },
                            )
                            reason = "empty queue refill" if scheduled_state == "scheduled_empty_queue" else "scheduled pass"
                            log(f"{runtime.name} [strategist]: launched {strategist.tool}{f' ({strategist.model})' if strategist.model else ''} for {reason}")

            if busy_slots > 0:
                runtime_state = "worker_running"
            elif strategist_process_running and queue_depth <= 0:
                runtime_state = "empty_queue_refill"
            elif queue_depth <= 0:
                runtime_state = "idle_no_queue"
            else:
                runtime_state = "supervising"

            status_store.update_project(
                project_name,
                {
                    "project": project_name,
                    "config_status": project.get("status", "unknown"),
                    "blocked": bool(project.get("blocked")),
                    "wait_until": project.get("wait_until"),
                    "queue_depth": queue_depth,
                    "supervisor_pid": os.getpid(),
                    "supervisor_interval_seconds": max(5, args.interval_seconds),
                    "supervisor_checked_at": iso_now(),
                    "supervisor_state": runtime_state,
                    "last_error": None,
                    "worker_slots": len(runtime.worker_profiles),
                    "busy_slots": busy_slots,
                    "active_slot_processes": active_slot_processes,
                    "strategist_process_running": strategist_process_running,
                },
            )

        if args.once:
            log("Single supervisor pass complete.")
            return 0
        time.sleep(max(5, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
