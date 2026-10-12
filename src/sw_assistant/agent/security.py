from __future__ import annotations

import re
from typing import Any


MAX_AGENT_CONTEXT_BYTES = 200_000
_LOCAL_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\[^\\/\s]+[\\/])[^\r\n;；，。\"'<>]*")
_INLINE_SECRET = re.compile(
    r"(?i)\b(api[ _-]?key|authorization|password|secret|token)\s*[:=]\s*[^\s,;；]+"
)
_SECRET_KEYS = {"api_key", "authorization", "password", "secret", "token"}


def scrub_agent_value(value: Any, key: str = "") -> Any:
    lowered = key.casefold()
    if any(marker in lowered for marker in _SECRET_KEYS):
        return "<redacted>" if value else value
    if "path" in lowered:
        return "<local-path>" if value else value
    if isinstance(value, dict):
        return {
            str(child): scrub_agent_value(item, str(child))
            for child, item in value.items()
        }
    if isinstance(value, list):
        return [scrub_agent_value(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_agent_value(item) for item in value]
    if isinstance(value, str):
        without_secrets = _INLINE_SECRET.sub(r"\1=<redacted>", value)
        return _LOCAL_PATH.sub("<local-path>", without_secrets)
    return value
