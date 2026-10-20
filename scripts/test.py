"""Run isolated test groups; never load a real account or make model calls."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
# Child tests use only synthetic bindings; do not inherit an operator's identity/key.
env.update(BILI_LIFE_ID="fixture-agent", BILI_OWNER_UID="123")
env.pop("DASHSCOPE_API_KEY", None)
commands = [
    ["-m", "unittest", "discover", "-s", "social", "-p", "test_*.py"],
    ["-m", "unittest", "discover", "-s", "browse", "-p", "test_*.py"],
    ["-m", "unittest", "discover", "-s", "watch", "-p", "test_*.py"],
    ["-m", "unittest", "discover", "-s", "dm", "-p", "test_*.py"],
    ["-m", "unittest", "test_binding", "test_account_actions", "test_cli", "test_server"],
    ["verify.py"],
    ["scripts/smoke_stdio.py"],
]
for command in commands:
    print("Running:", " ".join(command), flush=True)
    subprocess.run([sys.executable, "-B", *command], cwd=ROOT, env=env, check=True)
print("All offline checks passed. No platform writes or paid model calls.")
