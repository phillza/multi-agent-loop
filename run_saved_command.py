"""
run_saved_command.py - Execute a saved command list inside the current terminal.

This avoids Windows Terminal argument parsing issues with long multiline prompts.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def resolve_windows_command(executable: str) -> str:
    appdata_cmd = Path(os.environ.get("APPDATA", "")) / "npm" / f"{executable}.cmd"
    if appdata_cmd.exists():
        return str(appdata_cmd)

    for suffix in (".cmd", ".exe"):
        resolved = shutil.which(f"{executable}{suffix}")
        if resolved:
            return resolved

    resolved = shutil.which(executable)
    return resolved or executable


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a saved command list.")
    parser.add_argument("command_file", help="Path to JSON file containing a command array")
    args = parser.parse_args()

    command_path = Path(args.command_file)
    payload = json.loads(command_path.read_text(encoding="utf-8"))
    cmd = payload["cmd"]
    cwd = payload.get("cwd")

    if cmd:
        cmd[0] = resolve_windows_command(cmd[0])

    completed = subprocess.run(cmd, cwd=cwd, check=False)
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
