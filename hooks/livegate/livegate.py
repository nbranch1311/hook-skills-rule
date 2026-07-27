#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from livegate_hooks import handle_before_shell, handle_post_tool
from livegate_lifecycle import observe, observe_group_until


def main() -> int:
    try:
        if len(sys.argv) == 6:
            observer = observe_group_until if sys.argv[1] == "--observe-group" else observe
            timeout = int(sys.argv[5])
            deadline = time.monotonic() + timeout + 1
            while True:
                try:
                    return observer(
                        Path(sys.argv[2]),
                        sys.argv[3],
                        sys.argv[4],
                        max(1, int(deadline - time.monotonic())),
                    )
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)

        payload = json.loads(sys.stdin.read() or "{}")
        if payload.get("hook_event_name") == "beforeShellExecution":
            print(json.dumps(handle_before_shell(payload)))
        elif payload.get("hook_event_name") == "postToolUse":
            handle_post_tool(payload)
            print("{}")
        else:
            print("{}")
    except Exception as error:
        print(
            json.dumps(
                {
                    "permission": "allow",
                    "agent_message": (
                        f"LiveGate warning ({type(error).__name__}); command allowed."
                    ),
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
