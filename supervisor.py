"""
supervisor.py - Deterministic watchdog for interactive AgentLoop sessions.

Responsibilities:
- Launch missing workers and orchestrators in Windows Terminal tabs
- Watch walkie-talkie users and recent message history
- Re-send current tasks to quiet top-level project handles
- Kick stale offline handles before relaunching

The supervisor does not replace the LLM orchestrator. It handles the mechanical
parts so the orchestrator can stay thin:
- projects.json remains the source of truth
- walkie-talkie carries short signals (READY/TASK/DONE/BLOCKED)
- this file handles timers, retries, relaunches, and nudges
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

import launch_all

AGENTLOOP_DIR = Path(__file__).resolve().parent
PROJECTS_FILE = AGENTLOOP_DIR / "projects.json"
LOG_DIR = AGENTLOOP_DIR / "logs"
STATE_FILE = LOG_DIR / "supervisor_state.json"
LOG_FILE = LOG_DIR / "supervisor.log"

DEFAULT_HUB_URL = os.getenv("WALKIE_TALKIE_HUB_URL", "http://localhost:9559")
DEFAULT_INTERVAL_SECONDS = 20
DEFAULT_HISTORY_LIMIT = 500
DEFAULT_QUIET_MINUTES = 15
DEFAULT_OFFLINE_GRACE_SECONDS = 45
DEFAULT_MISSING_GRACE_SECONDS = 20
DEFAULT_RELAUNCH_COOLDOWN_SECONDS = 90
DEFAULT_NUDGE_COOLDOWN_SECONDS = 300
DEFAULT_TEAM_RESPONSE_GRACE_SECONDS = 45
DEFAULT_TEAM_NUDGE_COOLDOWN_SECONDS = 60
DEFAULT_TEAM_RESTART_GRACE_SECONDS = 120


@dataclass
class HandleSpec:
    handle: str
    role: str
    project_name: str | None
    channel: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class HandleState:
    missing_since: float | None = None
    offline_since: float | None = None
    last_launch_at: float | None = None
    last_kick_at: float | None = None
    last_nudge_at: float | None = None
    launch_attempts: int = 0
    capped_notice_at: float | None = None


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ps_pattern_literal(value: str) -> str:
    return value.replace("'", "''")


def ensure_logs() -> None:
    LOG_DIR.mkdir(exist_ok=True)


def append_log(message: str) -> None:
    ensure_logs()
    line = f"[{iso_now()}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def stop_team_orchestrator_process(spec: HandleSpec) -> None:
    # Kill only the local process tree that launched this project orchestrator.
    patterns = [
        f"*join walkie-talkie as '{spec.handle}'*",
        f'*radio_join as "{spec.handle}"*',
    ]
    pattern_clauses = " -or ".join(
        f"($_.CommandLine -like '{ps_pattern_literal(pattern)}')" for pattern in patterns
    )
    powershell = f"""
$matched = Get-CimInstance Win32_Process | Where-Object {{
  $_.CommandLine -and ({pattern_clauses})
}}
$allowed = @('cmd.exe','python.exe','node.exe','codex.exe','opencode.exe')
$ids = New-Object System.Collections.Generic.HashSet[int]
foreach ($proc in $matched) {{
  [void]$ids.Add([int]$proc.ProcessId)
  $parentId = [int]$proc.ParentProcessId
  while ($parentId -gt 0) {{
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentId" -ErrorAction SilentlyContinue
    if (-not $parent) {{ break }}
    if ($allowed -notcontains $parent.Name) {{ break }}
    [void]$ids.Add([int]$parent.ProcessId)
    $parentId = [int]$parent.ParentProcessId
  }}
}}
$ids | Sort-Object -Descending | ForEach-Object {{
  try {{ Stop-Process -Id $_ -Force -ErrorAction Stop }} catch {{}}
}}
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", powershell],
        check=False,
        capture_output=True,
        text=True,
    )


def load_env_file() -> None:
    env_path = AGENTLOOP_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_state() -> dict[str, HandleState]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    states: dict[str, HandleState] = {}
    for handle, payload in data.items():
        states[handle] = HandleState(**payload)
    return states


def save_state(state: dict[str, HandleState]) -> None:
    ensure_logs()
    serializable = {handle: asdict(payload) for handle, payload in state.items()}
    STATE_FILE.write_text(json.dumps(serializable, indent=2), encoding="utf-8")


def projects_by_name(projects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {project["name"]: project for project in projects}


def task_for_project(project: dict[str, Any]) -> str:
    next_task = (project.get("next_task") or "").strip()
    if next_task and project.get("status") != "improving":
        return next_task
    return (
        "Read your CLAUDE.md and AGENTS.md, then brainstorm and implement the "
        "most impactful improvement you can find. Update next_task in projects.json when done."
    )


def parse_message_state(messages: list[dict[str, Any]], handles: set[str]) -> dict[str, dict[str, Any]]:
    summary = {
        handle: {
            "last_seen": None,
            "last_outbound": None,
            "last_inbound_task": None,
            "last_state": "unknown",
        }
        for handle in handles
    }

    for message in sorted(messages, key=lambda item: item.get("timestamp", 0)):
        sender = message.get("from")
        recipient = message.get("to")
        content = (message.get("content") or "").strip()
        ts = message.get("timestamp")

        if sender in handles:
            summary[sender]["last_seen"] = ts
            summary[sender]["last_outbound"] = ts
            if content.startswith("READY "):
                summary[sender]["last_state"] = "ready"
            elif content.startswith("RECEIVED "):
                summary[sender]["last_state"] = "working"
            elif content.startswith("DONE "):
                summary[sender]["last_state"] = "done"
            elif content.startswith("BLOCKED "):
                summary[sender]["last_state"] = "blocked"

        if recipient in {f"@{handle}" for handle in handles} and content.startswith("TASK"):
            target = recipient[1:]
            summary[target]["last_inbound_task"] = ts
            summary[target]["last_seen"] = ts
            summary[target]["last_state"] = "task_sent"

    return summary


def latest_team_channel_activity(
    messages: list[dict[str, Any]],
    orchestrator_handle: str,
    worker_handles: list[str],
    channel: str,
) -> dict[str, Any]:
    worker_handles_set = set(worker_handles)
    latest_worker_event: dict[str, Any] | None = None
    latest_orchestrator_event: dict[str, Any] | None = None

    for message in sorted(messages, key=lambda item: item.get("timestamp", 0)):
        if message.get("channel") != channel:
            continue

        sender = message.get("from")
        content = (message.get("content") or "").strip()
        timestamp = message.get("timestamp", 0)

        if sender == orchestrator_handle:
            latest_orchestrator_event = {
                "timestamp": timestamp,
                "content": content,
            }
            continue

        if sender not in worker_handles_set:
            continue

        if content.startswith(("READY ", "RECEIVED ", "DONE ", "BLOCKED ")):
            latest_worker_event = {
                "timestamp": timestamp,
                "content": content,
                "sender": sender,
            }

    return {
        "worker_event": latest_worker_event,
        "orchestrator_event": latest_orchestrator_event,
    }


class HubClient:
    def __init__(self, hub_url: str, admin_token: str | None):
        self.hub_url = hub_url.rstrip("/")
        self.admin_token = admin_token
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _admin_headers(self) -> dict[str, str]:
        if not self.admin_token:
            raise RuntimeError("Admin token is required for this action.")
        return {"Authorization": f"Bearer {self.admin_token}"}

    def users(self) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.hub_url}/users", timeout=10)
        response.raise_for_status()
        return response.json().get("users", [])

    def recent_messages(self, limit: int) -> list[dict[str, Any]]:
        if not self.admin_token:
            return []
        response = self.session.get(
            f"{self.hub_url}/admin-channel-history",
            params={"limit": limit},
            headers=self._admin_headers(),
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("messages", [])

    def admin_send(self, to: str, content: str, channel: str = "#all", from_name: str = "orchestrator") -> None:
        response = self.session.post(
            f"{self.hub_url}/admin-send",
            headers=self._admin_headers(),
            data=json.dumps({"from": from_name, "to": to, "content": content, "channel": channel}),
            timeout=10,
        )
        response.raise_for_status()

    def kick(self, name: str) -> None:
        response = self.session.post(
            f"{self.hub_url}/kick",
            headers=self._admin_headers(),
            data=json.dumps({"name": name}),
            timeout=10,
        )
        response.raise_for_status()


def build_expected_handles(
    active_projects: list[dict[str, Any]],
    teams: dict[str, Any],
    include_global_orchestrator: bool,
    single_tool_override: str | None,
    single_model_override: str | None,
    orch_model: str,
) -> list[HandleSpec]:
    specs: list[HandleSpec] = []

    if include_global_orchestrator:
        specs.append(
            HandleSpec(
                handle="orchestrator",
                role="global_orchestrator",
                project_name=None,
                channel="#all",
                meta={"orch_model": orch_model},
            )
        )

    for project in active_projects:
        team_cfg = teams.get(project["name"], {})
        if team_cfg:
            cfg = {**launch_all.TEAM_DEFAULTS, **team_cfg}
            orch_handle = launch_all.make_handle(project["name"])
            channel = cfg["channel"] or orch_handle
            worker_count = cfg["workers"] if isinstance(cfg["workers"], int) else len(cfg["workers"])
            worker_tools = cfg["workers"] if isinstance(cfg["workers"], list) else [cfg["worker_tool"]] * worker_count
            worker_handle_base = orch_handle[:15]
            worker_handles = [f"{worker_handle_base}-w{i + 1}" for i in range(worker_count)]

            specs.append(
                HandleSpec(
                    handle=orch_handle,
                    role="team_orchestrator",
                    project_name=project["name"],
                    channel="#all",
                    meta={
                        "channel": channel,
                        "worker_handles": worker_handles,
                        "tool": cfg["orch_tool"],
                        "model": cfg["orch_model"],
                        "effort": cfg["orch_effort"],
                    },
                )
            )

            for idx, (handle, tool) in enumerate(zip(worker_handles, worker_tools), start=1):
                specs.append(
                    HandleSpec(
                        handle=handle,
                        role="team_worker",
                        project_name=project["name"],
                        channel=f"#{channel}",
                        meta={
                            "channel": channel,
                            "orch_handle": orch_handle,
                            "worker_num": idx,
                            "tool": tool,
                            "model": cfg.get("worker_model"),
                        },
                    )
                )
        else:
            cfg = {**launch_all.SINGLE_WORKER_DEFAULTS, **project.get("worker_config", {})}
            if single_tool_override:
                cfg["tool"] = single_tool_override
            if single_model_override:
                cfg["model"] = single_model_override
            specs.append(
                HandleSpec(
                    handle=launch_all.make_handle(project["name"]),
                    role="single_worker",
                    project_name=project["name"],
                    channel="#all",
                    meta={
                        "tool": cfg["tool"],
                        "model": cfg["model"],
                        "effort": cfg["effort"],
                    },
                )
            )

    return specs


def launch_handle(spec: HandleSpec, projects_map: dict[str, dict[str, Any]], active_projects: list[dict[str, Any]]) -> None:
    if spec.role == "global_orchestrator":
        launch_all.launch_global_orchestrator(active_projects, spec.meta["orch_model"], "high")
        append_log("Launched global orchestrator tab.")
        return

    if not spec.project_name:
        return

    project = projects_map[spec.project_name]

    if spec.role == "single_worker":
        launch_all.launch_single_worker(project, spec.meta["tool"], spec.meta["model"], spec.meta["effort"])
        append_log(f"Launched worker tab for {spec.project_name} [{spec.handle}].")
        return

    if spec.role == "team_orchestrator":
        launch_all.launch_team_orchestrator(
            project,
            spec.meta["channel"],
            spec.meta["worker_handles"],
            spec.handle,
            spec.meta["tool"],
            spec.meta["model"],
            spec.meta["effort"],
        )
        append_log(f"Launched team orchestrator for {spec.project_name} [{spec.handle}].")
        return

    if spec.role == "team_worker":
        launch_all.launch_team_worker(
            project,
            spec.meta["channel"],
            spec.handle,
            spec.meta["orch_handle"],
            spec.meta["worker_num"],
            spec.meta["tool"],
            spec.meta["model"],
        )
        append_log(f"Launched team worker {spec.handle} for {spec.project_name}.")


def maybe_relaunch_handle(
    spec: HandleSpec,
    state: HandleState,
    is_registered: bool,
    is_online: bool,
    args: argparse.Namespace,
    client: HubClient,
    projects_map: dict[str, dict[str, Any]],
    active_projects: list[dict[str, Any]],
) -> None:
    now = now_ts()

    if not is_registered:
        state.offline_since = None
        if state.missing_since is None:
            state.missing_since = now
        if state.launch_attempts >= args.max_launch_attempts:
            if not state.capped_notice_at or now - state.capped_notice_at >= args.relaunch_cooldown_seconds:
                append_log(
                    f"Launch cap reached for {spec.handle}. "
                    f"Skipping more relaunch attempts until it registers or you restart the supervisor."
                )
                state.capped_notice_at = now
            return
        if args.launch_missing and now - state.missing_since >= args.missing_grace_seconds:
            if not state.last_launch_at or now - state.last_launch_at >= args.relaunch_cooldown_seconds:
                launch_handle(spec, projects_map, active_projects)
                state.last_launch_at = now
                state.launch_attempts += 1
        return

    state.missing_since = None
    state.launch_attempts = 0
    state.capped_notice_at = None

    if is_online:
        state.offline_since = None
        return

    if state.offline_since is None:
        state.offline_since = now
        return

    if now - state.offline_since < args.offline_grace_seconds:
        return

    if args.kick_stale and client.admin_token:
        if not state.last_kick_at or now - state.last_kick_at >= args.relaunch_cooldown_seconds:
            try:
                client.kick(spec.handle)
                state.last_kick_at = now
                append_log(f"Kicked stale offline handle {spec.handle}.")
            except requests.RequestException as exc:
                append_log(f"Failed to kick {spec.handle}: {exc}")

    if args.launch_missing:
        if not state.last_launch_at or now - state.last_launch_at >= args.relaunch_cooldown_seconds:
            if state.launch_attempts >= args.max_launch_attempts:
                if not state.capped_notice_at or now - state.capped_notice_at >= args.relaunch_cooldown_seconds:
                    append_log(
                        f"Launch cap reached for {spec.handle}. "
                        f"Skipping more relaunch attempts until it registers or you restart the supervisor."
                    )
                    state.capped_notice_at = now
                return
            launch_handle(spec, projects_map, active_projects)
            state.last_launch_at = now
            state.launch_attempts += 1


def maybe_nudge_handle(
    spec: HandleSpec,
    handle_state: HandleState,
    message_state: dict[str, Any],
    users: dict[str, dict[str, Any]],
    projects_map: dict[str, dict[str, Any]],
    client: HubClient,
    args: argparse.Namespace,
) -> None:
    if spec.role != "single_worker":
        return
    if not client.admin_token:
        return
    if spec.handle not in users or not users[spec.handle].get("online"):
        return
    if not spec.project_name or spec.project_name not in projects_map:
        return

    project = projects_map[spec.project_name]
    quiet_after = max(
        message_state.get("last_seen") or 0,
        handle_state.last_launch_at or 0,
    )
    if quiet_after <= 0:
        return

    now = now_ts()
    quiet_for_seconds = now - (quiet_after / 1000 if quiet_after > 10_000_000_000 else quiet_after)
    if quiet_for_seconds < args.quiet_minutes * 60:
        return
    if handle_state.last_nudge_at and now - handle_state.last_nudge_at < args.nudge_cooldown_seconds:
        return

    task = task_for_project(project)
    try:
        client.admin_send(
            to=f"@{spec.handle}",
            content=f"TASK: {task}",
            channel="#all",
            from_name="orchestrator",
        )
        handle_state.last_nudge_at = now
        append_log(f"Re-sent task to quiet handle {spec.handle}: {task[:120]}")
    except requests.RequestException as exc:
        append_log(f"Failed to send nudge to {spec.handle}: {exc}")


def maybe_nudge_team_orchestrator(
    spec: HandleSpec,
    handle_state: HandleState,
    users: dict[str, dict[str, Any]],
    client: HubClient,
    messages: list[dict[str, Any]],
    args: argparse.Namespace,
) -> None:
    if spec.role != "team_orchestrator":
        return
    if not client.admin_token:
        return
    if spec.handle not in users or not users[spec.handle].get("online"):
        return

    channel = f"#{spec.meta['channel']}"
    worker_handles = spec.meta.get("worker_handles", [])
    activity = latest_team_channel_activity(messages, spec.handle, worker_handles, channel)
    worker_event = activity["worker_event"]
    if not worker_event:
        return

    orchestrator_event = activity["orchestrator_event"]
    worker_ts = worker_event["timestamp"]
    orch_ts = orchestrator_event["timestamp"] if orchestrator_event else 0

    if orch_ts >= worker_ts:
        return

    now = now_ts()
    worker_age_seconds = now - (worker_ts / 1000 if worker_ts > 10_000_000_000 else worker_ts)
    if worker_age_seconds < args.team_response_grace_seconds:
        return
    if handle_state.last_nudge_at and now - handle_state.last_nudge_at < args.team_nudge_cooldown_seconds:
        return

    content = worker_event["content"]
    sender = worker_event["sender"]
    try:
        client.admin_send(
            to=f"@{spec.handle}",
            channel=channel,
            from_name="orchestrator",
            content=(
                f"Channel watcher: {sender} sent '{content}'. "
                f"Read recent history on {channel}, respond to that worker if needed, and return to radio_standby."
            ),
        )
        handle_state.last_nudge_at = now
        append_log(
            f"Nudged team orchestrator {spec.handle} on {channel} after worker activity from {sender}: {content[:120]}"
        )
    except requests.RequestException as exc:
        append_log(f"Failed to send team nudge to {spec.handle}: {exc}")


def maybe_restart_team_orchestrator(
    spec: HandleSpec,
    handle_state: HandleState,
    users: dict[str, dict[str, Any]],
    client: HubClient,
    messages: list[dict[str, Any]],
    args: argparse.Namespace,
    projects_map: dict[str, dict[str, Any]],
    active_projects: list[dict[str, Any]],
) -> None:
    if spec.role != "team_orchestrator":
        return
    if not client.admin_token:
        return
    if spec.handle not in users or not users[spec.handle].get("online"):
        return
    if not handle_state.last_nudge_at:
        return

    channel = f"#{spec.meta['channel']}"
    worker_handles = spec.meta.get("worker_handles", [])
    activity = latest_team_channel_activity(messages, spec.handle, worker_handles, channel)
    worker_event = activity["worker_event"]
    if not worker_event:
        return

    orchestrator_event = activity["orchestrator_event"]
    worker_ts = worker_event["timestamp"]
    orch_ts = orchestrator_event["timestamp"] if orchestrator_event else 0
    if orch_ts >= worker_ts:
        return

    now = now_ts()
    if now - handle_state.last_nudge_at < args.team_restart_grace_seconds:
        return
    if handle_state.last_launch_at and now - handle_state.last_launch_at < args.relaunch_cooldown_seconds:
        return
    if not spec.project_name or spec.project_name not in projects_map:
        return

    try:
        stop_team_orchestrator_process(spec)
        client.kick(spec.handle)
        append_log(f"Restarting stalled team orchestrator {spec.handle} for {spec.project_name}.")
    except requests.RequestException as exc:
        append_log(f"Failed to kick stalled team orchestrator {spec.handle}: {exc}")

    time.sleep(5)
    launch_handle(spec, projects_map, active_projects)
    handle_state.last_launch_at = now
    handle_state.last_nudge_at = None


def run_iteration(args: argparse.Namespace, state: dict[str, HandleState]) -> None:
    load_env_file()
    client = HubClient(args.hub_url, args.admin_token or os.getenv("WALKIE_TALKIE_ADMIN_TOKEN"))

    projects = launch_all.load_projects()
    teams = launch_all.load_teams()
    active_projects = launch_all.get_active_projects(projects)

    if args.projects:
        active_projects = launch_all.filter_by_names(projects, args.projects)

    projects_map = projects_by_name(active_projects)
    expected = build_expected_handles(
        active_projects=active_projects,
        teams=teams,
        include_global_orchestrator=not args.no_global_orch,
        single_tool_override=args.tool,
        single_model_override=args.model,
        orch_model=args.orch_model,
    )

    users = {user["name"]: user for user in client.users()}
    messages = client.recent_messages(args.history_limit)
    message_summary = parse_message_state(messages, {spec.handle for spec in expected})

    append_log(
        "Supervisor check: "
        f"{len(active_projects)} active project(s), "
        f"{len(expected)} expected handle(s), "
        f"{len(users)} registered user(s)."
    )

    for spec in expected:
        handle_state = state.setdefault(spec.handle, HandleState())
        user = users.get(spec.handle)
        is_registered = user is not None
        is_online = bool(user and user.get("online"))

        maybe_relaunch_handle(
            spec=spec,
            state=handle_state,
            is_registered=is_registered,
            is_online=is_online,
            args=args,
            client=client,
            projects_map=projects_map,
            active_projects=active_projects,
        )

        maybe_nudge_handle(
            spec=spec,
            handle_state=handle_state,
            message_state=message_summary.get(spec.handle, {}),
            users=users,
            projects_map=projects_map,
            client=client,
            args=args,
        )

        maybe_nudge_team_orchestrator(
            spec=spec,
            handle_state=handle_state,
            users=users,
            client=client,
            messages=messages,
            args=args,
        )

        maybe_restart_team_orchestrator(
            spec=spec,
            handle_state=handle_state,
            users=users,
            client=client,
            messages=messages,
            args=args,
            projects_map=projects_map,
            active_projects=active_projects,
        )

    save_state(state)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic watchdog for AgentLoop interactive workers.")
    parser.add_argument("projects", nargs="*", help="Optional project name filters.")
    parser.add_argument("--hub-url", default=DEFAULT_HUB_URL, help=f"Walkie-talkie hub URL (default: {DEFAULT_HUB_URL})")
    parser.add_argument("--admin-token", default=None, help="Admin token for /admin-send and /kick. Defaults to WALKIE_TALKIE_ADMIN_TOKEN env var.")
    parser.add_argument("--no-global-orch", action="store_true", help="Do not expect or relaunch the global orchestrator.")
    parser.add_argument("--no-launch", dest="launch_missing", action="store_false", help="Observe only. Do not launch missing handles.")
    parser.add_argument("--no-kick", dest="kick_stale", action="store_false", help="Do not kick stale offline handles.")
    parser.add_argument("--once", action="store_true", help="Run one supervisor check and exit.")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Polling interval between checks.")
    parser.add_argument("--history-limit", type=int, default=DEFAULT_HISTORY_LIMIT, help="How many recent hub messages to inspect.")
    parser.add_argument("--quiet-minutes", type=int, default=DEFAULT_QUIET_MINUTES, help="Minutes of silence before re-sending the current task.")
    parser.add_argument("--offline-grace-seconds", type=int, default=DEFAULT_OFFLINE_GRACE_SECONDS, help="Seconds to wait before treating an offline handle as stale.")
    parser.add_argument("--missing-grace-seconds", type=int, default=DEFAULT_MISSING_GRACE_SECONDS, help="Seconds to wait before relaunching an unregistered handle.")
    parser.add_argument("--relaunch-cooldown-seconds", type=int, default=DEFAULT_RELAUNCH_COOLDOWN_SECONDS, help="Minimum seconds between relaunch attempts for the same handle.")
    parser.add_argument("--nudge-cooldown-seconds", type=int, default=DEFAULT_NUDGE_COOLDOWN_SECONDS, help="Minimum seconds between task re-sends for the same handle.")
    parser.add_argument("--team-response-grace-seconds", type=int, default=DEFAULT_TEAM_RESPONSE_GRACE_SECONDS, help="Seconds to wait after a worker message before nudging a project orchestrator on its team channel.")
    parser.add_argument("--team-nudge-cooldown-seconds", type=int, default=DEFAULT_TEAM_NUDGE_COOLDOWN_SECONDS, help="Minimum seconds between nudges to the same team orchestrator.")
    parser.add_argument("--team-restart-grace-seconds", type=int, default=DEFAULT_TEAM_RESTART_GRACE_SECONDS, help="Seconds to wait after a team-orchestrator nudge before restarting that orchestrator if it still has not responded.")
    parser.add_argument("--max-launch-attempts", type=int, default=2, help="Maximum relaunch attempts for a handle before the supervisor stops retrying.")
    parser.add_argument("--orch-model", default="sonnet", help="Model to use if the global orchestrator is relaunched.")
    parser.add_argument("--tool", choices=list(launch_all.TOOL_CONFIGS), default=None, help="Override tool for single workers.")
    parser.add_argument("--model", default=None, help="Override model for single workers.")
    parser.set_defaults(launch_missing=True, kick_stale=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    ensure_logs()
    load_env_file()
    state = load_state()

    append_log("Supervisor starting.")

    try:
        while True:
            run_iteration(args, state)
            if args.once:
                break
            time.sleep(max(5, args.interval_seconds))
    except KeyboardInterrupt:
        append_log("Supervisor stopped by user.")
        return 0
    except requests.RequestException as exc:
        append_log(f"Supervisor HTTP error: {exc}")
        return 1
    except Exception as exc:  # pragma: no cover - last-resort guard for overnight runs
        append_log(f"Supervisor crashed: {exc}")
        return 1

    append_log("Supervisor finished single check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
