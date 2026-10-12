"""Lifecycle tests run against the seeded simulated estate."""

import pytest

from tests.terraform.conftest import sandbox_render_input, sandbox_snapshot  # noqa: F401


@pytest.fixture
def moto_promotion_ends_replication(monkeypatch):  # noqa: ANN001, ANN201
    """Make moto promote a replica the way AWS does.

    moto 5.2.3 flips its internal replica flag but keeps reporting `ReadReplicaSourceDBInstanceIdentifier`
    and keeps the replica in its source's `ReadReplicaDBInstanceIdentifiers`, so nothing in a describe says
    the promotion happened. AWS drops both; this patch does the same for the tests that follow the
    promotion through to the reconciled files.
    """
    from moto.rds.models import RDSBackend

    real_promote = RDSBackend.promote_read_replica

    def promote_like_aws(self, db_kwargs):  # noqa: ANN001, ANN202
        replica = self.databases[db_kwargs["db_instance_identifier"]]
        source_identifier = replica.source_db_instance_identifier
        promoted = real_promote(self, db_kwargs)
        if source_identifier and source_identifier in self.databases:
            source = self.databases[source_identifier]
            if promoted.db_instance_identifier in source.replicas:
                source.remove_replica(promoted)
        promoted.source_db_instance_identifier = None
        return promoted

    monkeypatch.setattr(RDSBackend, "promote_read_replica", promote_like_aws)
