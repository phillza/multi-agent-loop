from __future__ import annotations

import argparse
import time
from typing import Any

from auto_worker_pass import build_command, default_failure_result, extract_json, run_with_heartbeat
from project_runtime import discover_project_runtime, update_project_record
from runtime_store import RuntimeStatusStore, append_run_event, iso_now
from task_backends import TaskItem


class NoopBackend:
    def heartbeat(self, worker_name: str, task: TaskItem) -> None:
        return


def build_prompt(runtime) -> str:
    docs = runtime.docs or []
    docs_text = "\n".join(f"- {path}" for path in docs) if docs else "- No repo docs were auto-discovered."
    task_board_line = f"Task board: {runtime.task_board_path}" if runtime.task_board_path else "This project does not use a shared markdown task board."
    goals = runtime.project_record.get("goals") or []
    goals_text = "\n".join(f"- {goal}" for goal in goals) if goals else "- Use the project context and recent completions to choose the next useful work."
    queue_refill_rule = (
        "If no claimable `[ ]` rows exist, add 3-5 concrete new `[ ]` tasks to the worker-operable section of TASK_BOARD.md.\n"
        "Each new task must include clear scope, file ownership hints, and dependencies when relevant."
        if runtime.task_backend == "markdown_board"
        else "If the project queue is empty, research the codebase and define the next highest-impact task in `next_task`."
    )

    return f"""You are the strategist for project: {runtime.name}

Project directory: {runtime.project_dir}
{task_board_line}
Task backend: {runtime.task_backend}

Read these docs first:
{docs_text}

Project goals:
{goals_text}

Your job in this pass:
- Audit what is already complete
- Find what is blocked, thin, or missing
- Refill the queue with practical next tasks
- Keep the project moving without duplicating active worker implementation

Rules:
- Prefer updating the task board or project task state over random implementation
- If the queue is already healthy, tighten priorities or dependencies instead of churning tasks
- If obvious tasks are complete, proactively research what to build next and refill the queue
- Include at least one concrete improvement task that evaluates better data sources/APIs/datasets when relevant to the project
- Avoid vague tasks: each new task must be specific, testable, and directly tied to project goals
- {queue_refill_rule}
- Leave the project with a clearer next wave of work than you found

FINAL line must be one JSON object:
{{
  "completed": "one sentence summary of what changed",
  "next_task": "project-level next task or empty string",
  "status": "in_progress",
  "blocked": false,
  "blocker_description": null,
  "wait_until": null,
  "tasks_created": ["optional ids"]
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one strategist pass for a project.")
    parser.add_argument("project", help="Project name (partial match ok)")
    parser.add_argument("--tool", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()

    runtime = discover_project_runtime(args.project)
    if not runtime.strategist or not (runtime.strategist.enabled or runtime.strategist.trigger_on_empty_queue):
        raise ValueError(f"Strategist is not enabled for {runtime.name}")

    tool = args.tool or runtime.strategist.tool or "codex"
    model = args.model or runtime.strategist.model
    effort = args.effort or runtime.strategist.effort
    status_store = RuntimeStatusStore()
    worker_id = f"{runtime.slug}:strategist"
    worker_name = f"{runtime.slug}-strategist"
    pseudo_task = TaskItem(
        task_id="strategist-pass",
        title="Strategist pass",
        description="Audit the project and refresh the next wave of work.",
        source="strategist",
        status="working",
        owner=worker_name,
    )

    status_store.update_worker(
        worker_id,
        {
            "project": runtime.name,
            "slot": "strategist",
            "role": "strategist",
            "tool": tool,
            "model": model,
            "status": "working",
            "current_task": pseudo_task.description,
            "started_at": iso_now(),
        },
    )

    pass_started = time.monotonic()
    result: dict[str, Any]
    try:
        prompt = build_prompt(runtime)
        cmd, last_message_file = build_command(tool, model, effort, prompt, runtime.project_dir)
        _, output_text = run_with_heartbeat(
            cmd=cmd,
            cwd=runtime.project_dir,
            status_store=status_store,
            worker_id=worker_id,
            backend=NoopBackend(),
            worker_name=worker_name,
            task=pseudo_task,
            timeout_seconds=args.timeout_seconds,
            last_message_file=last_message_file,
        )
        result = extract_json(output_text) or default_failure_result("Strategist did not return the required final JSON result.")
    except Exception as exc:
        result = default_failure_result(f"Strategist pass crashed: {exc}")

    blocked = bool(result.get("blocked"))
    summary = (result.get("completed") or "").strip()
    updates: dict[str, Any] = {
        "blocked": blocked,
        "blocker_description": result.get("blocker_description") if blocked else None,
    }
    if "next_task" in result:
        updates["next_task"] = result.get("next_task", "")
    else:
        updates["next_task"] = runtime.project_record.get("next_task", "")
    requested_status = str(result.get("status") or "").strip().lower()
    allowed_statuses = {"in_progress", "improving"}
    if blocked:
        updates["status"] = "blocked"
    elif requested_status in allowed_statuses:
        updates["status"] = requested_status
    else:
        updates["status"] = runtime.project_record.get("status", "in_progress")
    if "wait_until" in result:
        updates["wait_until"] = result.get("wait_until")

    update_project_record(runtime.name, updates, log_entry=summary if summary else None)

    status_store.update_worker(
        worker_id,
        {
            "status": "blocked" if blocked else "idle",
            "last_result": summary,
            "last_error": result.get("blocker_description") if blocked else None,
            "finished_at": iso_now(),
            "current_task": None,
        },
    )
    append_run_event(
        {
            "project": runtime.name,
            "pass_type": "strategist",
            "worker_id": worker_id,
            "worker_name": worker_name,
            "slot": "strategist",
            "task_id": "strategist-pass",
            "task_source": "strategist",
            "task_title": "Strategist pass",
            "tool": tool,
            "model": model,
            "effort": effort,
            "blocked": blocked,
            "status": result.get("status") or runtime.project_record.get("status", "in_progress"),
            "completed": summary,
            "next_task": result.get("next_task") if "next_task" in result else runtime.project_record.get("next_task", ""),
            "tasks_created": result.get("tasks_created") or [],
            "return_code": 1 if blocked else 0,
            "duration_seconds": round(max(0.0, time.monotonic() - pass_started), 2),
            "error": result.get("blocker_description") if blocked else None,
        }
    )
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
