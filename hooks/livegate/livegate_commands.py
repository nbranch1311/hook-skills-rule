from __future__ import annotations

import hashlib
import re
import shlex
import time
from pathlib import Path
from typing import Any

ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
OUTPUT_ASSIGNMENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
SENSITIVE_FLAG = re.compile(r"(?i)^--?(?:api-)?(?:token|key|secret|password|authorization)$")
SENSITIVE_INLINE = re.compile(
    r"(?i)^(--?(?:api-)?(?:token|key|secret|password|authorization))=(.+)$"
)
SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:api_?)?(?:token|key|secret|password|authorization)=)[^&\s]+"
)
BEARER = re.compile(r"(?i)\bBearer\s+\S+")


def command_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    while tokens and ENVIRONMENT_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    return tokens


def normalize_command(command: str) -> str:
    return shlex.join(command_tokens(command))


def display_command(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "<unparseable command>"
    redacted: list[str] = []
    hide_next = False
    for token in tokens:
        inline = SENSITIVE_INLINE.match(token)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
        elif ENVIRONMENT_ASSIGNMENT.match(token):
            redacted.append(f"{token.split('=', 1)[0]}=<redacted>")
        elif inline:
            redacted.append(f"{inline.group(1)}=<redacted>")
        else:
            redacted.append(SENSITIVE_QUERY.sub(r"\1<redacted>", token))
            hide_next = bool(SENSITIVE_FLAG.match(token))
    return redact_text(shlex.join(redacted))


def redact_text(value: str) -> str:
    value = OUTPUT_ASSIGNMENT.sub(r"\1=<redacted>", value)
    value = SENSITIVE_QUERY.sub(r"\1<redacted>", value)
    return BEARER.sub("Bearer <redacted>", value)


def command_hash(command: str) -> str:
    return hashlib.sha256(normalize_command(command).encode()).hexdigest()


def requested_second(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    prefix = "LIVEGATE_SECOND="
    return next(
        (token[len(prefix) :] for token in tokens if token.startswith(prefix)),
        None,
    )


def approval_matches(
    approval: dict[str, Any] | None,
    workspace: Path,
    application_id: str,
    command_fingerprint: str,
    session: str,
) -> bool:
    return bool(
        approval
        and approval.get("workspace") == str(workspace)
        and approval.get("applicationId") == application_id
        and approval.get("commandHash") == command_fingerprint
        and approval.get("session") == session
        and approval.get("expiresAt", 0) > time.time()
    )
