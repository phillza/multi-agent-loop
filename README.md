# AgentLoop (multi-agent-loop)

> Autonomous multi-project agent loop. Launch multiple AI workers in
> parallel terminal tabs, each running against a project in your
> `projects.json` queue. The workers read the project's `AGENTS.md` /
> `CLAUDE.md`, execute the `next_task`, then write back the result and
> pick up the next task — forever.

[![CI](https://github.com/phillza/multi-agent-loop/actions/workflows/tests.yml/badge.svg)](https://github.com/phillza/multi-agent-loop/actions/workflows/tests.yml)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## What it does

You maintain a `projects.json` queue. Each entry has a `name`, a
`path` to a project directory on disk, a `status` (active / paused /
improving / complete / blocked), a `next_task`, and a list of `goals`.
AgentLoop reads that queue and launches a worker per active project in
its own terminal tab. Each worker:

1. Reads the project's own `AGENTS.md` / `CLAUDE.md` for context
2. Sends the `next_task` to a CLI (Claude, Codex, or OpenCode) with
   full tool access
3. Streams the model's thinking + tool calls back to the terminal live
4. Parses the final JSON the model returns
5. Updates the project entry in `projects.json` (new `next_task`,
   status, log entry, etc.)
6. Sleeps a few seconds, then loops

The result: you set the queue at night, wake up to log files full of
completed work and a fresh set of `next_task` values ready for the next
cycle.

## Who this is for

- You have 3-30 long-running projects that you want an AI to keep
  chipping away at while you sleep
- You want **parallel** execution (one agent per project, not one agent
  juggling all of them)
- You want **persistent state** — the agent can pick up where it left
  off across restarts because `projects.json` survives
- You want **direct observation** — every worker is a real terminal tab
  you can `Ctrl+C` into to take over

If you only have one project, this is overkill. Use Claude Code /
Codex / OpenCode directly.

## Requirements

- **Python 3.8+** (uses `from __future__ import annotations` style)
- **Windows Terminal** (`wt`) on Windows for the multi-tab launcher.
  On macOS / Linux the launcher falls back to spawning standalone
  terminal windows via `subprocess.Popen`.
- At least one of: **Claude Code** (`claude`), **Codex CLI** (`codex`),
  or **OpenCode** (`opencode`) installed and on `PATH`.
- **A working auth path for the tool you pick.** Specifically:
  - For **Claude Code**: either an active subscription *or*
    `ANTHROPIC_API_KEY` in your environment. If your organization
    has disabled subscription access, you must use the API key
    path. Without a working auth path, `claude --print` will
    return an auth-error message that the worker will correctly
    log as "Failed to parse JSON from response" and retry.
  - For **Codex CLI**: an active OpenAI auth path.
  - For **OpenCode**: a configured provider.

The repo has zero pip dependencies — it's stdlib only.

### Common first-run gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `FileNotFoundError: ... 'projects.json'` | You skipped the `cp` step in quick start | `cp projects.example.json projects.json` then edit |
| `Failed to parse JSON from Claude's response. Retrying...` (forever) | Auth path is broken — Claude/Codex/OpenCode rejected the call | Check `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / provider config |
| Workers launch but immediately exit | CLI tool not on `PATH` or wrong name | `where claude` / `where codex` / `where opencode` on Windows; `which` on macOS/Linux |

## Quick start

```bash
# 1. Clone
git clone https://github.com/phillza/multi-agent-loop
cd multi-agent-loop

# 2. Create your project queue
cp projects.example.json projects.json
# edit projects.json to point at your real project directories

# 3. Set the base directory your projects live under
export AGENTLOOP_PROJECTS_BASE=/path/to/your/projects
# (Windows: setx AGENTLOOP_PROJECTS_BASE "C:\path\to\your\projects")

# 4. Launch workers for every active project
python agent_loop.py

# 5. Or pick one
python agent_loop.py "My Project"

# 6. Or open the web dashboard
python monitor_web.py
# (open the URL it prints in your browser)
```

The first time you run it, the workers will look at each project's
`next_task` and start. Subsequent runs pick up from where the previous
one left off because state lives in `projects.json`.

## Scripts overview

| Script | What it does | Best for |
|---|---|---|
| `agent_loop.py` | Launches workers for every active project in separate tabs | Overnight autonomous runs across many projects |
| `agent_loop_interactive.py` | Launches interactive CLI sessions per project (not auto-looping) | Manual session, you drive |
| `agent_loop_split.py` | All sessions in a single tab as split panes | One screen, all sessions visible |
| `worker.py "Name"` | Single non-interactive worker in current terminal | Manual single-project run |
| `interactive_worker.py` | Python-controlled chunk loop with mid-task steering | Steering without walkie-talkie infra |
| `monitor.py` | Textual TUI — all workers in one terminal | Quick terminal overview |
| `monitor_web.py` | Web dashboard — browser UI with start/stop/steer | Best overall management |
| `steer.py` | CLI tool to set the next task for a running worker | Quick steering from any terminal |
| `runtime_supervisor.py` | Headless supervisor for autonomous runtime passes | Default autonomous launcher target |
| `auto_worker_pass.py` | One headless worker pass against one claimed task | Autonomous execution slot |
| `auto_strategist_pass.py` | One headless strategist pass to refill or refine tasks | Autonomous backlog upkeep |
| `launch_direct_session.py` | Opens one autonomous CLI session in its own tab for a single project | Direct "just work on this project" runs |
| `launch_orchestrator.py` | Launches global orchestrator session | Start the coordinator |
| `launch_team.py` | Launches project team: 1 orch + N workers | Multi-worker project |
| `launch_all.py` | Launches everything (teams + single workers + global orch) | Full system launch |
| `easy_agentloop.py` | Beginner-friendly menu + shortcuts | Easiest daily use |
| `dashboard.py` / `launch_dashboard.py` | Helper for opening the web dashboard | Port-finding wrapper |

## Architecture

### Non-interactive mode (default)

```
agent_loop.py
   |
   +--> Windows Terminal tab 1  -->  worker.py "Project A"
   +--> Windows Terminal tab 2  -->  worker.py "Project B"
   +--> Windows Terminal tab 3  -->  worker.py "Project C"
                                          |
                                          +--> read projects.json
                                          +--> read project's AGENTS.md/CLAUDE.md
                                          +--> spawn: claude --print --output-format stream-json ...
                                          +--> stream output to terminal + log file
                                          +--> parse final JSON line
                                          +--> update projects.json
                                          +--> sleep N seconds
                                          +--> loop
```

Workers auto-loop forever. Each task is a separate CLI invocation, so
if one crashes the next one starts cleanly.

### Runtime supervisor mode (recommended for overnight runs)

```
launch_all.py  -->  one supervisor tab
                        |
                        +--> runtime_supervisor.py  -->  polls runtime_status.json
                                                       +--> spawns auto_worker_pass.py slots
                                                       +--> spawns auto_strategist_pass.py when queue is dry
```

The supervisor is the always-on loop. Worker and strategist passes are
disposable one-shot jobs, so the system can relaunch them if they stop.

### Web dashboard

```
monitor_web.py  -->  FastAPI server (auto-finds a free port)
                          |
                          +--> reads logs/runtime_status.json
                          +--> polls runtime project summaries
                          +--> browser: passive view of queue depth, slot state, heartbeats
```

## Project status model

| Status | Worker behavior |
|---|---|
| `in_progress` | Executes `next_task` each cycle |
| `improving` | Runs the improvement-discovery prompt (longer delay between cycles) |
| `paused` | Waits and rechecks (won't launch new CLI calls) |
| `complete` | Skipped by launcher |
| `blocked` | Sleeps 60s, rechecks — waiting on human input |

`wait_until` (ISO datetime) lets a project skip itself until a future
time (e.g. "next match starts in 4 hours"). Zero tokens spent while
waiting.

## `projects.json` schema

```json
{
  "name": "Project Name",
  "path": "folder-name-under-projects-base",
  "status": "in_progress",
  "context": "One-sentence fallback (real context comes from the project's AGENTS.md/CLAUDE.md)",
  "next_task": "Specific task description",
  "goals": ["Goal 1", "Goal 2"],
  "blocked": false,
  "blocker_description": null,
  "wait_until": null,
  "log": []
}
```

`AGENTLOOP_PROJECTS_BASE` is the parent directory. The `path` field is
the subdirectory name under it. Set the env var, then `path` becomes
relative.

A real example lives in `projects.example.json` — copy it to
`projects.json` and edit.

## `teams.json` schema (optional)

For multi-worker projects (1 orchestrator + N workers), add an entry
keyed by the project name. Anything not in `teams.json` gets a single
worker. See `teams.json` for the full schema with `_defaults` and an
example.

## Configuration constants

`worker.py` exposes a few knobs at the top of the file:

| Constant | Default | Purpose |
|---|---|---|
| `WORKER_MODEL` | `"sonnet"` | Model for workers (passed to `--model`) |
| `COORDINATOR_TIMEOUT` | `600` | 10 min max per CLI call |
| `LOOP_DELAY_SECONDS` | `5` | Pause between task cycles |
| `IMPROVE_DELAY_SECONDS` | `120` | Pause between improvement cycles |
| `BLOCKED_RETRY_SECONDS` | `60` | How often to recheck blocked projects |

## Common operations

| You want to... | Do this |
|---|---|
| Add a new project | Append an entry to `projects.json` with `status: "in_progress"` |
| Pause a project | Set `"status": "paused"` in `projects.json` (or via dashboard/steer) |
| Unblock a project | Set `"blocked": false`, update `"next_task"` |
| Steer a running worker | `python steer.py "Project Name" "New task"` |
| Take over a worker tab | Press `Ctrl+C`, choose option 1 for interactive session |
| Open the dashboard | `python monitor_web.py` |

## Why a JSON queue instead of agent memory?

The model forgets. If the agent's context window resets, the next
session has no idea what it was doing. `projects.json` is the
durable memory: it survives crashes, restarts, model upgrades, even
switching between Claude / Codex / OpenCode.

## Limitations

- **No distributed runs.** Everything runs on one machine. The state
  file would need locking if you wanted to run multiple supervisors.
- **No worktree isolation.** If two workers accidentally target the
  same files, they'll race. `runtime_supervisor.py` does basic
  per-project slot tracking, but if your projects share files you
  need to be careful.
- **No LLM cost ceiling.** A bad prompt + a long-running task = an
  expensive night. Set up billing alerts in your CLI provider's
  dashboard.

## License

MIT — see [LICENSE](LICENSE).

## See also

- [`projects.example.json`](projects.example.json) — starter queue
- [`teams.json`](teams.json) — multi-worker team config schema
- [`.github/workflows/tests.yml`](.github/workflows/tests.yml) — CI
  smoke test
