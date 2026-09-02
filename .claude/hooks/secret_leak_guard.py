#!/usr/bin/env python3
"""
PostToolUse hook: scans a Bash command's OUTPUT after it has already run,
looking for leaked tokens.

Important limitation, be clear-eyed about this: this hook fires AFTER the
tool already executed and its output is already in the transcript. Exiting 2
does not erase or redact anything already shown. What it does do:
  - Immediately surfaces a loud warning so you notice and rotate the token
  - Writes an audit log entry with a timestamp so you have a record

The Claude Code JSON schema for what field holds the tool's output has
shifted across versions and different docs disagree on the exact key. This
script checks several possible keys defensively instead of hardcoding one,
so it doesn't silently stop working if the field name changes again.

Log location: .claude/hooks/secret_leak_guard.log (create the hooks dir
first, this appends there).
"""

import json
import os
import re
import sys
from datetime import UTC, datetime

HEX64_PATTERN = re.compile(r"\b[0-9a-fA-F]{64}\b")

LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "secret_leak_guard.log"
)


def extract_output(payload: dict[str, object]) -> str:
    """Pull the tool's stdout/stderr/output text out of whatever field the
    current Claude Code version actually uses. Checks multiple candidates."""
    candidates: list[object] = []

    for key in ("tool_response", "tool_output", "output", "result"):
        val = payload.get(key)
        if val is not None:
            candidates.append(val)

    text_chunks: list[str] = []
    for val in candidates:
        if isinstance(val, str):
            text_chunks.append(val)
        elif isinstance(val, dict):
            for subkey in ("stdout", "stderr", "output", "content", "text"):
                sub = val.get(subkey)
                if isinstance(sub, str):
                    text_chunks.append(sub)
    return "\n".join(text_chunks)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    if not isinstance(payload, dict):
        sys.exit(0)

    if payload.get("tool_name") != "Bash":
        sys.exit(0)

    output_text = extract_output(payload)
    if not output_text:
        sys.exit(0)

    matches = HEX64_PATTERN.findall(output_text)
    if not matches:
        sys.exit(0)

    # Log it, redacting all but the last 4 chars so the log itself isn't a
    # second leak
    redacted = [f"...{m[-4:]}" for m in matches]
    timestamp = datetime.now(UTC).isoformat()
    command = payload.get("tool_input", {}).get("command", "<unknown>")

    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{timestamp} | command: {command} | tokens found: {redacted}\n")
    except OSError:
        pass  # don't crash the hook over a log-write failure

    print(
        f"WARNING: possible secret token(s) detected in command output "
        f"({len(matches)} match(es), tails: {redacted}). This already ran and "
        f"the output is already visible. Rotate the token now and check "
        f"{LOG_PATH} for the record.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
