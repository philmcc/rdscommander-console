import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { ChangeSetEntry, OperationDetail as OperationDetailData } from "@/api/types";
import { EnvironmentProvider, useEnvironment } from "@/app/EnvironmentContext";
import { OperationDetail } from "./OperationDetail";

const openTask = vi.hoisted(() => vi.fn());
vi.mock("@/features/tasks/TaskContext", () => ({
  useTask: () => ({ current: null, open: openTask, close: vi.fn() }),
}));

function environment(slug: string, displayName: string, isProduction: boolean) {
  return {
    slug,
    display_name: displayName,
    requires_approval: true,
    is_production: isProduction,
    change_management: "terraform",
    allow_direct_changes_to_foreign_owned: false,
    targets: [],
  };
}
const bootstrap = {
  company_display_name: "T",
  operator_name: "phil",
  engine_version: "0.2.0",
  csrf_token: "t",
  environments: [environment("sandbox", "Sandbox", false), environment("production", "Production", true)],
};

const detail: OperationDetailData = {
  id: "op-1",
  operation_kind: "terraform_adoption",
  title: "Adopt resources: demo-orders",
  state: "AWAITING_APPROVAL",
  environment_slug: "sandbox",
  target_key: "sandbox/000000000000/eu-west-2",
  resource_arn: "arn:aws:rds:eu-west-2:000000000000:db:demo-orders",
  resource_id: "abc",
  approval_required_text: "demo-orders",
  created_by_operator_name: "phil",
  approved_by_operator_name: null,
  failure_summary: null,
  created_at_utc: "2026-10-10T10:00:00Z",
  updated_at_utc: "2026-10-10T10:00:00Z",
  finished_at_utc: null,
  progress: null,
  plan: {},
  step_attempts: [
    {
      id: 1,
      step_name: "plan",
      attempt_number: 1,
      observation_state: "ABSENT",
      outcome_status: "completed",
      detail: "",
      output: {},
      started_at_utc: "2026-10-10T10:00:00Z",
      finished_at_utc: "2026-10-10T10:00:01Z",
    },
  ],
  diagnostic_reports: [
    {
      id: 1,
      step_name: "plan",
      severity: "warning",
      diagnostic_kind: "note",
      title: "Something to know",
      detail: "details here",
      fix_suggestion: "do this",
      created_at_utc: "2026-10-10T10:00:00Z",
    },
  ],
  execution_records: [
    {
      id: 5,
      step_name: "plan",
      record_kind: "terraform",
      target_key: null,
      command_line: "tofu plan",
      working_directory: "/w",
      api_name: null,
      request_parameters: null,
      response_payload: null,
      standard_output: "Plan: 1 to import",
      standard_error: "",
      exit_code: 2,
      succeeded: false,
      error_message: null,
      started_at_utc: "2026-10-10T10:00:00Z",
      duration_milliseconds: 1200,
    },
  ],
  terraform_runs: [
    {
      id: 9,
      run_kind: "plan",
      exit_code: 2,
      plan_summary: { import_count: 1 },
      execution_record_id: 5,
      created_at_utc: "2026-10-10T10:00:00Z",
    },
  ],
  change_set: [],
  requires_reboot: false,
  downtime: "none",
};

const planView = {
  run_id: 9,
  run_kind: "plan",
  plan_json: null,
  plan_summary: {
    create_count: 0,
    update_count: 0,
    delete_count: 0,
    replace_count: 0,
    import_count: 1,
    no_op_count: 0,
    resource_changes: [],
  },
};

/** Answers each operation's detail by id and the stored plan; returns the requests made as `METHOD url`. */
function serve(operations: Record<string, OperationDetailData | Response>) {
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push(`${init?.method ?? "GET"} ${url}`);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.endsWith("/plan")) return Response.json(planView);
      const operation = operations[url.split("/").at(-1) ?? ""];
      if (operation instanceof Response) return operation;
      if (operation) return Response.json(operation);
      throw new Error(`unexpected ${url}`);
    }),
  );
  return requests;
}

/** Says which environment the shell has selected, so a test can see the detail switch it. */
function SelectedEnvironment() {
  return <output aria-label="Selected environment">{useEnvironment().selected?.slug}</output>;
}

function detailPage(operationId: string) {
  return (
    <MemoryRouter>
      <EnvironmentProvider>
        <OperationDetail operationId={operationId} />
        <SelectedEnvironment />
      </EnvironmentProvider>
    </MemoryRouter>
  );
}

function renderDetail(operationId = "op-1") {
  return render(detailPage(operationId));
}

beforeEach(() => openTask.mockReset());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetBootstrapCache();
  window.localStorage.clear();
});

describe("OperationDetail", () => {
  it("shows steps, diagnostics with fix suggestions and the full output of an execution record", async () => {
    serve({ "op-1": detail });
    renderDetail();
    expect(await screen.findByRole("heading", { name: "Adopt resources: demo-orders" })).toBeInTheDocument();
    const steps = within(screen.getByRole("region", { name: "Steps" }));
    expect(steps.getByText("plan")).toBeInTheDocument();
    expect(steps.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("Something to know")).toBeInTheDocument();
    expect(screen.getByText(/do this/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /tofu plan/ }));
    expect(screen.getByText("Plan: 1 to import")).toBeInTheDocument();
  });

  it("opens the task panel on the operation instead of approving it directly", async () => {
    const requests = serve({ "op-1": detail });
    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: "Review and approve" }));
    expect(openTask).toHaveBeenCalledWith({ kind: "", operationId: "op-1", environmentSlug: "sandbox" });
    expect(requests.some((request) => request.startsWith("POST"))).toBe(false);
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("offers no approval once the operation is no longer waiting for it", async () => {
    serve({ "op-1": { ...detail, state: "SUCCEEDED", finished_at_utc: "2026-10-10T10:05:00Z" } });
    renderDetail();
    await screen.findByRole("heading", { name: "Adopt resources: demo-orders" });
    expect(screen.queryByRole("button", { name: "Review and approve" })).not.toBeInTheDocument();
    expect(screen.getByText("SUCCEEDED")).toBeInTheDocument();
  });

  it("shows how far a running operation has got", async () => {
    serve({
      "op-1": {
        ...detail,
        state: "RUNNING",
        progress: { completed_steps: 2, total_steps: 5, current_step: "write_import_blocks" },
      },
    });
    renderDetail();
    expect(await screen.findByText(/step 3 of 5/)).toBeInTheDocument();
    expect(screen.getByText("write_import_blocks")).toHaveClass("font-mono");
  });

  it("shows the failure summary of a failed operation", async () => {
    serve({ "op-1": { ...detail, state: "FAILED", failure_summary: "tofu apply exited with 1" } });
    renderDetail();
    expect(await screen.findByText("tofu apply exited with 1")).toBeInTheDocument();
  });

  it("shows the change set as a table with reboot and downtime flags", async () => {
    serve({
      "op-1": {
        ...detail,
        change_set: [
          {
            address: "aws_db_instance.orders",
            attribute: "db_instance_class",
            current: "db.t3.medium",
            desired: "db.m6g.large",
            apply_type: "pending-reboot",
            requires_reboot: true,
            downtime: "about 2 minutes",
          },
        ],
        requires_reboot: true,
        downtime: "about 2 minutes",
      },
    });
    renderDetail();
    const changeSet = within(await screen.findByRole("region", { name: "Change set" }));
    expect(changeSet.getByText("aws_db_instance.orders")).toBeInTheDocument();
    expect(changeSet.getByText("db_instance_class")).toHaveClass("font-mono");
    expect(changeSet.getByText("db.t3.medium")).toBeInTheDocument();
    expect(changeSet.getByText("db.m6g.large")).toBeInTheDocument();
    expect(changeSet.getByText("reboot")).toBeInTheDocument();
    expect(changeSet.getAllByText("about 2 minutes").length).toBeGreaterThan(0);
  });

  it("shows the plan of the latest plan run and links to the resource", async () => {
    const requests = serve({ "op-1": detail });
    renderDetail();
    expect(await screen.findByText(/1 to import/)).toBeInTheDocument();
    expect(requests).toContain("GET /api/v1/terraform/runs/9/plan");
    expect(screen.getByRole("link", { name: "demo-orders" })).toHaveAttribute("href", "/resources/abc");
  });

  it("says why the operation could not be loaded", async () => {
    serve({ "op-1": Response.json({ detail: "operation not found" }, { status: 404 }) });
    renderDetail();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the operation: operation not found",
    );
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("keeps the operation on screen when a later read fails, says so, and recovers when a read works", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const answers = [
      () => Response.json({ ...detail, state: "RUNNING" }),
      () => Response.json({ detail: "database is locked" }, { status: 500 }),
      () => Response.json({ ...detail, state: "SUCCEEDED" }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        if (url.endsWith("/plan")) return Response.json(planView);
        return (answers.shift() ?? answers[0] ?? (() => Response.json(detail)))();
      }),
    );
    renderDetail();
    expect(await screen.findByText("RUNNING")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not refresh the operation: database is locked",
    );
    expect(screen.getByRole("heading", { name: "Adopt resources: demo-orders" })).toBeInTheDocument();
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(await screen.findByText("SUCCEEDED")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows no plan for a failed plan run instead of loading one forever", async () => {
    const requests = serve({
      "op-1": {
        ...detail,
        state: "FAILED",
        terraform_runs: [
          {
            id: 9,
            run_kind: "plan",
            exit_code: 1,
            plan_summary: null,
            execution_record_id: 5,
            created_at_utc: "2026-10-10T10:00:00Z",
          },
        ],
      },
    });
    renderDetail();
    expect(await screen.findByText("No plan was produced")).toBeInTheDocument();
    expect(screen.queryByText("Loading plan…")).not.toBeInTheDocument();
    expect(requests.some((request) => request.endsWith("/plan"))).toBe(false);
    expect(screen.getByText("Something to know")).toBeInTheDocument();
  });

  it("shows the latest plan that has a summary when a later plan run failed", async () => {
    const requests = serve({
      "op-1": {
        ...detail,
        terraform_runs: [
          {
            id: 8,
            run_kind: "plan",
            exit_code: 2,
            plan_summary: { import_count: 1 },
            execution_record_id: 5,
            created_at_utc: "2026-10-10T10:00:00Z",
          },
          {
            id: 9,
            run_kind: "plan",
            exit_code: 1,
            plan_summary: null,
            execution_record_id: 6,
            created_at_utc: "2026-10-10T10:01:00Z",
          },
        ],
      },
    });
    renderDetail();
    expect(await screen.findByText(/1 to import/)).toBeInTheDocument();
    expect(requests).toContain("GET /api/v1/terraform/runs/8/plan");
    expect(screen.queryByText("No plan was produced")).not.toBeInTheDocument();
  });

  it("switches the shell to the operation's environment when it is not the selected one", async () => {
    serve({ "op-1": { ...detail, environment_slug: "production" } });
    renderDetail();
    await waitFor(() =>
      expect(screen.getByLabelText("Selected environment")).toHaveTextContent("production"),
    );
  });

  it("leaves the selected environment alone when the operation belongs to it", async () => {
    serve({ "op-1": detail });
    renderDetail();
    await screen.findByRole("heading", { name: "Adopt resources: demo-orders" });
    expect(screen.getByLabelText("Selected environment")).toHaveTextContent("sandbox");
  });

  it("shows no part of the previous operation while the next one loads", async () => {
    let answerSecond: (response: Response) => void = () => {};
    const secondAnswer = new Promise<Response>((resolve) => {
      answerSecond = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        if (url.endsWith("/plan")) return Response.json(planView);
        return url.endsWith("/op-2") ? secondAnswer : Response.json(detail);
      }),
    );
    const { rerender } = renderDetail("op-1");
    await screen.findByRole("heading", { name: "Adopt resources: demo-orders" });
    rerender(detailPage("op-2"));
    expect(screen.queryByRole("heading", { name: "Adopt resources: demo-orders" })).not.toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await act(async () => {
      answerSecond(Response.json({ ...detail, id: "op-2", title: "Reboot instance: demo-orders" }));
    });
    expect(await screen.findByRole("heading", { name: "Reboot instance: demo-orders" })).toBeInTheDocument();
  });
});

describe("OperationDetail for lifecycle operations", () => {
  it("shows what an action will do", async () => {
    serve({
      "op-1": {
        ...detail,
        operation_kind: "cluster_failover",
        title: "Fail over: demo-aurora",
        terraform_runs: [],
        execution_records: [],
        plan: {
          action_review: {
            what: ["fail over demo-aurora from demo-aurora-writer to demo-aurora-reader"],
            identifiers: ["demo-aurora"],
            requires_reboot: false,
            downtime: "about 30 seconds while the writer changes",
            reconcile: false,
          },
        },
        downtime: "about 30 seconds while the writer changes",
      },
    });
    renderDetail();
    const section = await screen.findByRole("region", { name: "What will happen" });
    expect(
      within(section).getByText(/fail over demo-aurora from demo-aurora-writer to demo-aurora-reader/i),
    ).toBeInTheDocument();
    expect(within(section).getByText("about 30 seconds while the writer changes")).toBeInTheDocument();
  });

  it("shows a decommission's second phase after its change set", async () => {
    const protectionOff: ChangeSetEntry = {
      address: "aws_db_instance.demo_temp",
      attribute: "deletion_protection",
      current: true,
      desired: false,
      apply_type: "immediate",
      requires_reboot: false,
      downtime: "none",
    };
    const deleteRow: ChangeSetEntry = {
      ...protectionOff,
      attribute: "presence",
      current: "exists",
      desired: "deleted",
    };
    serve({
      "op-1": {
        ...detail,
        operation_kind: "decommission",
        title: "Decommission: demo-temp",
        terraform_runs: [],
        execution_records: [],
        plan: {
          change_set: [protectionOff],
          second_phase: {
            summary: "delete demo-temp",
            what: ["delete demo-temp after the final snapshot demo-temp-final"],
            change_set: [deleteRow],
          },
        },
        change_set: [protectionOff, deleteRow],
      },
    });
    renderDetail();
    const then = await screen.findByRole("region", { name: "Then: delete demo-temp" });
    const changeSet = screen.getByRole("region", { name: "Change set" });
    expect(changeSet.compareDocumentPosition(then) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(changeSet).queryByText("presence")).toBeNull();
    expect(
      within(then).getByText("delete demo-temp after the final snapshot demo-temp-final"),
    ).toBeInTheDocument();
    expect(within(then).getByText("presence")).toBeInTheDocument();
  });
});

describe("OperationDetail for a major upgrade", () => {
  const majorUpgrade: OperationDetailData = {
    ...detail,
    operation_kind: "major_upgrade",
    title: "Major version upgrade: demo-orders",
    state: "RUNNING",
    approval_required_text: null,
    approved_by_operator_name: "phil",
    terraform_runs: [],
    execution_records: [],
    step_attempts: [
      {
        id: 4,
        step_name: "await_green_available",
        attempt_number: 1,
        observation_state: "PRESENT",
        outcome_status: "completed",
        detail: "",
        output: { deployment_status: "AVAILABLE", green_endpoint: "demo-orders-green.example" },
        started_at_utc: "2026-10-10T10:05:00Z",
        finished_at_utc: "2026-10-10T10:25:00Z",
      },
      {
        id: 5,
        step_name: "switchover",
        attempt_number: 1,
        observation_state: "ABSENT",
        outcome_status: null,
        detail: "",
        output: {},
        started_at_utc: "2026-10-10T10:40:00Z",
        finished_at_utc: null,
      },
    ],
    plan: {
      approval_stage: "switchover",
      deployment_id: "bgd-abc123",
      deployment_name: "rdsc-demo-orders-17-2",
      green_endpoint: "demo-orders-green.example",
      switchover_approved_by: "anna",
      switchover_approved_at_utc: "2026-10-10T10:39:00Z",
    },
  };

  it("shows the deployment's id, the status AWS last reported, the green endpoint and who approved the switchover", async () => {
    serve({ "op-1": majorUpgrade });
    renderDetail();
    const section = await screen.findByRole("region", { name: "Blue/green deployment" });
    expect(within(section).getByText("bgd-abc123")).toBeInTheDocument();
    expect(within(section).getByText("rdsc-demo-orders-17-2")).toBeInTheDocument();
    expect(within(section).getByText("AVAILABLE")).toBeInTheDocument();
    expect(within(section).getByText("demo-orders-green.example")).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(within(section).getByText(/anna/)).toBeInTheDocument();
  });

  it("says the deployment is being created before AWS reports a status, and names the blue side once kept", async () => {
    serve({
      "op-1": {
        ...majorUpgrade,
        step_attempts: [],
        plan: { approval_stage: "create_green", deployment_id: "bgd-abc123" },
      },
      "op-2": {
        ...majorUpgrade,
        id: "op-2",
        state: "SUCCEEDED",
        plan: { ...majorUpgrade.plan, blue_side_identifiers: ["demo-orders-old1"] },
      },
    });
    const { unmount } = renderDetail();
    const creating = await screen.findByRole("region", { name: "Blue/green deployment" });
    expect(within(creating).getByText("being created")).toBeInTheDocument();
    expect(within(creating).queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
    unmount();
    renderDetail("op-2");
    const finished = await screen.findByRole("region", { name: "Blue/green deployment" });
    expect(within(finished).getByText("demo-orders-old1")).toBeInTheDocument();
  });

  it("shows a cleanup's deployment as one line, not as an upgrade's deployment being created", async () => {
    serve({
      "op-1": {
        ...detail,
        operation_kind: "blue_green_cleanup",
        title: "Clean up blue/green deployment: bgd-abc123",
        state: "RUNNING",
        approval_required_text: null,
        terraform_runs: [],
        execution_records: [],
        step_attempts: [],
        plan: { deployment_id: "bgd-abc123", options: { delete_target: false } },
      },
    });
    renderDetail();
    const deletingLine = await screen.findByText(
      (_, element) => element?.tagName === "P" && element.textContent === "Deleting deployment bgd-abc123",
    );
    expect(within(deletingLine).getByText("bgd-abc123")).toHaveClass("font-mono");
    expect(screen.queryByRole("region", { name: "Blue/green deployment" })).not.toBeInTheDocument();
    expect(screen.queryByText("being created")).not.toBeInTheDocument();
  });

  it("shows no deployment for an operation that has none", async () => {
    serve({ "op-1": { ...detail, state: "SUCCEEDED" } });
    renderDetail();
    await screen.findByRole("region", { name: "Steps" });
    expect(screen.queryByRole("region", { name: "Blue/green deployment" })).not.toBeInTheDocument();
  });
});
