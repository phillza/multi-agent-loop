from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from project_runtime import AGENTLOOP_DIR, PROJECTS_FILE, WorkerProfile, discover_project_runtime, update_project_record
from runtime_store import LOG_DIR, RuntimeStatusStore, append_run_event, iso_now, slugify
from task_backends import TaskItem, build_task_backend


DEFAULT_TIMEOUT_SECONDS = 1800
HEARTBEAT_SECONDS = 30


def resolve_windows_command(executable: str) -> str:
    appdata_cmd = Path(os.environ.get("APPDATA", "")) / "npm" / f"{executable}.cmd"
    if appdata_cmd.exists():
        return str(appdata_cmd)
    for suffix in (".cmd", ".exe"):
        resolved = shutil.which(f"{executable}{suffix}")
        if resolved:
            return resolved
    return shutil.which(executable) or executable


def extract_json(raw_text: str) -> dict[str, Any] | None:
    if not raw_text.strip():
        return None
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    end = raw_text.rfind("}")
    if end == -1:
        return None
    depth = 0
    start = end
    while start >= 0:
        char = raw_text[start]
        if char == "}":
            depth += 1
        elif char == "{":
            depth -= 1
            if depth == 0:
                break
        start -= 1
    if start < 0:
        return None
    try:
        return json.loads(raw_text[start : end + 1])
    except json.JSONDecodeError:
        return None


def load_role(runtime, slot: str | None) -> WorkerProfile:
    if slot:
        match = next((profile for profile in runtime.worker_profiles if profile.slot == slot), None)
        if not match:
            available = ", ".join(profile.slot for profile in runtime.worker_profiles)
            raise ValueError(f"Unknown slot '{slot}'. Available: {available}")
        return match
    return runtime.worker_profiles[0]


def build_prompt(runtime, task: TaskItem, worker_name: str) -> str:
    docs = runtime.docs or []
    docs_text = "\n".join(f"- {path}" for path in docs) if docs else "- No repo docs were auto-discovered. Inspect the codebase directly."
    backend_note = (
        "Do not edit TASK_BOARD.md status fields or task lock/heartbeat files yourself. "
        "The runtime already claimed this task and will mark it done or blocked for you."
        if runtime.task_backend == "markdown_board"
        else f"Do not manually edit {PROJECTS_FILE} to record completion. Return the final JSON and the runtime will persist it."
    )

    return f"""You are an autonomous implementation worker.

Project: {runtime.name}
Worker: {worker_name}
Working directory: {runtime.project_dir}
Claimed task ID: {task.task_id}
Claimed task: {task.description}
Task backend: {runtime.task_backend}

Read these docs first if they exist:
{docs_text}

Rules:
- {backend_note}
- Work only on the claimed task for this pass.
- Run relevant verification before finishing.
- If the task is already complete or invalid, explain that in the final JSON instead of touching unrelated work.
- Keep your final response concise and make the FINAL line a single JSON object.

FINAL JSON schema:
{{
  "completed": "one sentence summary",
  "next_task": "specific next task or empty string",
  "status": "in_progress",
  "blocked": false,
  "blocker_description": null,
  "wait_until": null,
  "tests_ran": ["command 1", "command 2"]
}}
"""


def build_command(tool: str, model: str | None, effort: str | None, prompt: str, project_dir: Path) -> tuple[list[str], Path | None]:
    tool = tool.lower()
    if tool == "claude":
        cmd = [
            resolve_windows_command("claude"),
            "-p",
            "--dangerously-skip-permissions",
            "--output-format",
            "text",
            "--add-dir",
            str(AGENTLOOP_DIR),
        ]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        cmd.append(prompt)
        return cmd, None

    if tool == "codex":
        last_message_file = Path(tempfile.gettempdir()) / f"agentloop_codex_last_{int(time.time() * 1000)}.txt"
        cmd = [
            resolve_windows_command("codex"),
            "exec",
            "--color",
            "never",
            "--ask-for-approval",
            "never",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
            "--add-dir",
            str(AGENTLOOP_DIR),
            "--output-last-message",
            str(last_message_file),
        ]
        if model:
            cmd += ["--model", model]
        cmd.append(prompt)
        return cmd, last_message_file

    if tool == "opencode":
        cmd = [
            resolve_windows_command("opencode"),
            "run",
            "--dir",
            str(project_dir),
            "--format",
            "default",
        ]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--variant", effort]
        cmd.append(prompt)
        return cmd, None

    raise ValueError(f"Unsupported tool '{tool}'")


def default_failure_result(reason: str) -> dict[str, Any]:
    return {
        "completed": "",
        "next_task": "",
        "status": "in_progress",
        "blocked": True,
        "blocker_description": reason,
        "wait_until": None,
        "tests_ran": [],
    }


def run_with_heartbeat(
    cmd: list[str],
    cwd: Path,
    status_store: RuntimeStatusStore,
    worker_id: str,
    backend,
    worker_name: str,
    task: TaskItem,
    timeout_seconds: int,
    last_message_file: Path | None,
) -> tuple[int, str]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{slugify(worker_id)}.log"
    output_lines: list[str] = []
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    status_store.update_worker(worker_id, {"pid": proc.pid, "started_at": iso_now()})

    def reader() -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {iso_now()} {worker_id} ===\n")
            if proc.stdout is None:
                return
            for line in proc.stdout:
                output_lines.append(line)
                handle.write(line)
                handle.flush()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    started = time.time()
    timed_out = False
    while proc.poll() is None:
        if time.time() - started > timeout_seconds:
            timed_out = True
            proc.kill()
            break
        backend.heartbeat(worker_name, task)
        status_store.heartbeat(
            worker_id,
            status="working",
            task_id=task.task_id,
            task_title=task.title,
            current_task=task.description,
        )
        time.sleep(HEARTBEAT_SECONDS)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    thread.join(timeout=10)
    output_text = "".join(output_lines)
    if last_message_file and last_message_file.exists():
        try:
            output_text = output_text + "\n" + last_message_file.read_text(encoding="utf-8")
        except OSError:
            pass
    if timed_out:
        output_text += "\n" + json.dumps(default_failure_result(f"Worker timed out after {timeout_seconds} seconds"))
    return proc.returncode or 0, output_text


def persist_result(runtime, task: TaskItem, result: dict[str, Any]) -> None:
    summary = (result.get("completed") or "").strip()
    blocked = bool(result.get("blocked"))
    updates: dict[str, Any] = {
        "blocked": blocked,
        "blocker_description": result.get("blocker_description") if blocked else None,
    }

    if "next_task" in result:
        updates["next_task"] = result.get("next_task") or ""
    if result.get("status"):
        updates["status"] = result["status"]
    elif blocked:
        updates["status"] = "blocked"
    if "wait_until" in result:
        updates["wait_until"] = result.get("wait_until")

    log_entry = None
    if summary:
        log_entry = f"{task.task_id}: {summary}"
    update_project_record(runtime.name, updates, log_entry=log_entry)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one autonomous worker pass for a project.")
    parser.add_argument("project", help="Project name (partial match ok)")
    parser.add_argument("--slot", default=None, help="Worker slot/profile to use")
    parser.add_argument("--tool", default=None, help="Override CLI tool")
    parser.add_argument("--model", default=None, help="Override model")
    parser.add_argument("--effort", default=None, help="Override effort/variant")
    parser.add_argument("--worker-name", default=None, help="Stable worker name for locks/status")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    runtime = discover_project_runtime(args.project)
    profile = load_role(runtime, args.slot)
    tool = args.tool or profile.tool
    model = args.model or profile.model
    effort = args.effort or profile.effort
    worker_name = args.worker_name or f"{runtime.slug}-{profile.slot}"
    worker_id = f"{runtime.slug}:{profile.slot}"
    status_store = RuntimeStatusStore()
    backend = build_task_backend(runtime)

    status_store.update_worker(
        worker_id,
        {
            "project": runtime.name,
            "slot": profile.slot,
            "role": profile.role,
            "tool": tool,
            "model": model,
            "status": "claiming",
            "worker_name": worker_name,
            "task_backend": runtime.task_backend,
        },
    )

    task = backend.claim_next(worker_name)
    if not task:
        status_store.update_worker(worker_id, {"status": "idle", "current_task": None, "task_id": None})
        return 0

    pass_started = time.monotonic()

    try:
        prompt = build_prompt(runtime, task, worker_name)
        cmd, last_message_file = build_command(tool, model, effort, prompt, runtime.project_dir)

        status_store.update_worker(
            worker_id,
            {
                "status": "working",
                "task_id": task.task_id,
                "task_title": task.title,
                "current_task": task.description,
                "started_at": iso_now(),
            },
        )

        return_code, output_text = run_with_heartbeat(
            cmd=cmd,
            cwd=runtime.project_dir,
            status_store=status_store,
            worker_id=worker_id,
            backend=backend,
            worker_name=worker_name,
            task=task,
            timeout_seconds=args.timeout_seconds,
            last_message_file=last_message_file,
        )

        result = extract_json(output_text)
        if not result:
            result = default_failure_result("Worker did not return the required final JSON result.")
        persist_result(runtime, task, result)

        if result.get("blocked"):
            backend.mark_blocked(worker_name, task, result.get("blocker_description") or "No blocker provided")
            status_store.update_worker(
                worker_id,
                {
                    "status": "blocked",
                    "last_result": result.get("completed") or "",
                    "last_error": result.get("blocker_description"),
                    "finished_at": iso_now(),
                },
            )
            append_run_event(
                {
                    "project": runtime.name,
                    "pass_type": "worker",
                    "worker_id": worker_id,
                    "worker_name": worker_name,
                    "slot": profile.slot,
                    "task_id": task.task_id,
                    "task_source": task.source,
                    "task_title": task.title,
                    "tool": tool,
                    "model": model,
                    "effort": effort,
                    "blocked": True,
                    "status": result.get("status") or "in_progress",
                    "completed": result.get("completed") or "",
                    "next_task": result.get("next_task"),
                    "tests_ran": result.get("tests_ran") or [],
                    "return_code": return_code,
                    "duration_seconds": round(max(0.0, time.monotonic() - pass_started), 2),
                    "error": result.get("blocker_description") or "No blocker provided",
                }
            )
            return 1

        backend.mark_done(worker_name, task, result.get("completed") or task.description)
        status_store.update_worker(
            worker_id,
            {
                "status": "idle",
                "last_result": result.get("completed") or "",
                "last_error": None,
                "finished_at": iso_now(),
                "task_id": None,
                "current_task": None,
            },
        )
        append_run_event(
            {
                "project": runtime.name,
                "pass_type": "worker",
                "worker_id": worker_id,
                "worker_name": worker_name,
                "slot": profile.slot,
                "task_id": task.task_id,
                "task_source": task.source,
                "task_title": task.title,
                "tool": tool,
                "model": model,
                "effort": effort,
                "blocked": False,
                "status": result.get("status") or "in_progress",
                "completed": result.get("completed") or "",
                "next_task": result.get("next_task"),
                "tests_ran": result.get("tests_ran") or [],
                "return_code": return_code,
                "duration_seconds": round(max(0.0, time.monotonic() - pass_started), 2),
                "error": None,
            }
        )
        return return_code
    except Exception as exc:
        reason = f"Worker pass crashed: {exc}"
        failure = default_failure_result(reason)
        try:
            persist_result(runtime, task, failure)
        except Exception:
            pass
        try:
            backend.mark_blocked(worker_name, task, reason)
        except Exception:
            pass
        status_store.update_worker(
            worker_id,
            {
                "status": "blocked",
                "last_result": "",
                "last_error": reason,
                "finished_at": iso_now(),
            },
        )
        append_run_event(
            {
                "project": runtime.name,
                "pass_type": "worker",
                "worker_id": worker_id,
                "worker_name": worker_name,
                "slot": profile.slot,
                "task_id": task.task_id,
                "task_source": task.source,
                "task_title": task.title,
                "tool": tool,
                "model": model,
                "effort": effort,
                "blocked": True,
                "status": "in_progress",
                "completed": "",
                "next_task": "",
                "tests_ran": [],
                "return_code": 1,
                "duration_seconds": round(max(0.0, time.monotonic() - pass_started), 2),
                "error": reason,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
