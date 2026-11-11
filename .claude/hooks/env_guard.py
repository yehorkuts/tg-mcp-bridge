#!/usr/bin/env python3
"""Secret guard for tg-mcp-bridge.

Hook mode (no arguments): Claude Code PreToolUse hook. Reads the tool call JSON
from stdin and blocks (exit 2) any call that touches `.env` or `.env.*`
(except `.env.example`), and any write to `.env.example` that contains
real-looking secrets instead of placeholders.

CLI mode (used by CI and pre-commit):
  python .claude/hooks/env_guard.py --check .env.example   # placeholders only
  python .claude/hooks/env_guard.py --check-repo           # no .env files tracked by git
"""
import json
import os
import re
import subprocess
import sys

ALLOWED_NAME = ".env.example"

# Keys whose value in .env.example must be empty or a <placeholder>.
PLACEHOLDER_KEYS = {"BOT_TOKEN", "TG_WEBHOOK_SECRET", "ADMIN_TG_ID"}
PLACEHOLDER_RE = re.compile(r"^(|<[^<>]+>)$")

# Real-looking secrets that must never appear in .env.example.
SECRET_PATTERNS = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "Telegram bot token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"), "GitHub token"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "Anthropic API key"),
]

# A `.env` or `.env.<suffix>` token inside a shell command or a glob.
ENV_TOKEN_RE = re.compile(
    r"(?:^|(?<=[\s/\"'=:<>|;&(*]))(\.env(?:\.[A-Za-z0-9_-]+)?)(?=$|[\s\"'/;&|)<>*])"
)


def is_forbidden_name(path: str) -> bool:
    name = os.path.basename(path.rstrip("/"))
    if name == ALLOWED_NAME:
        return False
    return name == ".env" or name.startswith(".env.")


def forbidden_tokens(text: str) -> list[str]:
    return [t for t in ENV_TOKEN_RE.findall(text or "") if t != ALLOWED_NAME]


def check_env_example_text(text: str) -> list[str]:
    """Return a list of problems found in .env.example content (full file or a fragment)."""
    problems = []
    for label_re, label in SECRET_PATTERNS:
        if label_re.search(text):
            problems.append(f"looks like a real {label}")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if key in PLACEHOLDER_KEYS and not PLACEHOLDER_RE.match(value):
            problems.append(f"{key} must be empty or a <placeholder>, got a real-looking value")
    return problems


def block(reason: str) -> None:
    print(f"Blocked by env_guard: {reason}", file=sys.stderr)
    sys.exit(2)


def run_hook() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # not our business if the payload is unreadable

    tool = event.get("tool_name", "")
    data = event.get("tool_input") or {}

    if tool == "Bash":
        bad = forbidden_tokens(data.get("command", ""))
        if bad:
            block(f"shell commands must not touch {', '.join(sorted(set(bad)))}. "
                  "Real secrets live only on the server. Use .env.example for local runs.")
        sys.exit(0)

    if tool in ("Grep", "Glob"):
        for field in ("path", "glob", "pattern"):
            bad = forbidden_tokens(str(data.get(field, "")))
            if bad:
                block(f"{tool} must not target {', '.join(sorted(set(bad)))}.")
        sys.exit(0)

    path = data.get("file_path") or data.get("notebook_path") or ""
    if path and is_forbidden_name(path):
        block(f"{tool} on {os.path.basename(path)} is not allowed. Only .env.example may be read or edited.")

    if path and os.path.basename(path) == ALLOWED_NAME and tool in ("Write", "Edit", "MultiEdit"):
        if tool == "Write":
            text = data.get("content", "")
        elif tool == "Edit":
            text = data.get("new_string", "")
        else:
            text = "\n".join(e.get("new_string", "") for e in data.get("edits", []))
        problems = check_env_example_text(text)
        if problems:
            block(".env.example must contain placeholders only: " + "; ".join(problems))

    sys.exit(0)


def run_check(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        problems = check_env_example_text(f.read())
    for p in problems:
        print(f"{path}: {p}", file=sys.stderr)
    return 1 if problems else 0


def run_check_repo() -> int:
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    tracked = [f for f in files if is_forbidden_name(f)]
    for f in tracked:
        print(f"tracked secret file: {f}", file=sys.stderr)
    return 1 if tracked else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        sys.exit(run_check(sys.argv[2]))
    if len(sys.argv) == 2 and sys.argv[1] == "--check-repo":
        sys.exit(run_check_repo())
    run_hook()
