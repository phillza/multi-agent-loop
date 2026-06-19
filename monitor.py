"""
AgentLoop Monitor - Real-time multi-agent TUI dashboard.
All workers run here. Watch, steer, pause, resume — all in one window.

Usage:
  python monitor.py              # All active projects
  python monitor.py 3            # First 3 active projects
  python monitor.py "my-project"     # One specific project
"""

import json
import os
import re
import subprocess
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, RichLog, Input, Label, Button
from textual._work_decorator import work

# --- Config ---
PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")
WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def load_projects():
    with open(PROJECTS_FILE) as f:
        return json.load(f)


def get_project_dir(project):
    path = project.get("path", project["name"])
    return os.path.join(PROJECTS_BASE, path)


def strip_ansi(text):
    return ANSI_ESCAPE.sub("", text)


def colorize(line):
    """Apply Rich markup based on line prefix."""
    line = strip_ansi(line).rstrip()
    if not line:
        return None
    if line.startswith("[CLAUDE]"):
        return f"[cyan]{line}[/cyan]"
    elif line.startswith("  [Read]") or line.startswith("  [Edit]") or line.startswith("  [Write]") or line.startswith("  [Glob]") or line.startswith("  [Grep]"):
        return f"[yellow]{line}[/yellow]"
    elif line.startswith("  [Bash]"):
        return f"[magenta]{line}[/magenta]"
    elif line.startswith("  ->"):
        return f"[dim]{line}[/dim]"
    elif line.startswith("--- Done") or line.startswith("Completed:"):
        return f"[bold green]{line}[/bold green]"
    elif line.startswith("--- TASK ---") or line.startswith("--- IMPROVEMENT"):
        return f"[bold]{line}[/bold]"
    elif "BLOCKED" in line or "ERROR" in line or "Failed" in line:
        return f"[bold red]{line}[/bold red]"
    elif line.startswith("Waiting") or line.startswith("Paused"):
        return f"[orange1]{line}[/orange1]"
    else:
        return line


# --- Project Panel ---
class ProjectPanel(Vertical):
    DEFAULT_CSS = """
    ProjectPanel {
        border: solid $primary-darken-2;
        height: 100%;
        padding: 0;
    }
    ProjectPanel:focus-within {
        border: solid $accent;
    }
    ProjectPanel .panel-header {
        background: $primary-darken-3;
        padding: 0 1;
        height: 1;
    }
    ProjectPanel RichLog {
        height: 1fr;
        scrollbar-gutter: stable;
    }
    ProjectPanel .panel-controls {
        height: 3;
        padding: 0 1;
    }
    ProjectPanel Input {
        height: 3;
        border: tall $primary-darken-2;
    }
    """

    def __init__(self, project: dict, **kwargs):
        super().__init__(**kwargs)
        self.project = project
        self.project_name = project["name"]
        self._proc = None
        self._status = project.get("status", "unknown")

    def compose(self) -> ComposeResult:
        short_name = self.project_name[:28] + "..." if len(self.project_name) > 31 else self.project_name
        yield Label(f"[bold]{short_name}[/bold]", classes="panel-header", id=f"header-{self._safe_id}")
        yield RichLog(id=f"log-{self._safe_id}", markup=True, highlight=False, wrap=True)
        with Horizontal(classes="panel-controls"):
            yield Button("Pause", id=f"btn-pause-{self._safe_id}", variant="warning", compact=True)
            yield Button("Resume", id=f"btn-resume-{self._safe_id}", variant="success", compact=True)
            yield Button("Take Over", id=f"btn-takeover-{self._safe_id}", variant="primary", compact=True)
        yield Input(placeholder="Steer: type a message and press Enter...", id=f"input-{self._safe_id}")

    @property
    def _safe_id(self):
        return re.sub(r"[^a-z0-9]", "-", self.project_name.lower())

    def on_mount(self):
        self.update_header()
        self.start_worker()

    def update_header(self):
        status = self._status
        colors = {
            "in_progress": "green",
            "improving": "blue",
            "paused": "orange1",
            "blocked": "red",
            "complete": "dim",
        }
        color = colors.get(status, "white")
        short_name = self.project_name[:26] + "..." if len(self.project_name) > 29 else self.project_name
        try:
            label = self.query_one(f"#header-{self._safe_id}", Label)
            label.update(f"[bold]{short_name}[/bold]  [{color}]{status.upper()}[/{color}]")
        except Exception:
            pass

    def log_line(self, line: str):
        formatted = colorize(line)
        if formatted is not None:
            try:
                log = self.query_one(f"#log-{self._safe_id}", RichLog)
                log.write(formatted)
            except Exception:
                pass

    def log_system(self, msg: str):
        try:
            log = self.query_one(f"#log-{self._safe_id}", RichLog)
            log.write(f"[dim italic]{msg}[/dim italic]")
        except Exception:
            pass

    @work(thread=True, name="worker")
    def start_worker(self):
        self.app.call_from_thread(self.log_system, f"Starting worker for: {self.project_name}")
        cmd = [sys.executable, "-u", WORKER_SCRIPT, self.project_name]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=os.path.dirname(PROJECTS_FILE),
                bufsize=1,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            )
            for line in self._proc.stdout:
                self.app.call_from_thread(self.log_line, line)
            self._proc.wait()
            self.app.call_from_thread(self.log_system, f"Worker exited (code {self._proc.returncode})")
        except Exception as e:
            self.app.call_from_thread(self.log_system, f"[red]Worker error: {e}[/red]")

    def pause_worker(self):
        """Update projects.json to paused — worker will detect and stop."""
        with open(PROJECTS_FILE) as f:
            projects = json.load(f)
        for p in projects:
            if p["name"] == self.project_name and p["status"] in ("in_progress", "improving"):
                p["status"] = "paused"
                self._status = "paused"
                break
        with open(PROJECTS_FILE, "w") as f:
            json.dump(projects, f, indent=2)
        self.log_system("Pausing... (will stop after current task completes)")
        self.update_header()

    def resume_worker(self):
        """Update projects.json to in_progress and restart subprocess."""
        with open(PROJECTS_FILE) as f:
            projects = json.load(f)
        for p in projects:
            if p["name"] == self.project_name and p["status"] == "paused":
                p["status"] = "in_progress"
                self._status = "in_progress"
                break
        with open(PROJECTS_FILE, "w") as f:
            json.dump(projects, f, indent=2)
        self.update_header()
        self.log_system("Resuming worker...")
        self.start_worker()

    def takeover(self):
        """Open an interactive claude session in a new Windows Terminal tab."""
        project_dir = get_project_dir(self.project)
        try:
            subprocess.Popen([
                "wt", "-w", "0", "new-tab",
                "--title", f"[MANUAL] {self.project_name}",
                "-d", project_dir,
                "--", "claude", "--continue",
            ])
            self.log_system("[green]Opened interactive Claude session in new tab.[/green]")
            self.log_system("[dim]Switch to that tab to chat. Come back here when done.[/dim]")
        except Exception as e:
            self.log_system(f"[red]Could not open tab: {e}[/red]")

    def steer(self, message: str):
        """Inject a steering message as a new task override."""
        with open(PROJECTS_FILE) as f:
            projects = json.load(f)
        for p in projects:
            if p["name"] == self.project_name:
                p["next_task"] = f"[STEERED BY USER] {message}"
                break
        with open(PROJECTS_FILE, "w") as f:
            json.dump(projects, f, indent=2)
        self.log_system(f"[bold yellow]>>> Steered: {message}[/bold yellow]")
        self.log_system("[dim]Will pick up on next task cycle.[/dim]")


# --- Main App ---
class AgentMonitor(App):
    TITLE = "AgentLoop Monitor"
    SUB_TITLE = "Watch and steer your AI workers"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_status", "Refresh"),
        Binding("p", "pause_all", "Pause All"),
    ]

    def __init__(self, projects: list, **kwargs):
        super().__init__(**kwargs)
        self.projects = projects

    def compose(self) -> ComposeResult:
        yield Header()
        n = len(self.projects)
        cols = 1 if n == 1 else 2 if n <= 6 else 3

        for i in range(0, len(self.projects), cols):
            row_projects = self.projects[i:i + cols]
            with Horizontal():
                for p in row_projects:
                    safe = re.sub(r"[^a-z0-9]", "-", p["name"].lower())
                    yield ProjectPanel(p, id=f"panel-{safe}")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        for p in self.projects:
            safe = re.sub(r"[^a-z0-9]", "-", p["name"].lower())
            panel = self.query_one(f"#panel-{safe}", ProjectPanel)
            if btn_id == f"btn-pause-{safe}":
                panel.pause_worker()
            elif btn_id == f"btn-resume-{safe}":
                panel.resume_worker()
            elif btn_id == f"btn-takeover-{safe}":
                panel.takeover()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        input_id = event.input.id or ""
        message = event.value.strip()
        if not message:
            return
        for p in self.projects:
            safe = re.sub(r"[^a-z0-9]", "-", p["name"].lower())
            if input_id == f"input-{safe}":
                panel = self.query_one(f"#panel-{safe}", ProjectPanel)
                panel.steer(message)
                event.input.clear()

    def action_pause_all(self):
        with open(PROJECTS_FILE) as f:
            projects = json.load(f)
        for p in projects:
            if p["status"] in ("in_progress", "improving"):
                p["status"] = "paused"
        with open(PROJECTS_FILE, "w") as f:
            json.dump(projects, f, indent=2)
        self.notify("All workers paused.")

    def action_refresh_status(self):
        self.notify("Status refreshed.")


# --- Entry point ---
def main():
    projects = load_projects()
    active = [p for p in projects if p["status"] in ("in_progress", "improving") and not p.get("blocked")]

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        try:
            count = int(arg)
            active = active[:count]
        except ValueError:
            matches = [p for p in projects if arg.lower() in p["name"].lower()]
            if matches:
                active = matches
            else:
                print(f"No project matching '{arg}'")
                return

    if not active:
        print("No active projects. Set status to 'in_progress' in projects.json.")
        return

    print(f"Starting monitor with {len(active)} worker(s)...")
    app = AgentMonitor(active)
    app.run()


if __name__ == "__main__":
    main()
