"""Create the Terraform state bucket with versioning, encryption, and public access blocked."""

from __future__ import annotations

from botocore.client import BaseClient
from botocore.exceptions import ClientError

from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)


def ensure_state_bucket(s3_client: BaseClient, bucket_name: str, region: str) -> str:
    """Return `created` or `exists`; settings are reapplied either way so they converge."""
    status = "exists"
    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("404", "NoSuchBucket", "NotFound"):
            raise
        arguments: dict = {"Bucket": bucket_name}
        if region != "us-east-1":
            arguments["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3_client.create_bucket(**arguments)
        status = "created"
        logger.info("state_bucket_created", bucket_name=bucket_name, region=region)
    s3_client.put_bucket_versioning(Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled"})
    s3_client.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3_client.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    return status
