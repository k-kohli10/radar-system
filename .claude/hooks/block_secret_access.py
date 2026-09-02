#!/usr/bin/env python3
"""
PreToolUse hook: blocks Bash commands BEFORE they run if they look like they'd
deliberately expose a secret.

This runs pre-execution, so it can only inspect the command text itself, not
any output. It catches two things:
  1. Commands that cat/print/echo a known secrets file (.env, credentials.json, etc)
  2. Commands that have a hardcoded 64-char hex token pasted directly into them

Exit code 2 = block the tool call. Exit code 0 = allow it through.
"""

import json
import re
import sys

# Files that should never be dumped to stdout via a shell command.
# Add project-specific paths here.
SENSITIVE_FILE_PATTERNS = [
    r"\.env(\.\w+)?\b",
    r"credentials\.json\b",
    r"secrets?\.ya?ml\b",
    r"\bid_rsa\b",
    r"\.pem\b",
    r"gcloud/legacy_credentials",
    r"\.aws/credentials\b",
]

# Commands that dump file contents to stdout/terminal
DUMP_COMMANDS = r"(cat|less|more|head|tail|bat|type|printenv|env|export\s+-p)"

# A hardcoded 64-char hex token typed directly into a command line
HEX64_PATTERN = re.compile(r"\b[0-9a-fA-F]{64}\b")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed input from Claude Code itself, don't block on our own bug
        sys.exit(0)

    if not isinstance(payload, dict):
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    # Check 1: hardcoded hex-64 token in the command itself
    if HEX64_PATTERN.search(command):
        print(
            "BLOCKED: command contains what looks like a hardcoded 64-char "
            "hex token. Do not paste secrets directly into shell commands.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Check 2: command tries to dump a known secrets file
    dump_match = re.search(DUMP_COMMANDS, command)
    if dump_match:
        for pattern in SENSITIVE_FILE_PATTERNS:
            if re.search(pattern, command):
                print(
                    f"BLOCKED: command appears to print a sensitive file "
                    f"(matched pattern: {pattern}). If you need to check a "
                    f"specific value, grep for just that key instead of "
                    f"dumping the whole file.",
                    file=sys.stderr,
                )
                sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
