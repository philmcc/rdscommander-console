"""The test toolchain imports without deprecation warnings, so the suite's output stays clean."""

import subprocess
import sys


def test_the_starlette_test_client_imports_without_a_deprecation_warning() -> None:
    # Starlette 1.7 warns "Using `httpx` with `starlette.testclient` is deprecated; install `httpx2`
    # instead." when only httpx is installed; that was the one warning in every backend test run.
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import fastapi.testclient"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-600:]
