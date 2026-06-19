"""
Steer a running interactive Claude session by setting its next task.
The session picks this up after completing its current task.

Usage:
  python steer.py "my-project"  "Focus on xG features next"
  python steer.py "Crypto"  "Stop what you're doing and fix the API auth bug"
  python steer.py list                              # show all projects and status
"""

import json
import os
import sys

PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects.json")


def load_projects():
    with open(PROJECTS_FILE) as f:
        return json.load(f)


def save_projects(projects):
    with open(PROJECTS_FILE, "w") as f:
        json.dump(projects, f, indent=2)


def list_projects():
    projects = load_projects()
    print("\n=== Projects ===")
    for p in projects:
        status = p.get("status", "?")
        task = (p.get("next_task") or "")[:80]
        blocked = " [BLOCKED]" if p.get("blocked") else ""
        print(f"  [{status.upper()}]{blocked}  {p['name']}")
        if task:
            print(f"    next: {task}")
    print()


def steer(name_query, message):
    projects = load_projects()
    matches = [p for p in projects if name_query.lower() in p["name"].lower()]

    if not matches:
        print(f"No project matching '{name_query}'")
        return

    if len(matches) > 1:
        print(f"Multiple matches for '{name_query}':")
        for m in matches:
            print(f"  - {m['name']}")
        print("Be more specific.")
        return

    project = matches[0]
    project["next_task"] = f"[USER STEERED] {message}"

    save_projects(projects)
    print(f"Steered '{project['name']}'")
    print(f"  next_task = [USER STEERED] {message}")
    print(f"  (will pick up after current task completes)")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print('  python steer.py list')
        print('  python steer.py "Project Name" "Your message here"')
        return

    if sys.argv[1].lower() == "list":
        list_projects()
        return

    if len(sys.argv) < 3:
        print('Usage: python steer.py "Project Name" "Your message"')
        return

    steer(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
