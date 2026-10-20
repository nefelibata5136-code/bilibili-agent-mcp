"""Conservative public-tree audit; never reads ignored runtime/credential state."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
try:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    paths = [ROOT / p for p in result.stdout.decode("utf-8").split("\0") if p]
except (OSError, subprocess.CalledProcessError):
    paths = []
if not paths:
    paths = [p for p in ROOT.rglob("*") if p.is_file() and not any(part in {
        ".git", ".venv", "__pycache__", "build", "dist", "state"} or part.endswith((".egg-info", "-state")) for part in p.relative_to(ROOT).parts)]
errors = []
for path in paths:
    name = path.name.lower()
    if name in {"bili_credential.json", "cookies.txt", ".env"} or path.suffix.lower() in {
        ".sqlite3", ".sqlite", ".db", ".log", ".mp4", ".m4a", ".webm", ".png", ".jpg", ".zip"}:
        errors.append((str(path.relative_to(ROOT)), "runtime/private artifact"))
        continue
    text = path.read_text(encoding="utf-8")
    for expression, label in [
        (r"(?i)[a-z]:[/\\]Users[/\\][^\s\"']+", "personal absolute path"),
        (r"sk-[A-Za-z0-9_-]{24,}", "possible API key"),
        (r"gh[pousr]_[A-Za-z0-9]{30,}", "possible GitHub token"),
        (r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", "private key"),
    ]:
        if re.search(expression, text):
            errors.append((str(path.relative_to(ROOT)), label))
if errors:
    for name, label in errors:
        print(name, label)
    raise SystemExit(1)
print(f"Public-tree audit passed: {len(paths)} text files; no private/runtime artifacts or recognized secrets.")
