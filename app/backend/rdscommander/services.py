"""Services handed to every step. Fields are optional so tests can pass only what they need."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ServiceBundle:
    """Shared services for executors; narrowed types live in the modules that build them."""

    execution_recorder: Any | None = None
    aws_sessions: Any | None = None
    terraform_runner: Any | None = None
    session_factory: Any | None = None
    """Control database sessions, so that an executor can queue a follow-up operation."""
    sleep: Callable[[float], None] = time.sleep
    """Waits between polls of AWS; tests replace it so that no test really waits."""
