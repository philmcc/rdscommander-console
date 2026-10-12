"""Render Python values as HCL literals."""

from __future__ import annotations

import re
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _key(key: str) -> str:
    return key if _IDENTIFIER.match(key) else to_hcl(key)


def to_hcl(value: Any, indent: int = 2) -> str:
    """Return an HCL literal; strings escape quotes, backslashes, `${` and `%{`."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("${", "$${")
            .replace("%{", "%%{")
            .replace("\n", "\\n")
        )
        return f'"{escaped}"'
    if isinstance(value, list | tuple):
        return "[" + ", ".join(to_hcl(item, indent) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        keys = {k: _key(str(k)) for k in value}
        width = max(len(v) for v in keys.values())
        pad = " " * (indent + 2)
        lines = [
            f"{pad}{keys[k].ljust(width)} = {to_hcl(value[k], indent + 2)}" for k in sorted(value, key=str)
        ]
        return "{\n" + "\n".join(lines) + "\n" + " " * indent + "}"
    raise TypeError(f"cannot render {type(value).__name__} as HCL")
