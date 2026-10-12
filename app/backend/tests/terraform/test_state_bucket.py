import boto3

from rdscommander.terraform.state_bucket import ensure_state_bucket

BUCKET = "testco-rdscommander-state-000000000000-eu-west-2"


def test_ensure_state_bucket_creates_then_reports_existing(moto_aws) -> None:
    s3 = boto3.client("s3", region_name="eu-west-2")
    assert ensure_state_bucket(s3, BUCKET, "eu-west-2") == "created"
    assert ensure_state_bucket(s3, BUCKET, "eu-west-2") == "exists"
    assert s3.get_bucket_versioning(Bucket=BUCKET)["Status"] == "Enabled"
    assert (
        s3.get_public_access_block(Bucket=BUCKET)["PublicAccessBlockConfiguration"]["BlockPublicAcls"] is True
    )
    assert s3.get_bucket_encryption(Bucket=BUCKET)["ServerSideEncryptionConfiguration"]["Rules"]
