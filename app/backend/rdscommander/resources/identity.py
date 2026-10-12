"""URL-safe resource ids derived from ARNs."""

from __future__ import annotations

import base64
import binascii


def encode_resource_id(arn: str) -> str:
    """Encode an ARN as a URL-safe resource id.

    Args:
        arn: The AWS ARN of the resource.

    Returns:
        URL-safe base64 of the ARN without padding.

    Example:
        >>> encode_resource_id("arn:aws:rds:eu-west-2:000000000000:db:demo-orders")
        'YXJuOmF3czpyZHM6ZXUtd2VzdC0yOjAwMDAwMDAwMDAwMDpkYjpkZW1vLW9yZGVycw'
    """
    return base64.urlsafe_b64encode(arn.encode()).decode().rstrip("=")


def decode_resource_id(resource_id: str) -> str:
    """Decode a resource id back to the ARN it was made from.

    Args:
        resource_id: A value produced by `encode_resource_id`.

    Returns:
        The ARN.

    Raises:
        ValueError: If `resource_id` is not base64 text that decodes to an ARN.

    Example:
        >>> decode_resource_id("YXJuOmF3czpyZHM6ZXUtd2VzdC0yOjAwMDAwMDAwMDAwMDpkYjpkZW1vLW9yZGVycw")
        'arn:aws:rds:eu-west-2:000000000000:db:demo-orders'
    """
    padded_resource_id = resource_id + "=" * (-len(resource_id) % 4)
    try:
        arn = base64.urlsafe_b64decode(padded_resource_id.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as decode_error:
        raise ValueError(f"not a resource id: {resource_id!r}") from decode_error
    if not arn.startswith("arn:"):
        raise ValueError(f"not a resource id: {resource_id!r}")
    return arn
