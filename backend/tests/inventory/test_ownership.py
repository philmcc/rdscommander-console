from rdscommander.inventory.ownership import derive_ownership


def test_ownership_prefers_adopted_then_cloudformation_then_terraform_then_unmanaged() -> None:
    assert (
        derive_ownership({"rdscommander:managed": "true", "aws:cloudformation:stack-name": "s"}) == "adopted"
    )
    assert (
        derive_ownership({"aws:cloudformation:stack-name": "s", "terraform": "true"})
        == "foreign_cloudformation"
    )
    assert derive_ownership({"terraform": "true"}) == "foreign_terraform"
    assert derive_ownership({"tf_workspace": "prod"}) == "foreign_terraform"
    assert derive_ownership({"ManagedBy": "Terraform"}) == "foreign_terraform"
    assert derive_ownership({"ManagedBy": "hand"}) == "unmanaged"
    assert derive_ownership({}) == "unmanaged"
