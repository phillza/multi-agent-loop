"""Helper: reads prompt from file and launches interactive claude with it."""
import os
import sys

prompt_file = sys.argv[1]
add_dir = sys.argv[2]

with open(prompt_file, encoding="utf-8") as f:
    prompt = f.read().strip()

# Use -- to separate flags from the positional prompt argument
args = ["claude", "--model", "sonnet", "--add-dir", add_dir, "--", prompt]
os.execlp("claude", *args)
