"""
AgentLoop Dashboard - Real-time monitoring UI for all workers
Run: python run.py
"""

import streamlit as st
import json
import os
import time
from datetime import datetime

PROJECTS_FILE = "projects.json"
LOG_DIR = "logs"
RUNTIME_STATUS_FILE = os.path.join(LOG_DIR, "runtime_status.json")
TAIL_LINES = 500  # how many lines to show per worker log

st.set_page_config(
    page_title="AgentLoop Dashboard",
    page_icon="=",
    layout="wide",
)

# --- Helpers ---
def load_projects():
    try:
        with open(PROJECTS_FILE) as f:
            return json.load(f)
    except Exception as e:
        st.error(f"Failed to load projects.json: {e}")
        return []

def save_projects(projects):
    with open(PROJECTS_FILE, "w") as f:
        json.dump(projects, f, indent=2)

def load_runtime_status():
    try:
        with open(RUNTIME_STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"updated_at": None, "workers": {}}

def heartbeat_age_text(ts):
    if not ts:
        return ""
    try:
        then = datetime.fromisoformat(ts)
    except Exception:
        return ts
    delta = datetime.now() - then
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"

def get_log_tail(project_name, n_lines=TAIL_LINES):
    safe_name = project_name.replace(" ", "_").replace("/", "_").lower()
    log_path = os.path.join(LOG_DIR, f"{safe_name}.log")
    if not os.path.exists(log_path):
        return "(no log yet)"
    try:
        with open(log_path, "rb") as f:
            raw = f.read()
        # Fix mojibake: replace UTF-8 byte sequences with ASCII equivalents
        replacements = {
            b"\xe2\x86\x92": b"->",       # arrow
            b"\xe2\x80\x93": b"-",        # en dash
            b"\xe2\x80\x94": b"--",       # em dash
            b"\xe2\x80\x99": b"'",        # right single quote
            b"\xe2\x80\x98": b"'",        # left single quote
            b"\xe2\x80\x9c": b'"',        # left double quote
            b"\xe2\x80\x9d": b'"',        # right double quote
            b"\xe2\x80\xa2": b"-",        # bullet
            b"\xc2\xb1": b"+/-",          # plus-minus
            b"\xe2\x89\xa5": b">=",       # greater-equal
            b"\xe2\x89\xa4": b"<=",       # less-equal
            b"\xe2\x80\xa6": b"...",      # ellipsis
        }
        for bad, good in replacements.items():
            raw = raw.replace(bad, good)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines(True)
        return "".join(lines[-n_lines:])
    except Exception as e:
        return f"(error reading log: {e})"

def get_log_size(project_name):
    safe_name = project_name.replace(" ", "_").replace("/", "_").lower()
    log_path = os.path.join(LOG_DIR, f"{safe_name}.log")
    if not os.path.exists(log_path):
        return 0
    return os.path.getsize(log_path)

# --- Load data ---
projects = load_projects()
runtime_status = load_runtime_status()

# --- Header ---
st.title("AgentLoop Orchestrator")

# --- Stats bar ---
counts = {}
for p in projects:
    s = p.get("status", "unknown")
    counts[s] = counts.get(s, 0) + 1

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("In Progress", counts.get("in_progress", 0))
col2.metric("Improving", counts.get("improving", 0))
col3.metric("Blocked", sum(1 for p in projects if p.get("blocked")))
col4.metric("Paused", counts.get("paused", 0))
col5.metric("Complete", counts.get("complete", 0))

st.divider()

runtime_workers = list((runtime_status.get("workers") or {}).values())
if runtime_workers:
    st.subheader("Runtime Workers")
    table_rows = []
    for worker in sorted(runtime_workers, key=lambda item: (item.get("project", ""), item.get("slot", ""))):
        table_rows.append({
            "Project": worker.get("project", ""),
            "Slot": worker.get("slot", ""),
            "Tool": worker.get("tool", ""),
            "Status": worker.get("status", ""),
            "Task": worker.get("task_id") or "",
            "Current task": worker.get("current_task") or "",
            "Heartbeat": heartbeat_age_text(worker.get("last_heartbeat_at")),
            "Last result": (worker.get("last_result") or "")[:120],
            "Last error": (worker.get("last_error") or "")[:120],
        })
    st.caption(f"Updated: {runtime_status.get('updated_at') or 'unknown'}")
    st.dataframe(table_rows, use_container_width=True, hide_index=True)
    st.divider()

# --- Controls ---
col_ctrl1, col_ctrl2, col_ctrl3 = st.columns(3)
with col_ctrl1:
    if st.button("Pause ALL workers"):
        for p in projects:
            if p["status"] in ("in_progress", "improving"):
                p["status"] = "paused"
        save_projects(projects)
        st.rerun()
with col_ctrl2:
    if st.button("Resume ALL workers"):
        for p in projects:
            if p["status"] == "paused":
                p["status"] = "in_progress"
        save_projects(projects)
        st.rerun()
with col_ctrl3:
    refresh_rate = st.selectbox("Auto-refresh", [3, 5, 10, 30, 0], index=2, format_func=lambda x: f"{x}s" if x else "Off")

# Log history slider
log_lines = st.slider("Log history (lines)", min_value=50, max_value=2000, value=500, step=50)

st.divider()

# --- Filter tabs ---
tab_active, tab_paused, tab_complete, tab_all = st.tabs(["Active Workers", "Paused", "Complete/Blocked", "All Projects"])

def render_worker_card(p, show_log=True, tab=""):
    """Render a single project/worker card."""
    key_suffix = f"{tab}_{p['name']}"
    status = p.get("status", "unknown")
    blocked = p.get("blocked", False)

    # Status badge
    waiting = p.get("wait_until")
    if blocked:
        badge = ":red[BLOCKED]"
    elif waiting:
        badge = ":orange[WAITING]"
    elif status == "in_progress":
        badge = ":green[WORKING]"
    elif status == "improving":
        badge = ":blue[IMPROVING]"
    elif status == "paused":
        badge = ":orange[PAUSED]"
    elif status == "complete":
        badge = ":gray[COMPLETE]"
    else:
        badge = f":gray[{status.upper()}]"

    with st.expander(f"{badge} **{p['name']}**", expanded=False):
        # Info row
        info_col1, info_col2 = st.columns([3, 1])
        with info_col1:
            st.markdown(f"**Next task:** {p.get('next_task', 'None')}")
            if blocked and p.get("blocker_description"):
                st.warning(f"Blocked: {p['blocker_description']}")
            if waiting:
                st.info(f"Waiting until: {waiting}")
        with info_col2:
            log_size = get_log_size(p["name"])
            if log_size > 0:
                st.caption(f"Log: {log_size / 1024:.1f} KB")

        # Controls
        btn_col1, btn_col2, btn_col3 = st.columns(3)
        with btn_col1:
            if status in ("in_progress", "improving"):
                if st.button("Pause", key=f"pause_{key_suffix}"):
                    p["status"] = "paused"
                    save_projects(projects)
                    st.rerun()
            elif status == "paused":
                if st.button("Resume", key=f"resume_{key_suffix}"):
                    p["status"] = "in_progress"
                    save_projects(projects)
                    st.rerun()
        with btn_col2:
            if blocked:
                if st.button("Unblock", key=f"unblock_{key_suffix}"):
                    p["blocked"] = False
                    p["blocker_description"] = None
                    if p["status"] == "complete":
                        p["status"] = "in_progress"
                    save_projects(projects)
                    st.rerun()

        # Recent log entries (collapsed by default to save space)
        if p.get("log"):
            with st.expander(f"Recent activity ({len(p['log'])} entries)", expanded=False):
                for entry in p["log"][-3:]:
                    st.text(f"  - {entry[:120]}")

        # Live log output
        if show_log:
            log_content = get_log_tail(p["name"], n_lines=log_lines)
            if log_content != "(no log yet)":
                st.caption(f"**Worker output (last {log_lines} lines):**")
                st.text_area(
                    "log",
                    value=log_content,
                    height=700,
                    key=f"log_{key_suffix}",
                    label_visibility="collapsed",
                )

# --- Active workers tab ---
with tab_active:
    active = [p for p in projects if p["status"] in ("in_progress", "improving") and not p.get("blocked")]
    if not active:
        st.info("No active workers. Resume some projects or start agent_loop.py.")
    for p in active:
        render_worker_card(p, show_log=True, tab="active")

# --- Paused tab ---
with tab_paused:
    paused = [p for p in projects if p["status"] == "paused"]
    if not paused:
        st.info("No paused projects.")
    for p in paused:
        render_worker_card(p, show_log=False, tab="paused")

# --- Complete/Blocked tab ---
with tab_complete:
    done = [p for p in projects if p["status"] in ("complete", "abandoned") or p.get("blocked")]
    if not done:
        st.info("No complete or blocked projects.")
    for p in done:
        render_worker_card(p, show_log=False, tab="done")

# --- All projects tab ---
with tab_all:
    for p in projects:
        render_worker_card(p, show_log=False, tab="all")

# --- Auto-refresh ---
if refresh_rate and refresh_rate > 0:
    time.sleep(refresh_rate)
    st.rerun()
