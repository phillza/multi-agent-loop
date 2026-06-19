from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


ROOT_DIR = Path(__file__).resolve().parent
PROJECTS_FILE = ROOT_DIR / "projects.json"
RUNTIME_STATUS_FILE = ROOT_DIR / "logs" / "runtime_status.json"
LAUNCH_ALL = ROOT_DIR / "launch_all.py"
MONITOR_WEB = ROOT_DIR / "monitor_web.py"
RUNTIME_SUPERVISOR = ROOT_DIR / "runtime_supervisor.py"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        normalized = ts.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def seconds_since(ts: str | None) -> int | None:
    then = parse_iso(ts)
    if not then:
        return None
    now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
    return max(0, int((now - then).total_seconds()))


def wait_until_is_future(wait_until: str | None) -> bool:
    dt = parse_iso(wait_until)
    if not dt:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return dt > now


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def active_projects(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for project in projects:
        if project.get("status") not in ("in_progress", "improving"):
            continue
        if project.get("blocked"):
            continue
        if wait_until_is_future(project.get("wait_until")):
            continue
        active.append(project)
    return active


def resolve_project_filters(filters: list[str]) -> list[str] | None:
    projects = load_json(PROJECTS_FILE, [])
    names = [str(p.get("name", "")) for p in projects]
    resolved: list[str] = []
    seen: set[str] = set()
    for query in filters:
        exact = [name for name in names if name.lower() == query.lower()]
        if len(exact) == 1:
            target = exact[0]
        else:
            partial = [name for name in names if query.lower() in name.lower()]
            if not partial:
                print(f"No project matches '{query}'.")
                return None
            if len(partial) > 1:
                print(f"Ambiguous project filter '{query}'. Matches:")
                for candidate in partial:
                    print(f"  - {candidate}")
                return None
            target = partial[0]
        if target not in seen:
            resolved.append(target)
            seen.add(target)
    return resolved


def runtime_supervisor_is_live(max_age_seconds: int = 120) -> bool:
    runtime = load_json(RUNTIME_STATUS_FILE, {})
    if not isinstance(runtime, dict):
        return False
    updated_age = seconds_since(runtime.get("updated_at"))
    if updated_age is None or updated_age > max_age_seconds:
        return False
    projects = runtime.get("projects") or {}
    for entry in projects.values():
        checked_age = seconds_since(entry.get("supervisor_checked_at") or entry.get("last_updated_at"))
        if checked_age is not None and checked_age <= max_age_seconds:
            return True
    return False


def run_launch_all(project_filters: list[str] | None = None, force: bool = False) -> int:
    resolved_filters = project_filters
    if project_filters:
        resolved_filters = resolve_project_filters(project_filters)
        if resolved_filters is None:
            return 1
    if runtime_supervisor_is_live() and not force:
        print("A runtime supervisor heartbeat is already live. Skipping new launch.")
        print("Use --force if you intentionally want another supervisor.")
        return 0
    cmd = [sys.executable, str(LAUNCH_ALL), "--autonomous"]
    if resolved_filters:
        cmd.extend(resolved_filters)
    return subprocess.call(cmd, cwd=str(ROOT_DIR))


def _popen_new_tab(title: str, cmd: list[str], cwd: Path) -> None:
    if shutil.which("wt"):
        subprocess.Popen(["wt", "-w", "0", "new-tab", "--title", title, "-d", str(cwd), "--", *cmd], cwd=str(cwd))
        return
    cmd_str = " ".join(f'"{part}"' if " " in part else part for part in cmd)
    subprocess.Popen(f'start "{title}" {cmd_str}', shell=True, cwd=str(cwd))


def open_dashboard(new_tab: bool) -> int:
    cmd = [sys.executable, str(MONITOR_WEB)]
    if new_tab:
        _popen_new_tab("AgentLoop Dashboard", cmd, ROOT_DIR)
        print("Dashboard started in a new tab.")
        return 0
    return subprocess.call(cmd, cwd=str(ROOT_DIR))


def status_snapshot() -> int:
    projects = load_json(PROJECTS_FILE, [])
    runtime = load_json(RUNTIME_STATUS_FILE, {"projects": {}, "workers": {}})

    active = active_projects(projects)
    paused = [p for p in projects if p.get("status") == "paused"]
    complete = [p for p in projects if p.get("status") == "complete"]
    blocked = [p for p in projects if p.get("blocked")]

    print(f"\nAgentLoop Status ({now_iso()})")
    print(f"- Active:   {len(active)}")
    print(f"- Paused:   {len(paused)}")
    print(f"- Complete: {len(complete)}")
    print(f"- Blocked:  {len(blocked)}")

    runtime_projects = runtime.get("projects", {}) if isinstance(runtime, dict) else {}
    runtime_workers = runtime.get("workers", {}) if isinstance(runtime, dict) else {}
    print(f"- Runtime project records: {len(runtime_projects)}")
    print(f"- Runtime worker records:  {len(runtime_workers)}")

    if runtime_projects:
        print("\nTop active runtime projects:")
        shown = 0
        for name, info in runtime_projects.items():
            state = info.get("supervisor_state", "unknown")
            queue_depth = info.get("queue_depth", "?")
            busy = info.get("busy_slots", 0)
            print(f"  - {name}: state={state}, queue={queue_depth}, busy_slots={busy}")
            shown += 1
            if shown >= 10:
                break
    else:
        print("\nNo runtime status found yet. Start with: python easy_agentloop.py start")
    print()
    return 0


def doctor_check() -> int:
    ok = True
    print(f"\nAgentLoop Doctor ({now_iso()})")

    required_files = [PROJECTS_FILE, LAUNCH_ALL, MONITOR_WEB, RUNTIME_SUPERVISOR]
    for path in required_files:
        if path.exists():
            print(f"[OK] File exists: {path.name}")
        else:
            ok = False
            print(f"[FAIL] Missing file: {path}")

    projects = load_json(PROJECTS_FILE, [])
    if not isinstance(projects, list) or not projects:
        ok = False
        print("[FAIL] projects.json is missing or invalid.")
    else:
        active = active_projects(projects)
        if not active:
            print("[WARN] No runnable active projects right now.")
        else:
            print(f"[OK] Runnable active projects: {len(active)}")

    runtime = load_json(RUNTIME_STATUS_FILE, None)
    if runtime is None:
        print("[WARN] runtime_status.json not found yet (start may not be running).")
    else:
        updated = runtime.get("updated_at") if isinstance(runtime, dict) else None
        if updated:
            age = seconds_since(updated)
            if age is not None and age > 600:
                ok = False
                print(f"[FAIL] runtime_status is stale ({age}s old): {updated} (start with: python easy_agentloop.py start)")
            else:
                age_text = f" ({age}s ago)" if age is not None else ""
                print(f"[OK] runtime_status updated_at: {updated}{age_text}")
        else:
            print("[WARN] runtime_status.json exists but has no updated_at field.")

    if shutil.which("wt"):
        print("[OK] Windows Terminal (wt) detected.")
    else:
        print("[WARN] Windows Terminal (wt) not detected. Fallback launch still works.")

    # Dashboard API smoke check (runtime mode)
    try:
        from fastapi.testclient import TestClient
        import monitor_web

        monitor_web._monitor_mode = "runtime"
        client = TestClient(monitor_web.app)
        projects_resp = client.get("/api/projects")
        if projects_resp.status_code != 200:
            ok = False
            print(f"[FAIL] Dashboard API /api/projects returned {projects_resp.status_code}.")
        else:
            projects_payload = projects_resp.json()
            if not isinstance(projects_payload, list):
                ok = False
                print("[FAIL] Dashboard API /api/projects did not return a list.")
            else:
                detail_failures = 0
                for project in projects_payload:
                    name = project.get("name")
                    if not name:
                        continue
                    detail_resp = client.get("/api/project/" + quote(name, safe=""))
                    if detail_resp.status_code != 200:
                        detail_failures += 1
                        continue
                    detail_payload = detail_resp.json()
                    if detail_payload.get("ok") is False or detail_payload.get("name") != name:
                        detail_failures += 1
                if detail_failures:
                    ok = False
                    print(f"[FAIL] Dashboard detail endpoint failed for {detail_failures} project(s).")
                else:
                    print(f"[OK] Dashboard API smoke check passed for {len(projects_payload)} project(s).")
    except Exception as exc:
        ok = False
        print(f"[FAIL] Dashboard API smoke check failed: {exc}")

    if ok:
        print("\nDoctor result: PASS\n")
        return 0
    print("\nDoctor result: FAIL\n")
    return 1


def overnight_checklist() -> int:
    print("\nAgentLoop Overnight Checklist")
    print("1) Health check:   python easy_agentloop.py doctor")
    print("2) Start runtime:  python easy_agentloop.py start")
    print("3) Open dashboard: python easy_agentloop.py dashboard")
    print("4) Morning check:  python easy_agentloop.py status")
    print("5) Re-run doctor:  python easy_agentloop.py doctor")
    print("\nTip: If start says a supervisor is already live, that is expected. Use --force only when you intentionally want another one.")
    print()
    return 0


def interactive_menu() -> int:
    while True:
        print("\n=== AgentLoop Easy Menu ===")
        print("1) Start overnight (all active projects)")
        print("2) Start one project")
        print("3) Open dashboard")
        print("4) Start overnight + open dashboard")
        print("5) Status snapshot")
        print("6) Doctor check")
        print("7) Overnight checklist")
        print("8) Exit")
        choice = input("Choose 1-8: ").strip()

        if choice == "1":
            code = run_launch_all()
            if code != 0:
                print("Launch failed.")
        elif choice == "2":
            name = input("Project name (full or partial): ").strip()
            if not name:
                print("Project name is required.")
                continue
            code = run_launch_all([name])
            if code != 0:
                print("Launch failed.")
        elif choice == "3":
            open_dashboard(new_tab=True)
        elif choice == "4":
            code = run_launch_all()
            if code == 0:
                time.sleep(1)
                open_dashboard(new_tab=True)
            else:
                print("Launch failed.")
        elif choice == "5":
            status_snapshot()
        elif choice == "6":
            doctor_check()
        elif choice == "7":
            overnight_checklist()
        elif choice == "8":
            return 0
        else:
            print("Invalid choice. Please enter a number from 1 to 8.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Beginner-friendly launcher for AgentLoop overnight runs and monitoring."
    )
    sub = parser.add_subparsers(dest="command")

    start_parser = sub.add_parser("start", help="Start autonomous overnight runtime.")
    start_parser.add_argument("projects", nargs="*", help="Optional project names (full or partial).")
    start_parser.add_argument("--force", action="store_true", help="Launch even if a runtime supervisor heartbeat is already live.")

    dash_parser = sub.add_parser("dashboard", help="Open the web dashboard.")
    dash_parser.add_argument("--here", action="store_true", help="Run dashboard in current terminal instead of opening a new tab.")

    sub.add_parser("status", help="Show quick runtime/project status.")
    sub.add_parser("doctor", help="Run basic health checks.")
    sub.add_parser("checklist", help="Show a simple overnight run checklist.")
    sub.add_parser("menu", help="Open interactive menu.")

    args = parser.parse_args()

    if args.command in (None, "menu"):
        return interactive_menu()
    if args.command == "start":
        return run_launch_all(args.projects, force=args.force)
    if args.command == "dashboard":
        return open_dashboard(new_tab=not args.here)
    if args.command == "status":
        return status_snapshot()
    if args.command == "doctor":
        return doctor_check()
    if args.command == "checklist":
        return overnight_checklist()
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
