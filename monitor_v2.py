"""
AgentLoop Monitor v2 - One collapsible panel per project, each with a live Claude session.
Click a project header to expand it. Type in the input box to send messages to that Claude.

Requirements:
  pip install textual pywinpty

Usage:
  python monitor_v2.py              # All active projects
  python monitor_v2.py 3            # First 3
  python monitor_v2.py "my-project"     # One specific project
"""

import json
import os
import re
import sys
import threading
from datetime import datetime

try:
    from winpty import PtyProcess
    HAS_PTY = True
except ImportError:
    HAS_PTY = False

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Button, Footer, Header, Input, RichLog
from textual._work_decorator import work

PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")
AGENTLOOP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_BASE = os.environ.get("AGENTLOOP_PROJECTS_BASE") or os.getcwd()
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


# --- Helpers ---

def load_projects():
    with open(PROJECTS_FILE) as f:
        return json.load(f)


def get_project_dir(project):
    return os.path.join(PROJECTS_BASE, project.get("path", project["name"])).replace("\\", "/")


def startup_prompt(project):
    task = project.get("next_task", "Check CLAUDE.md and AGENTS.md for what to work on next")
    return (
        f"You are an autonomous coding agent for project: {project['name']}. "
        f"Current task: {task}. "
        f"Read CLAUDE.md and AGENTS.md first for full context. "
        f"Complete the task, then update your entry in {PROJECTS_FILE} "
        f"(set completed_task, update next_task, append to log). "
        f"Pick up the next task immediately and keep going autonomously at high effort. "
        f"If blocked: set blocked=true and blocker_description in projects.json. "
        f"The user may message you at any time to steer or redirect."
    )


def strip_ansi(text):
    return ANSI_RE.sub("", text)


# --- PTY Session ---

class PtySession:
    """Wraps a PTY running an interactive claude session."""

    def __init__(self, project, on_line, on_status):
        self.project = project
        self.on_line = on_line      # callback(str) — called from background thread
        self.on_status = on_status  # callback(str) — "running" | "idle" | "error"
        self._proc = None
        self._buf = ""
        self.running = False

    def start(self):
        cmd = [
            "claude", "--model", "sonnet",
            "--add-dir", AGENTLOOP_DIR,
            startup_prompt(self.project),
        ]
        try:
            self._proc = PtyProcess.spawn(
                cmd,
                cwd=get_project_dir(self.project),
                dimensions=(50, 220),
            )
            self.running = True
            self.on_status("running")
            threading.Thread(target=self._read_loop, daemon=True).start()
        except Exception as e:
            self.on_line(f"[red]Failed to start session: {e}[/red]")
            self.on_status("error")

    def _read_loop(self):
        while self._proc and self._proc.isalive():
            try:
                chunk = self._proc.read(2048)
                text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
                self._buf += text
                self._drain()
            except EOFError:
                break
            except Exception:
                break
        self.running = False
        self.on_status("idle")
        self.on_line("[dim]--- session ended ---[/dim]")

    def _drain(self):
        """Emit complete lines from the buffer."""
        while True:
            for sep in ("\r\n", "\n", "\r"):
                if sep in self._buf:
                    line, self._buf = self._buf.split(sep, 1)
                    clean = strip_ansi(line).strip()
                    if len(clean) > 1:  # skip single-char noise
                        self.on_line(clean)
                    break
            else:
                break  # no separator found, wait for more data

    def send(self, text: str):
        """Send a message to the session as if the user typed it."""
        if self._proc and self._proc.isalive():
            self._proc.write(text + "\r")

    def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self.running = False


# --- Project Panel ---

class ProjectPanel(Vertical):
    DEFAULT_CSS = """
    ProjectPanel {
        border: solid $primary-darken-2;
        height: auto;
        margin-bottom: 1;
    }
    ProjectPanel:focus-within {
        border: solid $accent;
    }
    ProjectPanel #header {
        width: 100%;
        height: 1;
        background: $primary-darken-3;
        border: none;
        text-align: left;
        padding: 0 1;
        color: $text;
    }
    ProjectPanel #header:hover {
        background: $primary-darken-1;
    }
    ProjectPanel #body {
        height: 24;
    }
    ProjectPanel RichLog {
        height: 21;
    }
    ProjectPanel Input {
        height: 3;
        border: tall $primary-darken-2;
    }
    """

    def __init__(self, project: dict, **kwargs):
        super().__init__(**kwargs)
        self.project = project
        self._name = project["name"]
        self._expanded = False
        self._status = "idle"
        self._session: PtySession | None = None

    def compose(self) -> ComposeResult:
        short = self._name[:50] + "…" if len(self._name) > 50 else self._name
        yield Button(f"► {short}  [dim]IDLE[/dim]", id="header")
        with Vertical(id="body"):
            yield RichLog(markup=True, wrap=True)
            yield Input(placeholder="Type a message and press Enter to send to Claude...")

    def on_mount(self):
        # Start collapsed
        self.query_one("#body").display = False
        # Launch PTY in a worker thread so call_from_thread works correctly
        self._launch()

    @work(thread=True, name="pty")
    def _launch(self):
        self._session = PtySession(
            self.project,
            on_line=lambda line: self.app.call_from_thread(self._add_line, line),
            on_status=lambda s: self.app.call_from_thread(self._set_status, s),
        )
        self._session.start()

    def _add_line(self, line: str):
        try:
            self.query_one(RichLog).write(line)
        except Exception:
            pass

    def _set_status(self, status: str):
        self._status = status
        self._refresh_header()

    def _refresh_header(self):
        arrow = "▼" if self._expanded else "►"
        short = self._name[:50] + "…" if len(self._name) > 50 else self._name
        color = {"running": "green", "idle": "dim", "error": "red"}.get(self._status, "dim")
        try:
            self.query_one("#header", Button).label = (
                f"{arrow} {short}  [{color}]{self._status.upper()}[/{color}]"
            )
        except Exception:
            pass

    def toggle(self):
        self._expanded = not self._expanded
        self.query_one("#body").display = self._expanded
        self._refresh_header()
        if self._expanded:
            # Focus the input when expanding
            try:
                self.query_one(Input).focus()
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "header":
            self.toggle()

    def on_input_submitted(self, event: Input.Submitted):
        msg = event.value.strip()
        if not msg:
            return
        if self._session:
            self._session.send(msg)
            self._add_line(f"[bold yellow]>>> {msg}[/bold yellow]")
        event.input.clear()

    def on_unmount(self):
        if self._session:
            self._session.stop()


# --- Main App ---

class AgentMonitor(App):
    TITLE = "AgentLoop Monitor"
    SUB_TITLE = "Click a project to expand it. Type to send messages directly to that Claude."

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "pause_all", "Pause All"),
        Binding("e", "expand_all", "Expand All"),
        Binding("c", "collapse_all", "Collapse All"),
    ]

    def __init__(self, projects: list, **kwargs):
        super().__init__(**kwargs)
        self._projects = projects

    def compose(self) -> ComposeResult:
        yield Header()
        with ScrollableContainer():
            for p in self._projects:
                sid = re.sub(r"[^a-z0-9]", "-", p["name"].lower())
                yield ProjectPanel(p, id=f"panel-{sid}")
        yield Footer()

    def action_pause_all(self):
        with open(PROJECTS_FILE) as f:
            projects = json.load(f)
        for p in projects:
            if p["status"] in ("in_progress", "improving"):
                p["status"] = "paused"
        with open(PROJECTS_FILE, "w") as f:
            json.dump(projects, f, indent=2)
        self.notify("All projects paused in projects.json.")

    def action_expand_all(self):
        for panel in self.query(ProjectPanel):
            if not panel._expanded:
                panel.toggle()

    def action_collapse_all(self):
        for panel in self.query(ProjectPanel):
            if panel._expanded:
                panel.toggle()


# --- Entry point ---

def main():
    if not HAS_PTY:
        print("ERROR: pywinpty is required.")
        print("Install it with:  pip install pywinpty")
        print("Then re-run:      python monitor_v2.py")
        sys.exit(1)

    projects = load_projects()
    now = datetime.now().isoformat()
    active = [
        p for p in projects
        if p["status"] in ("in_progress", "improving")
        and not p.get("blocked")
        and (not p.get("wait_until") or p["wait_until"] < now)
    ]

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        try:
            active = active[:int(arg)]
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

    print(f"Starting monitor with {len(active)} project(s)...")
    AgentMonitor(active).run()


if __name__ == "__main__":
    main()
