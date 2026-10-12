"""The direct-action executor base: review, approval, perform, settle, refresh and reconcile."""

from typing import Any

from rdscommander.control_db.models import ExecutionRecord
from rdscommander.operations.change_models import VerificationResult
from rdscommander.operations.direct_action import ActionReview, DirectActionExecutor
from rdscommander.operations.enums import OperationState
from rdscommander.operations.executor import StepContext
from rdscommander.operations.service import OperationService
from rdscommander.terraform.record import read_record
from tests.drift.test_detect_executor import _adopt_estate
from tests.operations.test_change_strategy import _direct_store, _runner, _workspace_dir
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _git, _prepare_repo

ORDERS = "demo-orders"
ORDERS_ADDRESS = "aws_db_instance.demo_orders"
TITLE = "Touch tag: demo-orders"


class TouchTagExecutor(DirectActionExecutor):
    """Test action: set a tag through the API and reconcile the rendered files."""

    operation_kind = "test_touch_tag"

    def review(self, context: StepContext) -> ActionReview:
        return ActionReview(
            what=["set tag probe=1 on demo-orders"],
            identifiers=[ORDERS],
            requires_reboot=False,
            downtime="none",
            reconcile=True,
        )

    def perform(self, context: StepContext) -> dict[str, Any]:
        rds = context.services.aws_sessions.client(
            context.target, "rds", operation_id=context.operation.id, step_name="perform"
        )
        rds.add_tags_to_resource(
            ResourceName=context.operation.resource_arn, Tags=[{"Key": "probe", "Value": "1"}]
        )
        return {"tag": "probe"}

    def settled(self, context: StepContext) -> VerificationResult:
        rds = context.services.aws_sessions.client(
            context.target, "rds", operation_id=context.operation.id, step_name="await_settled"
        )
        tags = rds.list_tags_for_resource(ResourceName=context.operation.resource_arn)["TagList"]
        live = {tag["Key"]: tag["Value"] for tag in tags}
        return VerificationResult(ok=live.get("probe") == "1", live={"probe": live.get("probe")})

    def touched_addresses(self, context: StepContext) -> set[str]:
        return {ORDERS_ADDRESS}


class RefusingExecutor(TouchTagExecutor):
    """Test action whose review refuses, so nothing may be called."""

    operation_kind = "test_refusing"

    def review(self, context: StepContext) -> ActionReview:
        raise ValueError("nothing to do")


def _register(runner, config_store) -> None:  # noqa: ANN001
    runner.executors[TouchTagExecutor.operation_kind] = TouchTagExecutor(config_store)
    runner.executors[RefusingExecutor.operation_kind] = RefusingExecutor(config_store)


def _create(service, session_factory, sandbox_snapshot, kind: str = "test_touch_tag", key: str = "touch-1"):  # noqa: ANN001, ANN202
    arn = arn_for(session_factory, sandbox_snapshot, ORDERS)
    return service.create(
        kind,
        "sandbox",
        TARGET_KEY,
        {"options": {}, "resource_arn": arn},
        key,
        "phil",
        resource_arn=arn,
        title=TITLE,
    )


def _calls_under(session_factory, operation_id, step_name: str) -> list[ExecutionRecord]:  # noqa: ANN001
    with session_factory() as session:
        return list(
            session.query(ExecutionRecord).filter_by(
                operation_id=operation_id, record_kind="aws_api", step_name=step_name
            )
        )


def test_direct_action_pauses_with_a_review_then_performs_settles_refreshes_and_reconciles_in_direct_mode(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    _register(runner, store)
    service = OperationService(session_factory)
    operation = _create(service, session_factory, sandbox_snapshot)
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL
    assert paused.plan["action_review"]["what"] == ["set tag probe=1 on demo-orders"]
    assert paused.plan["touched_addresses"] == [ORDERS_ADDRESS]
    assert not _calls_under(session_factory, operation.id, "perform")
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts if a.outcome_status == "completed"] == [
        "await_approval",
        "perform",
        "await_settled",
        "refresh_resource",
        "reconcile",
    ]
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    orders = next(i for i in recorded.instances if i.identifier == ORDERS)
    assert orders.tags.get("probe") == "1"
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == f"{TITLE} (phil, approved by sam)"
    )


def test_direct_action_in_terraform_mode_reconciles_with_a_refresh_only_apply_and_a_clean_plan(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _register(runner, config_store)
    fake = FakeTerraformRunner(runner.services.execution_recorder, plan_scripts=[{"resource_changes": []}])
    runner.services.terraform_runner = fake
    operation = _create(service, session_factory, sandbox_snapshot, key="touch-tf")
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "apply_refresh_only" in fake.calls and "plan:reconcile" in fake.calls
    assert fake.calls.index("apply_refresh_only") < fake.calls.index("plan:reconcile")
    assert "apply" not in fake.calls
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == f"{TITLE} (phil, approved by sam)"
    )
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert next(i for i in recorded.instances if i.identifier == ORDERS).tags.get("probe") == "1"


def test_direct_action_fails_reconcile_when_the_plan_is_not_clean_and_names_detect_drift(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _register(runner, config_store)
    head_before = _git("rev-parse", "HEAD", cwd=operator_settings.repo_root)
    not_clean = {
        "resource_changes": [
            {
                "address": ORDERS_ADDRESS,
                "type": "aws_db_instance",
                "change": {"actions": ["update"], "before": {"port": 5432}, "after": {"port": 5433}},
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[not_clean]
    )
    operation = _create(service, session_factory, sandbox_snapshot, key="touch-dirty")
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "reconcile_not_clean")
    assert "Detect drift" in diagnostic.fix_suggestion
    assert ORDERS_ADDRESS in diagnostic.detail
    assert final.plan["applied_without_record"] is True
    assert _git("rev-parse", "HEAD", cwd=operator_settings.repo_root) == head_before
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_a_review_error_fails_before_approval_and_nothing_is_called(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    _register(runner, store)
    service = OperationService(session_factory)
    operation = _create(service, session_factory, sandbox_snapshot, kind="test_refusing", key="refuse-1")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert [a.step_name for a in final.step_attempts] == ["await_approval"]
    diagnostic = next(d for d in final.diagnostic_reports if d.step_name == "await_approval")
    assert diagnostic.detail == "nothing to do"
    assert not _calls_under(session_factory, operation.id, "perform")


def test_a_settle_that_never_comes_marks_the_action_applied_without_record(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    runner.settings = runner.settings.model_copy(update={"direct_verify_timeout_seconds": 0})
    _register(runner, store)
    monkeypatch.setattr(
        TouchTagExecutor,
        "settled",
        lambda self, context: VerificationResult(ok=False, detail="probe is not there yet"),
    )
    service = OperationService(session_factory)
    operation = _create(service, session_factory, sandbox_snapshot, key="touch-unsettled")
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "action_not_settled")
    assert "probe is not there yet" in diagnostic.detail
    assert final.plan["applied_without_record"] is True


def test_the_reconcile_step_is_listed_only_once_the_review_says_so() -> None:
    from rdscommander.control_db.models import Operation

    executor = TouchTagExecutor(config_store=None)  # type: ignore[arg-type]
    before = Operation(operation_kind="test_touch_tag", plan={"options": {}})
    assert executor.step_names(before) == ["await_approval", "perform", "await_settled", "refresh_resource"]
    after = Operation(
        operation_kind="test_touch_tag",
        plan={"options": {}, "action_review": ActionReview(what=["x"], reconcile=True).model_dump()},
    )
    assert executor.step_names(after)[-1] == "reconcile"


def test_a_dirty_workspace_fails_at_approval_before_any_aws_call_when_the_action_reconciles(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    _register(runner, store)
    workspace_dir = _workspace_dir(operator_settings)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "hand_edit.tf").write_text("# somebody's uncommitted work\n")
    service = OperationService(session_factory)
    operation = _create(service, session_factory, sandbox_snapshot, key="touch-dirty-checkout")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert [a.step_name for a in final.step_attempts] == ["await_approval"]
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "checkout_guard")
    assert diagnostic.step_name == "await_approval"
    assert "dirty_terraform_directory" in diagnostic.detail
    assert "commit or discard" in diagnostic.fix_suggestion
    assert not _calls_under(session_factory, operation.id, "perform")
    assert "applied_without_record" not in final.plan
    assert "action_review" not in final.plan


def test_a_dirty_workspace_does_not_stop_an_action_that_never_reconciles(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    class QuietExecutor(TouchTagExecutor):
        operation_kind = "test_quiet"

        def review(self, context: StepContext) -> ActionReview:
            return ActionReview(what=["set tag probe=1 on demo-orders"], identifiers=[ORDERS])

    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    runner.executors[QuietExecutor.operation_kind] = QuietExecutor(store)
    workspace_dir = _workspace_dir(operator_settings)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "hand_edit.tf").write_text("# somebody's uncommitted work\n")
    service = OperationService(session_factory)
    operation = _create(service, session_factory, sandbox_snapshot, kind="test_quiet", key="quiet-1")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL


def test_a_restart_after_reconcile_wrote_its_files_resumes_to_success(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    """The files the step wrote before the process died are its own, so the resumed guard accepts them."""
    from datetime import timedelta

    from rdscommander.control_db.models import Operation, utc_now
    from rdscommander.lifecycle import reconcile as reconcile_module

    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    _register(runner, store)
    service = OperationService(session_factory)
    operation = _create(service, session_factory, sandbox_snapshot, key="touch-restart")
    runner.run_once()
    service.approve(operation.id, "sam")
    real_commit_paths = reconcile_module.commit_paths

    def die_before_the_commit(*arguments: Any, **keywords: Any) -> str:
        raise KeyboardInterrupt  # the process is gone; nothing after this point runs

    monkeypatch.setattr(reconcile_module, "commit_paths", die_before_the_commit)
    try:
        runner.run_once()
    except KeyboardInterrupt:
        pass
    assert service.get(operation.id).state == OperationState.RUNNING
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) != ""
    with session_factory() as session:
        session.get(Operation, operation.id).lease_expires_at_utc = utc_now() - timedelta(seconds=5)
        session.commit()
    monkeypatch.setattr(reconcile_module, "commit_paths", real_commit_paths)
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == f"{TITLE} (phil, approved by sam)"
    )
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert next(i for i in recorded.instances if i.identifier == ORDERS).tags.get("probe") == "1"
