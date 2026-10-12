"""Collect the AWS API names the console's own code calls while the suite runs.

The root conftest installs `collect_recorded_aws_api_names` for the whole session; every call that reaches
`ExecutionRecorder.record_aws_call` is attributed to the first source file outside botocore, boto3 and the
recorder. Calls made from `rdscommander/` (but not from the simulator seeder, which only ever talks to the
simulated account) are the calls a real account must allow; `tests/aws/test_iam_policy.py` checks the
shipped IAM policy against them after every other test has run.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any

import boto3
import botocore

import rdscommander
from rdscommander.execution_records.service import ExecutionRecorder

PRODUCT_PACKAGE_DIRECTORY = Path(rdscommander.__file__).resolve().parent
SIMULATOR_SEEDER_DIRECTORY = PRODUCT_PACKAGE_DIRECTORY / "simulated"
RECORDING_MACHINERY_FILES = frozenset(
    {
        str(PRODUCT_PACKAGE_DIRECTORY / "aws" / "call_recorder.py"),
        str(PRODUCT_PACKAGE_DIRECTORY / "execution_records" / "service.py"),
        str(Path(__file__).resolve()),
    }
)
AWS_SDK_DIRECTORIES = tuple(str(Path(package.__file__).resolve().parent) for package in (botocore, boto3))

PRODUCT_AWS_API_NAMES: set[str] = set()
"""`service:Operation` names called from the console's own code during this session."""


def _calling_source_file(frame: FrameType | None) -> str | None:
    """The first source file on the stack that is neither the AWS SDK nor the recording machinery."""
    while frame is not None:
        filename = str(Path(frame.f_code.co_filename).resolve())
        if filename not in RECORDING_MACHINERY_FILES and not filename.startswith(AWS_SDK_DIRECTORIES):
            return filename
        frame = frame.f_back
    return None


def _is_product_call(source_file: str | None) -> bool:
    """True when `source_file` is console code that runs against real accounts."""
    if source_file is None:
        return False
    path = Path(source_file)
    return path.is_relative_to(PRODUCT_PACKAGE_DIRECTORY) and not path.is_relative_to(
        SIMULATOR_SEEDER_DIRECTORY
    )


@contextmanager
def collect_recorded_aws_api_names() -> Iterator[None]:
    """Wrap `ExecutionRecorder.record_aws_call` so each product call's API name is remembered.

    Example:
        >>> with collect_recorded_aws_api_names():  # doctest: +SKIP
        ...     run_the_suite()
        >>> "rds:DescribeDBInstances" in PRODUCT_AWS_API_NAMES  # doctest: +SKIP
        True
    """
    original_record_aws_call: Callable[..., int] = ExecutionRecorder.record_aws_call

    def record_and_remember(self: ExecutionRecorder, *, api_name: str, **keyword_arguments: Any) -> int:
        if _is_product_call(_calling_source_file(sys._getframe(1))):
            PRODUCT_AWS_API_NAMES.add(api_name)
        return original_record_aws_call(self, api_name=api_name, **keyword_arguments)

    ExecutionRecorder.record_aws_call = record_and_remember  # type: ignore[method-assign]
    try:
        yield
    finally:
        ExecutionRecorder.record_aws_call = original_record_aws_call  # type: ignore[method-assign]
