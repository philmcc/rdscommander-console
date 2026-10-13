"""Validation errors as sentences: the `problems` of a 422 answer, and the handler for malformed requests.

Pydantic words its errors for programmers ("String should match pattern '^[a-z]...$'", "Value error, ...",
"Field required"). Every 422 the console answers carries `{"detail": "<one sentence>", "problems":
["<field path>: <sentence>", ...]}` instead, the same shape as a refused operation request, so the frontend
shows the problems as written and puts each under the field it names.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic_core import ErrorDetails

from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)

PATH_SEPARATOR = " -> "
"""Separates the parts of a field path, e.g. `overrides -> instance`; the frontend splits on it."""
REQUEST_LOCATIONS = frozenset({"path", "query", "body", "header", "cookie"})
"""The first part of a request error's location, which the field path leaves out."""
UNKNOWN_OPTION_SENTENCE = "is not an option of this task"
UNKNOWN_REQUEST_FIELD_SENTENCE = "is not a field of this request"
EXAMPLE_UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
PYDANTIC_PREFIXES = ("Value error, ", "Assertion failed, ")
"""Prefixes Pydantic puts before the message a validator raised; the message itself is already a sentence."""
PYDANTIC_SUBJECTS = (
    ("Input should be", "must be"),
    ("Input should have", "must have"),
    ("String should", "must"),
    ("List should", "must"),
    ("Dictionary should", "must"),
    ("Value should", "must"),
)
"""Openings of Pydantic messages and the words a sentence about a named field uses instead."""
TYPE_SENTENCES = {
    "missing": "is required",
    "string_type": "must be text",
    "bool_type": "must be true or false",
    "bool_parsing": "must be true or false",
    "int_type": "must be a whole number",
    "int_parsing": "must be a whole number",
    "int_from_float": "must be a whole number",
    "float_type": "must be a number",
    "float_parsing": "must be a number",
    "list_type": "must be a list",
    "dict_type": "must be an object of names and values",
    "model_type": "must be an object of names and values",
    "model_attributes_type": "must be an object of names and values",
    "uuid_type": f"must be a UUID such as {EXAMPLE_UUID}",
    "uuid_parsing": f"must be a UUID such as {EXAMPLE_UUID}",
}
"""Sentences for error types whose meaning does not depend on the error's context."""
FIRST_SENTENCE_END = re.compile(r"(?<=\.)\s")


def _pattern_description(help_text: str) -> str:
    """The first sentence of a field's help, as the end of "must match ...".

    Example:
        >>> _pattern_description("Up to 40 lowercase letters. Optional.")
        'up to 40 lowercase letters'
    """
    first_sentence = FIRST_SENTENCE_END.split(help_text.strip(), maxsplit=1)[0].rstrip(".")
    first_word = first_sentence.split(" ", 1)[0]
    if len(first_word) > 1 and first_word.isupper():
        return first_sentence  # an acronym such as AWS keeps its capitals
    return first_sentence[:1].lower() + first_sentence[1:]


def _count_sentence(verb: str, count: int, singular: str, plural: str) -> str:
    """E.g. `must have at least one entry`, `must be at most 255 characters long`."""
    noun = singular if count == 1 else plural
    return f"{verb} {'one' if count == 1 else count} {noun}"


def _sentence_from_context(
    error_type: str, context: dict[str, Any], field_hints: dict[str, Any] | None, unknown_field_sentence: str
) -> str | None:
    """The sentence for an error type whose wording depends on the error's context; None for other types.

    Raises:
        KeyError: The context lacks a value the type's sentence needs.
    """
    if error_type == "extra_forbidden":
        return unknown_field_sentence or UNKNOWN_REQUEST_FIELD_SENTENCE
    if error_type == "string_pattern_mismatch":
        help_text = str((field_hints or {}).get("help", "")).strip()
        return f"must match {_pattern_description(help_text) if help_text else context['pattern']}"
    if error_type == "string_too_short":
        if int(context["min_length"]) == 1:
            return "must not be empty"
        return (
            _count_sentence("must be at least", int(context["min_length"]), "character", "characters")
            + " long"
        )
    if error_type == "string_too_long":
        return (
            _count_sentence("must be at most", int(context["max_length"]), "character", "characters")
            + " long"
        )
    if error_type == "too_short":
        return _count_sentence("must have at least", int(context["min_length"]), "entry", "entries")
    if error_type == "too_long":
        return _count_sentence("must have at most", int(context["max_length"]), "entry", "entries")
    if error_type in ("literal_error", "enum"):
        return f"must be {context['expected']}"
    if error_type == "json_invalid":
        return f"the request body is not valid JSON ({context['error']})"
    comparisons = {
        "greater_than_equal": ("ge", "at least"),
        "less_than_equal": ("le", "at most"),
        "greater_than": ("gt", "more than"),
        "less_than": ("lt", "less than"),
    }
    if error_type in comparisons:
        key, words = comparisons[error_type]
        return f"must be {words} {context[key]}"
    return None


def sentence_for_error(
    error: ErrorDetails, field_hints: dict[str, Any] | None = None, unknown_field_sentence: str = ""
) -> str:
    """One Pydantic error as the end of a sentence about the field it names.

    Args:
        error: One entry of `ValidationError.errors()` or `RequestValidationError.errors()`.
        field_hints: The `ui` hints of the field the error is about, when it has them; a pattern error
            describes the accepted values with the first sentence of `help`.
        unknown_field_sentence: What to say about a field the model does not accept.

    Returns:
        A sentence fragment such as `is required` or `must match up to 40 lowercase letters`, or the
        validator's own message (without Pydantic's prefix) for a value error. A type this module has no
        sentence for keeps Pydantic's message with its opening reworded (`Input should be` -> `must be`).

    Example:
        >>> sentence_for_error({"type": "missing", "loc": ("slug",), "msg": "Field required", "input": {}})
        'is required'
    """
    error_type = error["type"]
    message = error["msg"]
    if error_type in TYPE_SENTENCES:
        return TYPE_SENTENCES[error_type]
    try:
        sentence = _sentence_from_context(
            error_type, dict(error.get("ctx") or {}), field_hints, unknown_field_sentence
        )
    except (KeyError, TypeError, ValueError):
        sentence = None  # an unexpected context: Pydantic's own message, reworded below, still says it
    if sentence is not None:
        return sentence
    for prefix in PYDANTIC_PREFIXES:
        if message.startswith(prefix):
            return message.removeprefix(prefix)
    for pydantic_opening, sentence_opening in PYDANTIC_SUBJECTS:
        if message.startswith(pydantic_opening):
            return sentence_opening + message.removeprefix(pydantic_opening)
    return message


def _problem(field_path: str, sentence: str) -> str:
    """`<field path>: <sentence>`, or the sentence alone for an error about no one field."""
    return f"{field_path}: {sentence}" if field_path else sentence


def _field_hints(model: type[BaseModel] | None, location: Sequence[int | str]) -> dict[str, Any] | None:
    """The `ui` hints of the model field an error location names; None for a nested value or no model."""
    if model is None or len(location) != 1:
        return None
    field = model.model_fields.get(str(location[0]))
    extra = field.json_schema_extra if field is not None else None
    hints = extra.get("ui") if isinstance(extra, dict) else None
    return hints if isinstance(hints, dict) else None


def option_problems(errors: Sequence[ErrorDetails], options_model: type[BaseModel]) -> list[str]:
    """The `problems` of a 422 for options that failed `options_model`'s validation.

    Args:
        errors: `ValidationError.errors()` of validating the options.
        options_model: The definition's options model; its fields' `ui.help` describes pattern errors.

    Returns:
        One `<field path>: <sentence>` per error; a sentence alone for an error about the options as a whole.

    Example:
        >>> option_problems(error.errors(), EnvironmentImportOptions)  # doctest: +SKIP
        ['slug: must match 2 to 40 lowercase letters, digits and hyphens, starting with a letter, ...']
    """
    return [
        _problem(
            PATH_SEPARATOR.join(str(part) for part in error["loc"]),
            sentence_for_error(error, _field_hints(options_model, error["loc"]), UNKNOWN_OPTION_SENTENCE),
        )
        for error in errors
    ]


def _request_problem_parts(error: ErrorDetails) -> tuple[str, str]:
    """One request error as its field path (without `path`, `query` or `body`) and its sentence."""
    location = list(error["loc"])
    if location and location[0] in REQUEST_LOCATIONS:
        location = location[1:]
    if error["type"] == "json_invalid":
        return "", sentence_for_error(error)
    if not location and error["type"] == "missing":
        return "", "the request body is required"
    return PATH_SEPARATOR.join(str(part) for part in location), sentence_for_error(error)


def request_problems(errors: Sequence[ErrorDetails]) -> list[str]:
    """The `problems` of a 422 for a request whose path, query or body failed validation.

    Args:
        errors: `RequestValidationError.errors()`.

    Returns:
        One `<field path>: <sentence>` per error, e.g. `operation_id: must be a UUID such as ...`.

    Example:
        >>> request_problems([{"type": "missing", "loc": ("body", "kind"), "msg": "Field required"}])
        ['kind: is required']
    """
    return [_problem(*_request_problem_parts(error)) for error in errors]


def request_problem_detail(errors: Sequence[ErrorDetails]) -> str:
    """The one-sentence `detail` of a 422 for a malformed request, naming every problem.

    Args:
        errors: `RequestValidationError.errors()`.

    Returns:
        `the request is not valid: ...`, each problem as `<field path> <sentence>`.

    Example:
        >>> request_problem_detail([{"type": "uuid_parsing", "loc": ("path", "operation_id"), "msg": "x"}])
        'the request is not valid: operation_id must be a UUID such as 3fa85f64-5717-4562-b3fc-2c963f66afa6'
    """
    if not errors:
        return "the request is not valid"
    readable = [
        f"{field_path} {sentence}" if field_path else sentence
        for field_path, sentence in map(_request_problem_parts, errors)
    ]
    return f"the request is not valid: {'; '.join(readable)}"


async def request_validation_problem_response(request: Request, error: Exception) -> JSONResponse:
    """Answer a malformed request 422 with `{"detail": "<sentence>", "problems": ["<field>: <sentence>"]}`.

    Registered for `RequestValidationError` on the application, so no route answers a list-valued `detail`.

    Args:
        request: The request that failed validation.
        error: The `RequestValidationError` FastAPI raised.

    Returns:
        The 422 response.

    Example:
        >>> app.add_exception_handler(RequestValidationError, request_validation_problem_response)
    """
    errors = error.errors() if isinstance(error, RequestValidationError) else []
    problems = request_problems(errors)
    detail = request_problem_detail(errors)
    logger.warning(
        "request_validation_failed",
        http_method=request.method,
        request_path=request.url.path,
        problems=problems,
        fix_suggestion="correct the named fields of the request as the problems describe, then send it again",
    )
    return JSONResponse(status_code=422, content={"detail": detail, "problems": problems})
