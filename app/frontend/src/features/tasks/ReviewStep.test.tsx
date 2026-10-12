import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChangeSetEntry, OperationDetail } from "@/api/types";
import { ChangeSetView, PreflightChecklist, ReviewStep } from "./ReviewStep";

afterEach(() => vi.unstubAllGlobals());

/** An operation waiting for approval, with nothing to review until a test gives it something. */
function awaitingOperation(overrides: Partial<OperationDetail> = {}): OperationDetail {
  return {
    id: "op-1",
    operation_kind: "cluster_failover",
    title: "Fail over: demo-aurora",
    state: "AWAITING_APPROVAL",
    environment_slug: "sandbox",
    target_key: "sandbox/000000000000/eu-west-2",
    resource_arn: "arn:aws:rds:eu-west-2:000000000000:cluster:demo-aurora",
    resource_id: "abc",
    approval_required_text: "demo-aurora",
    created_by_operator_name: "phil",
    approved_by_operator_name: null,
    failure_summary: null,
    created_at_utc: "2026-10-10T10:00:00Z",
    updated_at_utc: "2026-10-10T10:00:00Z",
    finished_at_utc: null,
    progress: null,
    plan: {},
    step_attempts: [],
    diagnostic_reports: [],
    execution_records: [],
    terraform_runs: [],
    change_set: [],
    requires_reboot: false,
    downtime: "none",
    ...overrides,
  };
}

const FAILOVER_REVIEW = {
  what: ["fail over demo-aurora from demo-aurora-writer to demo-aurora-reader"],
  identifiers: ["demo-aurora"],
  requires_reboot: false,
  downtime: "about 30 seconds while the writer changes",
  reconcile: false,
};

const PROTECTION_OFF: ChangeSetEntry = {
  address: "aws_db_instance.demo_temp",
  attribute: "deletion_protection",
  current: true,
  desired: false,
  apply_type: "immediate",
  requires_reboot: false,
  downtime: "none",
};

const DELETE_ROW: ChangeSetEntry = {
  address: "aws_db_instance.demo_temp",
  attribute: "presence",
  current: "exists",
  desired: "deleted",
  apply_type: "immediate",
  requires_reboot: false,
  downtime: "the database is deleted after the final snapshot demo-temp-final completes",
};

const SECOND_PHASE = {
  summary: "delete demo-temp",
  what: ["delete demo-temp after the final snapshot demo-temp-final"],
  change_set: [DELETE_ROW],
};

describe("ReviewStep for an action", () => {
  it("shows what will happen and the downtime, and is ready to approve", async () => {
    const onReadyChange = vi.fn();
    render(
      <ReviewStep
        operation={awaitingOperation({
          plan: { action_review: FAILOVER_REVIEW },
          downtime: FAILOVER_REVIEW.downtime,
        })}
        mode="terraform"
        onReadyChange={onReadyChange}
      />,
    );
    const section = screen.getByRole("region", { name: "What will happen" });
    expect(
      within(section).getByText(/fail over demo-aurora from demo-aurora-writer to demo-aurora-reader/i),
    ).toBeInTheDocument();
    expect(screen.getByText("about 30 seconds while the writer changes")).toBeInTheDocument();
    expect(screen.queryByText("Waiting for the plan…")).toBeNull();
    await waitFor(() => expect(onReadyChange).toHaveBeenLastCalledWith(true));
  });

  it("says the files are re-rendered afterwards when the action reconciles", () => {
    render(
      <ReviewStep
        operation={awaitingOperation({ plan: { action_review: { ...FAILOVER_REVIEW, reconcile: true } } })}
        mode="direct"
        onReadyChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/re-rendered and committed/)).toBeInTheDocument();
  });
});

describe("ReviewStep for a two-phase change", () => {
  it("in direct mode shows the first change set, then the second phase with its rows", async () => {
    const onReadyChange = vi.fn();
    render(
      <ReviewStep
        operation={awaitingOperation({
          operation_kind: "decommission",
          plan: { change_set: [PROTECTION_OFF], second_phase: SECOND_PHASE },
          change_set: [PROTECTION_OFF, DELETE_ROW],
        })}
        mode="direct"
        onReadyChange={onReadyChange}
      />,
    );
    const changeSet = screen.getByRole("region", { name: "Change set" });
    const then = screen.getByRole("region", { name: "Then: delete demo-temp" });
    expect(changeSet.compareDocumentPosition(then) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(changeSet).getByText("deletion_protection")).toBeInTheDocument();
    expect(within(changeSet).queryByText("presence")).toBeNull();
    expect(
      within(then).getByText("delete demo-temp after the final snapshot demo-temp-final"),
    ).toBeInTheDocument();
    expect(within(then).getByText("presence")).toBeInTheDocument();
    await waitFor(() => expect(onReadyChange).toHaveBeenLastCalledWith(true));
  });

  it("in direct mode with nothing to change first says so, and is ready", async () => {
    const onReadyChange = vi.fn();
    render(
      <ReviewStep
        operation={awaitingOperation({
          plan: { change_set: [], second_phase: SECOND_PHASE },
          change_set: [DELETE_ROW],
        })}
        mode="direct"
        onReadyChange={onReadyChange}
      />,
    );
    expect(screen.getByText(/Nothing needs changing first/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Then: delete demo-temp" })).toBeInTheDocument();
    await waitFor(() => expect(onReadyChange).toHaveBeenLastCalledWith(true));
  });

  it("in Terraform mode shows the plan, then the second phase's rows as a change set", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          run_id: 9,
          run_kind: "plan",
          plan_json: null,
          plan_summary: {
            create_count: 0,
            update_count: 1,
            delete_count: 0,
            replace_count: 0,
            import_count: 0,
            no_op_count: 0,
            resource_changes: [],
          },
        }),
      ),
    );
    const onReadyChange = vi.fn();
    render(
      <ReviewStep
        operation={awaitingOperation({
          plan: { second_phase: SECOND_PHASE },
          change_set: [DELETE_ROW],
          terraform_runs: [
            {
              id: 9,
              run_kind: "plan",
              exit_code: 2,
              plan_summary: { update_count: 1 },
              execution_record_id: null,
              created_at_utc: "2026-10-10T10:00:00Z",
            },
          ],
        })}
        mode="terraform"
        onReadyChange={onReadyChange}
      />,
    );
    const plan = screen.getByRole("region", { name: "Terraform plan" });
    const then = screen.getByRole("region", { name: "Then: delete demo-temp" });
    expect(plan.compareDocumentPosition(then) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(then).getByText("presence")).toBeInTheDocument();
    await waitFor(() => expect(onReadyChange).toHaveBeenLastCalledWith(true));
  });
});

describe("PreflightChecklist", () => {
  it("wraps a long unbroken detail inside its column instead of running past the panel", () => {
    const detail = `Traceback ${"^".repeat(400)} File "/app/backend/rdscommander/aws/rds_reader.py"`;
    render(
      <PreflightChecklist
        heading="Diagnostics"
        reports={[
          {
            id: 1,
            created_at_utc: "2026-10-11T06:29:12Z",
            detail,
            diagnostic_kind: "unexpected_exception",
            fix_suggestion: "open the execution record",
            severity: "error",
            step_name: "refresh",
            title: "inventory_refresh failed",
          },
        ]}
      />,
    );
    const text = screen.getByText(detail).parentElement;
    expect(text).toHaveClass("min-w-0", "[overflow-wrap:anywhere]");
  });
});

describe("ChangeSetView", () => {
  it("flags a change that waits for the maintenance window", () => {
    render(
      <ChangeSetView
        changeSet={[
          {
            address: "aws_db_instance.demo",
            attribute: "backup_retention_period",
            current: 7,
            desired: 14,
            apply_type: "pending-window",
            requires_reboot: false,
            downtime: "none",
          },
        ]}
      />,
    );
    expect(screen.getByText("queued for the maintenance window")).toBeInTheDocument();
  });
});

describe("ReviewStep at a major upgrade's switchover", () => {
  const switchoverPlan = {
    approval_stage: "switchover",
    green_endpoint: "demo-orders-green-abc.eu-west-2.rds.amazonaws.com",
    action_review: {
      what: ["switch production traffic from demo-orders to green"],
      identifiers: ["demo-orders"],
      requires_reboot: false,
      downtime: "about a minute",
      reconcile: true,
    },
  };

  it("tells the operator to select the green endpoint when the clipboard cannot be reached", async () => {
    vi.stubGlobal("navigator", {
      ...navigator,
      clipboard: { writeText: vi.fn(async () => Promise.reject(new Error("not allowed"))) },
    });
    render(
      <ReviewStep
        operation={awaitingOperation({ operation_kind: "major_upgrade", plan: switchoverPlan })}
        mode="direct"
        onReadyChange={() => {}}
      />,
    );
    const endpoint = screen.getByRole("region", { name: "Green endpoint" });
    within(endpoint).getByRole("button", { name: "Copy" }).click();
    expect(
      await within(endpoint).findByText(/Could not reach the clipboard; select the text and copy it instead/),
    ).toBeInTheDocument();
  });

  it("shows no green endpoint at the first pause, before the green side exists", () => {
    render(
      <ReviewStep
        operation={awaitingOperation({
          operation_kind: "major_upgrade",
          plan: { ...switchoverPlan, approval_stage: "create_green" },
        })}
        mode="direct"
        onReadyChange={() => {}}
      />,
    );
    expect(screen.queryByRole("region", { name: "Green endpoint" })).not.toBeInTheDocument();
  });
});
