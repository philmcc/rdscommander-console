import pytest

from rdscommander.resources.identity import decode_resource_id, encode_resource_id


def test_resource_ids_round_trip_and_are_url_safe() -> None:
    arn = "arn:aws:rds:eu-west-2:000000000000:db:demo-orders"
    resource_id = encode_resource_id(arn)
    assert "/" not in resource_id and "+" not in resource_id and "=" not in resource_id
    assert decode_resource_id(resource_id) == arn


def test_decode_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="resource id"):
        decode_resource_id("not base64!!")
