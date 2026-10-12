from rdscommander.terraform.hcl import to_hcl


def test_to_hcl_literals() -> None:
    assert to_hcl(None) == "null"
    assert to_hcl(True) == "true"
    assert to_hcl(5) == "5"
    assert to_hcl('say "hi" ${x} %{y}') == '"say \\"hi\\" $${x} %%{y}"'
    assert to_hcl(["a", 1]) == '["a", 1]'
    assert to_hcl({"team": "data", "rdscommander:managed": "true"}) == (
        '{\n    "rdscommander:managed" = "true"\n    team                   = "data"\n  }'
    )
