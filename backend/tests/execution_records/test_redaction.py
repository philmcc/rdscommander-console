from rdscommander.execution_records.redaction import redact_mapping


def test_redact_mapping_hides_secret_like_keys_recursively() -> None:
    redacted = redact_mapping(
        {
            "MasterUserPassword": "pw",
            "Nested": {"aws_secret_access_key": "k", "Name": "ok"},
            "Tags": [{"Key": "Token", "Value": "t"}],
        }
    )
    assert redacted["MasterUserPassword"] == "[REDACTED]"
    assert redacted["Nested"]["aws_secret_access_key"] == "[REDACTED]"
    assert redacted["Nested"]["Name"] == "ok"
    assert redacted["Tags"][0]["Value"] == "t"
