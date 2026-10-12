from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import boto3

from rdscommander.control_db.models import ResourceAdoption, TerraformRun, TerraformWorkspace, utc_now
from rdscommander.execution_records.service import SubprocessResult
from rdscommander.history.change_events import explain_event
from rdscommander.main import build_executors, build_services
from rdscommander.operations.enums import OperationState
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.terraform.adoption_plan import build_adoption_plan
from rdscommander.terraform.record import read_record
from rdscommander.terraform.runner import PlanRunResult, StateShowResult
from tests.terraform.conftest import TARGET_KEY, arn_for

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, text=True, capture_output=True, check=True
    ).stdout.strip()


@dataclass
class FakeTerraformRunner:
    """Scripted plans: first plan shows imports, verification plans show no changes."""

    recorder: object
    plan_scripts: list[dict] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    applied_plan_files: list[str] = field(default_factory=list)
    state_move_error: str = ""
    state_addresses: list[str] | None = None
    state_identifiers: dict[str, str] = field(default_factory=dict)

    def environment_for(self, target, settings):  # noqa: ANN001, ANN201
        return {}

    def _ok(self, text: str = "") -> SubprocessResult:
        recorded = self.recorder.record_subprocess(
            record_kind="terraform",
            command=["fake"],
            working_directory=None,
            environment=None,
            operation_id=None,
            step_name="fake",
            target_key=None,
        )
        return SubprocessResult(
            record_id=recorded.record_id, exit_code=0, standard_output=text, standard_error=""
        )

    def init(self, workspace_dir, target, settings, *, operation_id, step_name):  # noqa: ANN001, ANN201
        self.calls.append("init")
        (workspace_dir / ".terraform").mkdir(exist_ok=True)
        (workspace_dir / ".terraform.lock.hcl").write_text("lock\n")
        return self._ok()

    def plan(
        self, workspace_dir, target, settings, *, operation_id, step_name, plan_file_name="console.tfplan"
    ):  # noqa: ANN001, ANN201
        self.calls.append(f"plan:{step_name}")
        script = self.plan_scripts.pop(0) if self.plan_scripts else {"resource_changes": []}
        if isinstance(script, Exception):
            raise script
        (workspace_dir / plan_file_name).write_text("binary plan")
        has_changes = any(
            c["change"]["actions"] != ["no-op"] or c["change"].get("importing")
            for c in script.get("resource_changes", [])
        )
        return PlanRunResult(result=self._ok(json.dumps(script)), has_changes=has_changes, plan_json=script)

    def apply(
        self, workspace_dir, target, settings, *, operation_id, step_name, plan_file_name="console.tfplan"
    ):  # noqa: ANN001, ANN201
        self.calls.append("apply")
        self.applied_plan_files.append(plan_file_name)
        return self._ok("Apply complete!")

    def apply_refresh_only(self, workspace_dir, target, settings, *, operation_id, step_name):  # noqa: ANN001, ANN201
        self.calls.append("apply_refresh_only")
        return self._ok("Apply complete!")

    def state_list(self, workspace_dir, target, settings, *, operation_id, step_name):  # noqa: ANN001, ANN201
        self.calls.append("state_list")
        return self._ok("\n".join(self.state_addresses or []))

    def state_move(self, workspace_dir, target, settings, source, destination, *, operation_id, step_name):  # noqa: ANN001, ANN201
        self.calls.append(f"state_mv:{source}->{destination}")
        if self.state_move_error:
            result = self._ok()
            return SubprocessResult(
                record_id=result.record_id,
                exit_code=1,
                standard_output="",
                standard_error=self.state_move_error,
            )
        if self.state_addresses is not None:
            self.state_addresses = [destination if item == source else item for item in self.state_addresses]
        return self._ok(f"Move {source} to {destination}")

    def state_show(self, workspace_dir, target, settings, address, *, operation_id, step_name):  # noqa: ANN001, ANN201
        """The scripted `identifier` state holds at `address` (`state_identifiers`), else a failure."""
        self.calls.append(f"state_show:{address}")
        if address not in self.state_identifiers:
            result = self._ok()
            return StateShowResult(
                result=SubprocessResult(
                    record_id=result.record_id,
                    exit_code=1,
                    standard_output="",
                    standard_error="No instance found for the given address",
                ),
                attributes={},
            )
        return StateShowResult(result=self._ok(), attributes={"identifier": self.state_identifiers[address]})

    def state_remove(self, workspace_dir, target, settings, address, *, operation_id, step_name):  # noqa: ANN001, ANN201
        self.calls.append(f"state_rm:{address}")
        if self.state_addresses is not None:
            self.state_addresses = [item for item in self.state_addresses if item != address]
        return self._ok(f"Removed {address}")


def _import_plan(addresses: list[str]) -> dict:
    return {
        "resource_changes": [
            {
                "address": a,
                "type": a.split(".")[0],
                "change": {
                    "actions": ["update"],
                    "before": {},
                    "after": {"tags_all": {"rdscommander:managed": "true"}},
                    "before_sensitive": {},
                    "after_sensitive": {},
                    "importing": {"id": a.split(".")[1]},
                },
            }
            for a in addresses
        ]
    }


def _prepare_repo(operator_settings) -> None:
    root = operator_settings.repo_root
    _git("init", "-q", "-b", "main", cwd=root)
    (root / "README.md").write_text("repo\n")
    # The same ignore rules the development workspace gets from bin/console: plan files and provider caches.
    (root / ".gitignore").write_text("terraform/**/.terraform/\nterraform/**/*.tfplan\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)


def _start(session_factory, operator_settings, config_store, snapshot, names, fake, key):
    runner = OperationRunner(
        session_factory,
        build_executors(fake.services, config_store),
        "r",
        operator_settings,
        config_store,
        fake.services,
    )
    arns = [arn_for(session_factory, snapshot, n) for n in names]
    with session_factory() as session:
        plan = build_adoption_plan(session, config_store.current, TARGET_KEY, arns)
    service = OperationService(session_factory)
    operation = service.create("terraform_adoption", "sandbox", TARGET_KEY, plan.model_dump(), key, "phil")
    return runner, service, operation


def _fake_services(operator_settings, session_factory, scripts):
    services = build_services(operator_settings, session_factory)
    fake = FakeTerraformRunner(services.execution_recorder, plan_scripts=scripts)
    services.terraform_runner = fake
    fake.services = services  # type: ignore[attr-defined]
    return fake


def test_adoption_of_one_resource_says_resource_and_names_operator_and_approver(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    fake = _fake_services(
        operator_settings,
        session_factory,
        [_import_plan(addresses), _import_plan(addresses), {"resource_changes": []}],
    )
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "one",
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    message = _git("log", "-1", "--format=%B", cwd=operator_settings.repo_root).strip().splitlines()
    assert (
        message[0]
        == "Adopt 1 resource into Terraform: sandbox/000000000000/eu-west-2 (phil, approved by sam)"
    )
    assert message[-1] == "- aws_db_parameter_group.demo_postgres16_tuned <- demo-postgres16-tuned"


def test_adoption_runs_end_to_end_with_approval_and_commits(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    addresses = [
        "aws_db_parameter_group.demo_postgres16_tuned",
        "aws_db_subnet_group.demo_private_subnets",
        "aws_db_instance.demo_orders",
    ]
    fake = _fake_services(
        operator_settings,
        session_factory,
        [_import_plan(addresses), _import_plan(addresses), {"resource_changes": []}],
    )
    names = ("demo-orders", "demo-postgres16-tuned", "demo-private-subnets")
    runner, service, operation = _start(
        session_factory, operator_settings, config_store, sandbox_snapshot, names, fake, "adopt-1"
    )

    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    assert (workspace_dir / "imports.tf").exists() and (workspace_dir / "instances.tf").exists()
    assert not (workspace_dir / "console-record.json").exists(), (
        "nothing is recorded before the import applied"
    )
    assert boto3.client("s3", region_name="eu-west-2").head_bucket(
        Bucket="testco-rdscommander-state-000000000000-eu-west-2"
    )

    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert fake.calls == ["init", "plan:plan", "plan:apply", "apply", "plan:verify_clean_plan"]
    assert not (workspace_dir / "imports.tf").exists()
    subject = _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
    assert (
        subject == "Adopt 3 resources into Terraform: sandbox/000000000000/eu-west-2 (phil, approved by sam)"
    )
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""
    recorded = read_record(workspace_dir)
    assert recorded is not None and recorded.snapshot_id == sandbox_snapshot.id
    assert [g.name for g in recorded.render_input.instance_parameter_groups] == ["demo-postgres16-tuned"]
    assert [s.name for s in recorded.render_input.subnet_groups] == ["demo-private-subnets"]
    assert [i.identifier for i in recorded.render_input.instances] == ["demo-orders"]
    assert recorded.render_input.import_blocks == [], "import blocks are scaffolding, not the record"
    committed_files = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=operator_settings.repo_root)
    assert "terraform/sandbox/000000000000/eu-west-2/console-record.json" in committed_files.splitlines()
    with session_factory() as session:
        workspace = session.query(TerraformWorkspace).filter_by(target_key=TARGET_KEY).one()
        assert workspace.last_applied_commit_sha == _git("rev-parse", "HEAD", cwd=operator_settings.repo_root)
        statuses = {
            r.adoption_status for r in session.query(ResourceAdoption).filter_by(workspace_id=workspace.id)
        }
        assert statuses == {"adopted"}
        kinds = [
            r.run_kind
            for r in session.query(TerraformRun)
            .filter_by(operation_id=operation.id)
            .order_by(TerraformRun.id)
        ]
        assert kinds == ["init", "plan", "plan", "apply", "verify_plan"]


def test_adoption_is_refused_when_plan_replaces_an_instance(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    destructive = _import_plan(["aws_db_parameter_group.demo_postgres16_tuned"])
    destructive["resource_changes"].append(
        {
            "address": "aws_db_instance.demo_orders",
            "type": "aws_db_instance",
            "change": {
                "actions": ["delete", "create"],
                "before": {},
                "after": {},
                "before_sensitive": {},
                "after_sensitive": {},
                "importing": {"id": "demo-orders"},
            },
        }
    )
    fake = _fake_services(operator_settings, session_factory, [destructive])
    names = ("demo-orders", "demo-postgres16-tuned", "demo-private-subnets")
    runner, service, operation = _start(
        session_factory, operator_settings, config_store, sandbox_snapshot, names, fake, "adopt-2"
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert [d.diagnostic_kind for d in final.diagnostic_reports] == ["policy_refusal"]


def test_apply_is_aborted_when_the_plan_changed_after_approval(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    drifted = _import_plan(addresses)
    drifted["resource_changes"][0]["change"]["after"]["family"] = "postgres17"
    fake = _fake_services(operator_settings, session_factory, [_import_plan(addresses), drifted])
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-3",
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED and "apply" not in fake.calls
    assert any(d.diagnostic_kind == "plan_changed_after_approval" for d in final.diagnostic_reports)


def _window_around_now() -> tuple[datetime, datetime]:
    """A change-history window wide enough to hold every operation a test just ran."""
    return utc_now() - timedelta(minutes=5), utc_now() + timedelta(minutes=5)


def test_an_adoption_refused_at_its_pre_apply_plan_does_not_explain_a_change_in_its_window(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """The refusal comes before the binary's apply, so the change that caused it stays an outside change."""
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    drifted = _import_plan(addresses)
    drifted["resource_changes"][0]["change"]["after"]["family"] = "postgres17"
    fake = _fake_services(operator_settings, session_factory, [_import_plan(addresses), drifted])
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-refused-at-the-pre-apply-plan",
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.FAILED and "apply" not in fake.calls
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        assert explain_event(session, group_arn, *_window_around_now()) == ("outside_console", None)


def test_an_adoption_whose_apply_never_returned_still_explains_a_change_in_its_window(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """Once the binary's apply was invoked AWS may have changed, even when no `apply` run was recorded."""
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    fake = _fake_services(
        operator_settings, session_factory, [_import_plan(addresses), _import_plan(addresses)]
    )

    def apply_that_never_returns(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("the console lost the terraform process half-way through apply")

    fake.apply = apply_that_never_returns
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-apply-never-returned",
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.FAILED
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        assert session.query(TerraformRun).filter_by(operation_id=operation.id, run_kind="apply").count() == 0
        assert explain_event(session, group_arn, *_window_around_now()) == ("operation", operation.id)


def test_dirty_checkout_blocks_before_any_file_is_written(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "stray.tf").write_text("x\n")
    fake = _fake_services(operator_settings, session_factory, [])
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-4",
    )
    runner.run_once()
    final = service.get(operation.id)
    assert (
        final.state == OperationState.FAILED
        and final.diagnostic_reports[0].diagnostic_kind == "checkout_guard"
    )
    assert sorted(path.name for path in workspace_dir.iterdir()) == ["stray.tf"]


def test_resume_on_another_runner_re_runs_the_checkout_guard(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """A different console resuming after approval must guard its own checkout before touching it.

    The adoption's own rendered files are accepted, but not once someone edited them by hand.
    """
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    fake = _fake_services(
        operator_settings,
        session_factory,
        [_import_plan(addresses), _import_plan(addresses), {"resource_changes": []}],
    )
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-5",
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    rendered = workspace_dir / "imports.tf"
    rendered.write_text(rendered.read_text() + "# edited by hand\n")
    service.approve(operation.id, "sam")
    other_runner = OperationRunner(
        session_factory,
        build_executors(fake.services, config_store),
        "another-machine",
        operator_settings,
        config_store,
        fake.services,
    )
    other_runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert final.diagnostic_reports[0].diagnostic_kind == "checkout_guard"
    assert [a.step_name for a in final.step_attempts].count("check_checkout") == 2


def test_an_adoption_waiting_for_approval_is_applied_after_the_console_identity_changed(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """`console down` and `up` re-create the container under a new identity; the files are still its own."""
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    fake = _fake_services(
        operator_settings,
        session_factory,
        [_import_plan(addresses), _import_plan(addresses), {"resource_changes": []}],
    )
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-recreated",
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    service.approve(operation.id, "sam")
    recreated_console = OperationRunner(
        session_factory,
        build_executors(fake.services, config_store),
        "0f3c9a2b1d4e:phil",
        operator_settings,
        config_store,
        fake.services,
    )
    recreated_console.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts].count("check_checkout") == 2
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_adoption_status_is_recorded_when_the_import_is_applied_even_if_verification_fails(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    residual = {
        "resource_changes": [
            {
                "address": addresses[0],
                "type": "aws_db_parameter_group",
                "change": {
                    "actions": ["update"],
                    "before": {"parameter": []},
                    "after": {"parameter": [{"name": "x"}]},
                    "before_sensitive": {},
                    "after_sensitive": {},
                },
            }
        ]
    }
    fake = _fake_services(
        operator_settings, session_factory, [_import_plan(addresses), _import_plan(addresses), residual]
    )
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-6",
    )
    runner.run_once()
    with session_factory() as session:
        workspace = session.query(TerraformWorkspace).filter_by(target_key=TARGET_KEY).one()
        [row] = session.query(ResourceAdoption).filter_by(workspace_id=workspace.id)
        assert row.adoption_status == "planned"
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert any(d.diagnostic_kind == "residual_plan_changes" for d in final.diagnostic_reports)
    with session_factory() as session:
        [row] = session.query(ResourceAdoption).filter_by(workspace_id=workspace.id)
        assert row.adoption_status == "adopted"
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    assert not (workspace_dir / "console-record.json").exists(), "a failed verification records nothing"


def test_stale_inventory_snapshot_is_refused_before_rendering(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from datetime import UTC, datetime, timedelta

    from rdscommander.control_db.models import InventorySnapshot

    _prepare_repo(operator_settings)
    with session_factory() as session:
        snapshot = session.get(InventorySnapshot, sandbox_snapshot.id)
        snapshot.captured_at_utc = datetime.now(UTC) - timedelta(days=3)
        session.commit()
    fake = _fake_services(operator_settings, session_factory, [])
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-7",
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert final.diagnostic_reports[0].diagnostic_kind == "stale_inventory"
    assert not (operator_settings.repo_root / "terraform" / "sandbox").exists()


def test_a_rejected_adoption_leaves_the_record_of_the_earlier_adoption_untouched(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    group = "aws_db_parameter_group.demo_postgres16_tuned"
    subnets = "aws_db_subnet_group.demo_private_subnets"
    fake = _fake_services(
        operator_settings,
        session_factory,
        [_import_plan([group]), _import_plan([group]), {"resource_changes": []}, _import_plan([subnets])],
    )
    runner, service, first = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "rec-1",
    )
    runner.run_once()
    service.approve(first.id, "sam")
    runner.run_once()
    assert service.get(first.id).state == OperationState.SUCCEEDED
    workspace_dir = operator_settings.repo_root / "terraform" / "sandbox" / "000000000000" / "eu-west-2"
    recorded_before = (workspace_dir / "console-record.json").read_text()
    runner, service, second = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-private-subnets",),
        fake,
        "rec-2",
    )
    runner.run_once()
    assert service.get(second.id).state == OperationState.AWAITING_APPROVAL
    assert "demo-private-subnets" in (workspace_dir / "subnet_groups.tf").read_text(), (
        "files are ready for review"
    )
    service.reject(second.id, "sam")
    runner.run_once()
    assert not (workspace_dir / "subnet_groups.tf").exists(), "the rejected adoption's files are removed"
    assert not (workspace_dir / "imports.tf").exists()
    assert _git("status", "--porcelain", "--", "terraform", cwd=operator_settings.repo_root) == ""
    assert (workspace_dir / "console-record.json").read_text() == recorded_before
    recorded = read_record(workspace_dir)
    assert [g.name for g in recorded.render_input.instance_parameter_groups] == ["demo-postgres16-tuned"]
    assert recorded.render_input.subnet_groups == []


def test_the_record_step_runs_although_verification_already_removed_the_import_blocks(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """`remove_import_blocks` is satisfied only when the record matches too, so it cannot be skipped."""
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    fake = _fake_services(
        operator_settings,
        session_factory,
        [_import_plan(addresses), _import_plan(addresses), {"resource_changes": []}],
    )
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "rec-3",
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    steps = {a.step_name: a for a in service.get(operation.id).step_attempts}
    assert steps["remove_import_blocks"].output["record"] == "console-record.json"
    assert steps["remove_import_blocks"].observation_state == "ABSENT", (
        "imports.tf was already gone; the record was not"
    )


def _adopt_tuned_group(session_factory, operator_settings, config_store, sandbox_snapshot, key: str):
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    fake = _fake_services(
        operator_settings,
        session_factory,
        [_import_plan(addresses), _import_plan(addresses), {"resource_changes": []}],
    )
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        key,
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    return service.get(operation.id)


def test_an_adoption_refreshes_the_inventory_once_it_commits(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """The adopted resource's page shows it adopted at once, and Adopt is no longer offered for it."""
    from rdscommander.catalogue.catalogue import actions_for_resource
    from rdscommander.control_db.models import InventorySnapshot
    from rdscommander.resources.queries import load_resource

    final = _adopt_tuned_group(
        session_factory, operator_settings, config_store, sandbox_snapshot, "adopt-refresh"
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    completed = [a.step_name for a in final.step_attempts if a.outcome_status == "completed"]
    assert completed[-2:] == ["commit_to_checkout", "refresh_resource"]
    [refresh] = [a for a in final.step_attempts if a.step_name == "refresh_resource"]
    with session_factory() as session:
        [snapshot] = session.query(InventorySnapshot).filter_by(operation_id=final.id).all()
        assert refresh.output == {"refreshed": True, "snapshot_id": snapshot.id}
        group = load_resource(
            session, config_store.current, arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
        )
    assert group.snapshot_id == snapshot.id and group.ownership == "adopted"
    actions = {a.kind: a for a in actions_for_resource(group, config_store.current.environment("sandbox"))}
    assert not actions["terraform_adoption"].applicable and actions["parameter_group_edit"].applicable


def test_a_failed_refresh_after_an_adoption_does_not_fail_the_committed_adoption(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.terraform import adoption_executor as adoption_executor_module

    def unreachable(context, target, step_name=None):  # noqa: ANN001, ANN202
        raise RuntimeError("the RDS endpoint could not be reached")

    monkeypatch.setattr(adoption_executor_module, "refresh_target_now", unreachable, raising=False)
    final = _adopt_tuned_group(
        session_factory, operator_settings, config_store, sandbox_snapshot, "adopt-no-refresh"
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    [refresh] = [a for a in final.step_attempts if a.step_name == "refresh_resource"]
    assert refresh.outcome_status == "completed" and refresh.output["refreshed"] is False
    assert "Refresh inventory" in refresh.detail


def test_a_failed_adoption_apply_says_the_kept_files_block_the_workspace_and_how_to_get_past_them(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    # The advice used to be "run a new adoption after fixing the cause", which the checkout guard then
    # refused with dirty_terraform_directory because the failed apply keeps its files.
    _prepare_repo(operator_settings)
    addresses = ["aws_db_parameter_group.demo_postgres16_tuned"]
    fake = _fake_services(
        operator_settings, session_factory, [_import_plan(addresses), _import_plan(addresses)]
    )

    def failing_apply(workspace_dir, target, settings, *, operation_id, step_name, plan_file_name="x"):  # noqa: ANN001, ANN202
        fake.calls.append("apply")
        ok = fake._ok()
        return SubprocessResult(
            record_id=ok.record_id,
            exit_code=1,
            standard_output="",
            standard_error="Error: waiting for completion",
        )

    fake.apply = failing_apply  # type: ignore[method-assign]
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned",),
        fake,
        "adopt-apply-fails",
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()

    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "terraform_apply_failed"]
    relative_dir = "terraform/sandbox/000000000000/eu-west-2"
    assert relative_dir in diagnostic.fix_suggestion
    assert "checkout guard" in diagnostic.fix_suggestion
    assert "tofu state list" in diagnostic.fix_suggestion
    assert "imports.tf" in diagnostic.fix_suggestion
    # Part of the adoption may be in state (the walkthrough's first pass): committing the files would
    # declare resources state lacks, so the advice says to run the same adoption again instead.
    assert "lists only some of them" in diagnostic.fix_suggestion
    assert "select the same resources again" in diagnostic.fix_suggestion


def test_adopting_the_same_selection_again_after_a_partly_applied_adoption_imports_the_rest_and_succeeds(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """The failed-apply advice: state holds one of the two selected addresses after the first try.

    The re-run must not refuse the one state already holds ("selected but not imported by this plan"): it is
    satisfied, and only the other is imported.
    """
    from rdscommander.terraform.workspaces import ensure_workspace_record

    _prepare_repo(operator_settings)
    group_address = "aws_db_parameter_group.demo_postgres16_tuned"
    subnet_address = "aws_db_subnet_group.demo_private_subnets"
    with session_factory() as session:
        workspace = ensure_workspace_record(
            session, config_store.current.target(TARGET_KEY), config_store.current
        )
        session.commit()
        bucket, key = workspace.state_bucket_name, workspace.state_key
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    state = {
        "version": 4,
        "resources": [{"mode": "managed", "type": "aws_db_parameter_group", "name": "demo_postgres16_tuned"}],
    }
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(state))
    group_in_state = {
        "address": group_address,
        "type": "aws_db_parameter_group",
        "change": {"actions": ["no-op"], "before": {}, "after": {}},
    }
    rerun_plan = _import_plan([subnet_address])
    rerun_plan["resource_changes"].insert(0, group_in_state)
    fake = _fake_services(
        operator_settings, session_factory, [rerun_plan, rerun_plan, {"resource_changes": []}]
    )
    runner, service, operation = _start(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        ("demo-postgres16-tuned", "demo-private-subnets"),
        fake,
        "adopt-rest",
    )
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    imports = (
        operator_settings.repo_root / "terraform/sandbox/000000000000/eu-west-2/imports.tf"
    ).read_text()
    assert subnet_address in imports and group_address not in imports
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    with session_factory() as session:
        statuses = {r.terraform_address: r.adoption_status for r in session.query(ResourceAdoption)}
    assert statuses == {group_address: "adopted", subnet_address: "adopted"}
