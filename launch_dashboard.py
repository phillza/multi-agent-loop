# run.py - Launch the AgentLoop dashboard on a free port
import socket
import subprocess
import sys

START_PORT = 8501
END_PORT = 8600

def find_free_port(start, end):
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) != 0:
                return port
    raise RuntimeError(f"No free port found between {start} and {end}")

if __name__ == "__main__":
    port = find_free_port(START_PORT, END_PORT)
    print(f"Starting AgentLoop dashboard on http://localhost:{port}")
    subprocess.run([
        sys.executable, "-m", "streamlit", "run", "dashboard.py",
        "--server.port", str(port),
        "--server.headless", "true",
    ])
