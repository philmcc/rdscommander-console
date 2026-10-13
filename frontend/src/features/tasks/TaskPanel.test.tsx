import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { resetCatalogueCache } from "./catalogue";
import { TaskProvider, type TaskRequest, useTask } from "./TaskContext";

const bootstrap = {
  operator_name: "phil",
  console_version: "0.2.0",
  csrf_token: "t",
  environments: [
    {
      slug: "sandbox",
      display_name: "Sandbox",
      requires_approval: true,
      is_production: false,
      change_management: "direct",
      allow_direct_changes_to_foreign_owned: false,
      targets: [
        {
          target_key: "sandbox/0/eu-west-2",
          aws_account_id: "0",
          aws_region: "eu-west-2",
          aws_profile_name: null,
          simulated: true,
        },
      ],
    },
  ],
};
const catalogue = {
  definitions: [
    {
      kind: "parameter_group_attach",
      title: "Attach parameter group",
      group: "Parameter groups",
      summary: "s",
      applies_to: ["instance"],
      modes: ["terraform", "direct"],
      destructive: false,
      disruptive: true,
      needs_target: false,
      options_schema: {
        type: "object",
        properties: {
          parameter_group_arn: { type: "string", ui: { widget: "text", help: "ARN", order: 1 } },
          reboot: {
            type: "string",
            enum: ["none", "now"],
            default: "none",
            ui: { widget: "choice", help: "", order: 2, labels: { none: "No", now: "Now" } },
          },
        },
        required: ["parameter_group_arn"],
      },
    },
  ],
};
const base = {
  id: "op-1",
  operation_kind: "parameter_group_attach",
  environment_slug: "sandbox",
  target_key: "sandbox/0/eu-west-2",
  created_by_operator_name: "phil",
  approved_by_operator_name: null,
  failure_summary: null,
  created_at_utc: "2026-10-10T10:00:00Z",
  updated_at_utc: "2026-10-10T10:00:00Z",
  finished_at_utc: null,
  title: "Attach parameter group: demo-orders",
  resource_arn: "arn:x",
  resource_id: "YXJuOng",
  approval_required_text: "demo-orders",
  plan: {},
  step_attempts: [],
  diagnostic_reports: [],
  execution_records: [],
  terraform_runs: [],
  change_set: null,
  change_set_verification: null,
  requires_reboot: false,
  downtime: "none",
  pending_reboot_attributes: [],
  foreign_owner_banner: null,
  touched_addresses: [],
};

function Opener() {
  const { open } = useTask();
  return (
    <button
      type="button"
      onClick={() =>
        open({
          kind: "parameter_group_attach",
          environmentSlug: "sandbox",
          resourceId: "YXJuOng",
          resourceIdentifier: "demo-orders",
        })
      }
    >
      go
    </button>
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetBootstrapCache();
  resetCatalogueCache();
});

describe("TaskPanel", () => {
  it("walks options, review and approve with identifier confirmation", async () => {
    const afterApproval = ["RUNNING", "SUCCEEDED"];
    const posts: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        if (url.endsWith("/api/v1/catalogue")) return Response.json(catalogue);
        if (url.endsWith("/api/v1/operations") && init?.method === "POST") {
          posts.push(String(init.body));
          return Response.json({ operation_id: "op-1", state: "QUEUED" }, { status: 202 });
        }
        if (url.endsWith("/approve")) {
          posts.push("approve");
          return Response.json({ ...base, state: "RUNNING" });
        }
        if (url.endsWith("/api/v1/operations/op-1")) {
          const state = posts.includes("approve")
            ? (afterApproval.shift() ?? "SUCCEEDED")
            : "AWAITING_APPROVAL";
          return Response.json({
            ...base,
            state,
            change_set: [
              {
                address: "aws_db_instance.demo_orders",
                attribute: "parameter_group_name",
                current: "a",
                desired: "b",
                apply_type: "pending-reboot",
                requires_reboot: true,
                downtime: "a reboot now",
              },
            ],
          });
        }
        throw new Error(`unexpected ${url} ${init?.method ?? "GET"}`);
      }),
    );
    render(
      <MemoryRouter>
        <EnvironmentWrapper />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "go" }));
    expect(await screen.findByRole("dialog", { name: "Attach parameter group" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("parameter_group_arn"), "arn:group");
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    expect(await screen.findByText("parameter_group_name")).toBeInTheDocument();
    expect(screen.getByText("a reboot now")).toBeInTheDocument();
    expect(posts[0]).toContain('"kind":"parameter_group_attach"');
    const approve = screen.getByRole("button", { name: "Approve and run" });
    expect(approve).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Type demo-orders to confirm/), "demo-orders");
    expect(approve).toBeEnabled();
    await userEvent.click(approve);
    await waitFor(() => expect(posts).toContain("approve"));
    expect(await screen.findByText(/Succeeded/)).toBeInTheDocument();
  });
});

type TaskApi = ReturnType<typeof useTask>;

/** Renders the providers and hands back the task API, so a test can open the panel while it is modal. */
function renderWithTaskApi(): { task: () => TaskApi } {
  let latest: TaskApi | null = null;
  function Capture() {
    latest = useTask();
    return null;
  }
  render(
    <MemoryRouter>
      <EnvironmentProvider>
        <TaskProvider>
          <Capture />
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
  return {
    task: () => {
      if (!latest) throw new Error("TaskProvider has not rendered");
      return latest;
    },
  };
}

const changeSetEntry = {
  address: "aws_db_instance.demo_orders",
  attribute: "parameter_group_name",
  current: "a",
  desired: "b",
  apply_type: "immediate",
  requires_reboot: false,
  downtime: "none",
};

const attachRequest: TaskRequest = {
  kind: "parameter_group_attach",
  environmentSlug: "sandbox",
  resourceId: "YXJuOng",
  resourceIdentifier: "demo-orders",
};
const reviewRequest: TaskRequest = { kind: "", operationId: "op-1", environmentSlug: "sandbox" };

const editDefinition = {
  kind: "parameter_group_edit",
  title: "Edit parameters",
  group: "Parameter groups",
  summary: "Set or reset parameters.",
  applies_to: ["parameter_group"],
  modes: ["terraform", "direct"],
  destructive: false,
  disruptive: false,
  needs_target: false,
  options_schema: {
    type: "object",
    properties: {
      parameters: { type: "object", ui: { widget: "parameters", help: "Values to set.", order: 1 } },
    },
  },
};
const importDefinition = {
  kind: "environment_import",
  title: "Import environment",
  group: "Environments",
  summary: "Add an account.",
  applies_to: ["environment"],
  modes: ["terraform", "direct"],
  destructive: false,
  disruptive: false,
  needs_target: false,
  pauses_for_approval: false,
  pauses_for_approval_when: null,
  options_schema: {
    type: "object",
    properties: {
      slug: { type: "string", title: "Slug", ui: { widget: "text", help: "Short name.", order: 1 } },
    },
  },
};

interface ApiStub {
  /** Every POST, in order. */
  posts: { path: string; body: string }[];
}

/**
 * Stubs fetch: bootstrap and catalogue always, everything else through `route`, which returns a Response
 * or undefined for a path it does not know.
 */
function stubApi(
  route: (
    path: string,
    init: RequestInit | undefined,
    stub: ApiStub,
  ) => Response | Promise<Response> | undefined,
  options: { environments?: object[]; definitions?: object[] } = {},
): ApiStub {
  const stub: ApiStub = { posts: [] };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "POST") stub.posts.push({ path, body: String(init.body ?? "") });
      if (path.endsWith("/api/v1/bootstrap")) {
        return Response.json({ ...bootstrap, environments: options.environments ?? bootstrap.environments });
      }
      if (path.endsWith("/api/v1/catalogue")) {
        return Response.json({ definitions: options.definitions ?? catalogue.definitions });
      }
      const answered = route(path, init, stub);
      if (answered) return answered;
      throw new Error(`unexpected ${path} ${init?.method ?? "GET"}`);
    }),
  );
  return stub;
}

describe("TaskPanel flows", () => {
  it("shows field problems inline and the others in a general list, and stays on the options", async () => {
    stubApi((path, init) => {
      if (path.endsWith("/api/v1/operations") && init?.method === "POST") {
        return Response.json(
          {
            detail: "invalid options",
            problems: [
              "parameter_group_arn: Field required",
              "Attach parameter group cannot run here: pending",
            ],
          },
          { status: 422 },
        );
      }
      return undefined;
    });
    const { task } = renderWithTaskApi();
    act(() => task().open(attachRequest));
    await screen.findByRole("dialog", { name: "Attach parameter group" });
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    expect(await screen.findByText("Field required")).toBeInTheDocument();
    expect(screen.getByLabelText("parameter_group_arn")).toHaveAccessibleDescription(/Field required/);
    expect(screen.getByLabelText("parameter_group_arn")).toBeInvalid();
    const general = screen.getByRole("alert");
    expect(within(general).getByText("Attach parameter group cannot run here: pending")).toBeInTheDocument();
    expect(within(general).getByText("Fix the options marked below.")).toBeInTheDocument();
    expect(within(general).queryByText("Field required")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue to review" })).toBeEnabled();
  });

  it("shows the server's sentences as given: under the labelled field they name, and the rest above", async () => {
    stubApi(
      (path, init) =>
        path.endsWith("/api/v1/operations") && init?.method === "POST"
          ? Response.json(
              {
                detail: "invalid options",
                problems: [
                  "slug: must match 2 to 40 lowercase letters, digits and hyphens, starting with a letter, for example staging",
                  "choose either a profile or the simulated account",
                ],
              },
              { status: 422 },
            )
          : undefined,
      {
        definitions: [
          {
            ...importDefinition,
            options_schema: {
              type: "object",
              properties: {
                slug: {
                  type: "string",
                  title: "Slug",
                  ui: { widget: "text", label: "Short name", help: "Used in addresses.", order: 1 },
                },
              },
            },
          },
        ],
      },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open({ kind: "environment_import" }));
    await userEvent.type(await screen.findByLabelText("Short name"), "Staging");
    await userEvent.click(screen.getByRole("button", { name: "Import environment" }));
    const sentence =
      "must match 2 to 40 lowercase letters, digits and hyphens, starting with a letter, for example staging";
    expect(await screen.findByText(sentence)).toBeInTheDocument();
    expect(screen.getByLabelText("Short name")).toHaveAccessibleDescription(`Used in addresses. ${sentence}`);
    const general = screen.getByRole("alert");
    expect(within(general).getByText("choose either a profile or the simulated account")).toBeInTheDocument();
  });

  it("shows the reason of a refusal that carries no problems", async () => {
    stubApi((path, init) =>
      path.endsWith("/api/v1/operations") && init?.method === "POST"
        ? Response.json({ detail: "Attach parameter group is not available in direct mode" }, { status: 422 })
        : undefined,
    );
    const { task } = renderWithTaskApi();
    act(() => task().open(attachRequest));
    await screen.findByRole("dialog", { name: "Attach parameter group" });
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    expect(
      await within(await screen.findByRole("alert")).findByText(
        "Attach parameter group is not available in direct mode",
      ),
    ).toBeInTheDocument();
  });

  it("passes a problem that starts with a parameter name to that parameter's row", async () => {
    stubApi(
      (path, init) => {
        if (path.endsWith("/api/v1/operations") && init?.method === "POST") {
          return Response.json(
            { detail: "invalid options", problems: ["work_mem must be an integer"] },
            { status: 422 },
          );
        }
        if (path.endsWith("/api/v1/resources/YXJuOng")) {
          return Response.json({
            actions: [],
            recent_operations: [],
            record: {
              facts: { parameter_group_family: "postgres16", parameters: { work_mem: { value: "4096" } } },
            },
          });
        }
        if (path.startsWith("/api/v1/parameters/families/postgres16")) {
          return Response.json({
            family: "postgres16",
            fetched_at_utc: "2026-10-10T10:00:00Z",
            source: "bundled",
            parameters: {
              work_mem: {
                name: "work_mem",
                allowed_values: null,
                apply_method_default: "immediate",
                apply_type: "dynamic",
                data_type: "integer",
                default_value: "4096",
                description: "Memory for sorts.",
                is_modifiable: true,
              },
            },
          });
        }
        return undefined;
      },
      { definitions: [editDefinition] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open({ ...attachRequest, kind: "parameter_group_edit" }));
    await userEvent.type(await screen.findByLabelText("Value of work_mem"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    await waitFor(() => expect(screen.getByLabelText("Value of work_mem")).toBeInvalid());
    // Once in the general list above the form and once on the row it names.
    expect(screen.getAllByText("work_mem must be an integer")).toHaveLength(2);
  });

  it("opens an existing operation from an empty kind and rejects it when asked", async () => {
    stubApi((path, _init, stub) => {
      if (path.endsWith("/reject")) return Response.json({ ...base, state: "CANCELLED" });
      if (path.endsWith("/api/v1/operations/op-1")) {
        const rejected = stub.posts.some((post) => post.path.endsWith("/reject"));
        return Response.json({
          ...base,
          state: rejected ? "CANCELLED" : "AWAITING_APPROVAL",
          approval_required_text: null,
          change_set: [changeSetEntry],
        });
      }
      return undefined;
    });
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    expect(await screen.findByRole("dialog", { name: "Attach parameter group" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Approve and run" })).toBeEnabled();
    expect(screen.queryByLabelText(/to confirm/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and run" })).not.toBeInTheDocument();
  });

  it("asks before replacing a task that was started from the options, and keeps it when declined", async () => {
    stubApi(() => undefined, { definitions: [...catalogue.definitions, importDefinition] });
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const { task } = renderWithTaskApi();
    act(() => task().open(attachRequest));
    await screen.findByRole("dialog", { name: "Attach parameter group" });
    act(() => task().open({ kind: "environment_import" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("dialog", { name: "Attach parameter group" })).toBeInTheDocument();
    act(() => task().open({ kind: "environment_import" }));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(await screen.findByRole("dialog", { name: "Import environment" })).toBeInTheDocument();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });

  it("replaces a task opened to review an existing operation without asking", async () => {
    stubApi((path) =>
      path.endsWith("/api/v1/operations/op-1")
        ? Response.json({ ...base, state: "AWAITING_APPROVAL" })
        : undefined,
    );
    const confirm = vi.spyOn(window, "confirm");
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    await screen.findByRole("button", { name: "Approve and run" });
    act(() => task().open(attachRequest));
    expect(confirm).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "Continue to review" })).toBeInTheDocument();
  });

  it("marks production in the panel", async () => {
    stubApi(() => undefined, {
      environments: [{ ...bootstrap.environments[0], display_name: "Prod EU", is_production: true }],
    });
    const { task } = renderWithTaskApi();
    act(() => task().open(attachRequest));
    const banner = (await screen.findByText("production", { selector: "strong" })).closest("p");
    expect(banner).toHaveTextContent("This is production (Prod EU).");
  });

  it("keeps a production environment's chip in the panel's fixed header, above the review that scrolls", async () => {
    stubApi(
      (path) =>
        path.endsWith("/api/v1/operations/op-1")
          ? Response.json({
              ...base,
              state: "AWAITING_APPROVAL",
              change_set: Array.from({ length: 40 }, (_, index) => ({
                ...changeSetEntry,
                attribute: `parameter_${index}`,
              })),
            })
          : undefined,
      { environments: [{ ...bootstrap.environments[0], display_name: "Prod EU", is_production: true }] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    const approve = await screen.findByRole("button", { name: "Approve and run" });
    const header = screen
      .getByRole("heading", { name: "Attach parameter group" })
      .closest('[data-slot="sheet-header"]');
    expect(header).not.toBeNull();
    const fixedHeader = header as HTMLElement;
    expect(fixedHeader).not.toContainElement(approve);
    const chip = within(fixedHeader).getByText("Prod EU").parentElement;
    expect(chip).toHaveTextContent("Prod EUproduction");
    expect(chip?.className).toContain("border-l-status-red");
  });

  it("sends a required switch left off as off, because that is what the form shows", async () => {
    const deletionProtection = {
      kind: "deletion_protection_change",
      title: "Change deletion protection",
      group: "Instances and clusters",
      summary: "Turn deletion protection on or off.",
      applies_to: ["cluster"],
      modes: ["terraform", "direct"],
      destructive: false,
      disruptive: false,
      needs_target: false,
      options_schema: {
        type: "object",
        properties: {
          deletion_protection: {
            type: "boolean",
            title: "Deletion protection",
            ui: { help: "AWS refuses to delete the resource while this is on.", order: 1 },
          },
          copy_tags_to_snapshot: {
            anyOf: [{ type: "boolean" }, { type: "null" }],
            default: null,
            ui: { order: 2 },
          },
        },
        required: ["deletion_protection"],
      },
    };
    const stub = stubApi(
      (path, init) =>
        path.endsWith("/api/v1/operations") && init?.method === "POST"
          ? Response.json({ detail: "refused" }, { status: 404 })
          : undefined,
      { definitions: [deletionProtection] },
    );
    const { task } = renderWithTaskApi();
    act(() =>
      task().open({ kind: "deletion_protection_change", environmentSlug: "sandbox", resourceId: "YXJuOng" }),
    );
    const toggle = await screen.findByRole("switch", { name: "Deletion protection" });
    expect(toggle).not.toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    await screen.findByText("refused");
    const body = JSON.parse(stub.posts[0]?.body ?? "{}");
    expect(body.options).toEqual({ deletion_protection: false });
  });

  it("sends no environment for a task that acts on none", async () => {
    const stub = stubApi(
      (path, init) =>
        path.endsWith("/api/v1/operations") && init?.method === "POST"
          ? Response.json({ detail: "refused" }, { status: 404 })
          : undefined,
      { definitions: [importDefinition] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open({ kind: "environment_import" }));
    await userEvent.type(await screen.findByLabelText("Slug"), "staging");
    await userEvent.click(screen.getByRole("button", { name: "Import environment" }));
    await screen.findByText("refused");
    const body = JSON.parse(stub.posts[0]?.body ?? "{}");
    expect(body).toMatchObject({
      kind: "environment_import",
      environment_slug: null,
      resource_id: null,
      options: { slug: "staging" },
    });
  });

  it("shows the Terraform plan, the foreign owner and the preflight warnings for a Terraform environment", async () => {
    stubApi(
      (path) => {
        if (path.endsWith("/api/v1/operations/op-1")) {
          return Response.json({
            ...base,
            state: "AWAITING_APPROVAL",
            approval_required_text: null,
            requires_reboot: true,
            downtime: "a short reboot",
            foreign_owner_banner: "cloudformation",
            terraform_runs: [
              {
                id: 7,
                run_kind: "plan",
                exit_code: 0,
                plan_summary: null,
                execution_record_id: null,
                created_at_utc: "2026-10-10T10:00:00Z",
              },
            ],
            diagnostic_reports: [
              {
                id: 1,
                step_name: "plan",
                severity: "warning",
                diagnostic_kind: "maintenance",
                title: "Maintenance is pending",
                detail: "",
                fix_suggestion: "Apply it first",
                created_at_utc: "2026-10-10T10:00:00Z",
              },
            ],
          });
        }
        if (path.endsWith("/api/v1/terraform/runs/7/plan")) {
          return Response.json({
            run_id: 7,
            run_kind: "plan",
            plan_summary: {
              create_count: 0,
              update_count: 1,
              delete_count: 0,
              replace_count: 0,
              import_count: 0,
              no_op_count: 0,
              resource_changes: [
                {
                  address: "aws_db_instance.orders",
                  resource_type: "aws_db_instance",
                  actions: ["update"],
                  is_import: false,
                  import_id: null,
                  before: { a: 1 },
                  after: { a: 2 },
                },
              ],
            },
          });
        }
        return undefined;
      },
      { environments: [{ ...bootstrap.environments[0], change_management: "terraform" }] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    expect(await screen.findByText("Terraform plan")).toBeInTheDocument();
    expect(await screen.findByText("aws_db_instance.orders")).toBeInTheDocument();
    expect(screen.queryByText("Change set")).not.toBeInTheDocument();
    expect(screen.getByText("cloudformation")).toBeInTheDocument();
    expect(screen.getByText("warn")).toBeInTheDocument();
    expect(screen.getByText("Maintenance is pending")).toBeInTheDocument();
    expect(screen.getByText("Fix: Apply it first")).toBeInTheDocument();
    expect(screen.getByText(/A reboot is required/)).toHaveTextContent("Expected downtime: a short reboot.");
  });

  it("shows a failed operation with its timeline, summary and diagnostics", async () => {
    stubApi((path) =>
      path.endsWith("/api/v1/operations/op-1")
        ? Response.json({
            ...base,
            state: "FAILED",
            failure_summary: "The apply stopped",
            step_attempts: [
              {
                id: 1,
                step_name: "apply_parameters",
                attempt_number: 1,
                observation_state: "ABSENT",
                outcome_status: "failed",
                detail: "exit 1",
                output: {},
                started_at_utc: "2026-10-10T10:00:00Z",
                finished_at_utc: "2026-10-10T10:00:01Z",
              },
            ],
            diagnostic_reports: [
              {
                id: 2,
                step_name: "apply_parameters",
                severity: "error",
                diagnostic_kind: "apply",
                title: "Apply failed",
                detail: "",
                fix_suggestion: "Retry after the reboot",
                created_at_utc: "2026-10-10T10:00:01Z",
              },
            ],
          })
        : undefined,
    );
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("The apply stopped")).toBeInTheDocument();
    expect(screen.getByText("apply_parameters")).toBeInTheDocument();
    expect(screen.getByText("fail")).toBeInTheDocument();
    expect(screen.getByText("Fix: Retry after the reboot")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open operation" })).toHaveAttribute("href", "/operations/op-1");
  });

  it("keeps Approve disabled while the change set is still computing", async () => {
    let reads = 0;
    stubApi((path) => {
      if (!path.endsWith("/api/v1/operations/op-1")) return undefined;
      reads += 1;
      return Response.json({
        ...base,
        state: "AWAITING_APPROVAL",
        approval_required_text: null,
        change_set:
          reads < 2
            ? null
            : [
                {
                  address: "aws_db_instance.demo_orders",
                  attribute: "parameter_group_name",
                  current: "a",
                  desired: "b",
                  apply_type: "immediate",
                  requires_reboot: false,
                  downtime: "none",
                },
              ],
      });
    });
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    expect(await screen.findByText("Computing…")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "Approve and run" });
    expect(approve).toBeDisabled();
    // The review is the current step while the operation waits for approval; Approve is not yet.
    const steps = within(screen.getByRole("list", { name: "Steps" })).getAllByRole("listitem");
    expect(steps.map((step) => step.getAttribute("aria-current"))).toEqual([null, "step", null]);
    expect(await screen.findByText("parameter_group_name", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByText("Computing…")).not.toBeInTheDocument();
    await waitFor(() => expect(approve).toBeEnabled());
  });

  it("keeps Approve disabled until the Terraform plan has loaded", async () => {
    let reads = 0;
    let releasePlan: () => void = () => undefined;
    const planReleased = new Promise<void>((resolve) => {
      releasePlan = resolve;
    });
    stubApi(
      (path) => {
        if (path.endsWith("/api/v1/operations/op-1")) {
          reads += 1;
          return Response.json({
            ...base,
            state: "AWAITING_APPROVAL",
            approval_required_text: null,
            terraform_runs:
              reads < 2
                ? []
                : [
                    {
                      id: 7,
                      run_kind: "plan",
                      exit_code: 0,
                      plan_summary: null,
                      execution_record_id: null,
                      created_at_utc: "2026-10-10T10:00:00Z",
                    },
                  ],
          });
        }
        if (path.endsWith("/api/v1/terraform/runs/7/plan")) {
          return planReleased.then(() =>
            Response.json({
              run_id: 7,
              run_kind: "plan",
              plan_summary: {
                create_count: 0,
                update_count: 1,
                delete_count: 0,
                replace_count: 0,
                import_count: 0,
                no_op_count: 0,
                resource_changes: [
                  {
                    address: "aws_db_instance.orders",
                    resource_type: "aws_db_instance",
                    actions: ["update"],
                    is_import: false,
                    import_id: null,
                    before: { a: 1 },
                    after: { a: 2 },
                  },
                ],
              },
            }),
          );
        }
        return undefined;
      },
      { environments: [{ ...bootstrap.environments[0], change_management: "terraform" }] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    expect(await screen.findByText("Waiting for the plan…")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "Approve and run" });
    expect(approve).toBeDisabled();
    expect(await screen.findByText("Loading plan…", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(approve).toBeDisabled();
    releasePlan();
    expect(await screen.findByText("aws_db_instance.orders")).toBeInTheDocument();
    await waitFor(() => expect(approve).toBeEnabled());
  });

  it("names the target from the operation's title when opened by operation id alone", async () => {
    stubApi((path) =>
      path.endsWith("/api/v1/operations/op-1")
        ? Response.json({ ...base, state: "AWAITING_APPROVAL" })
        : undefined,
    );
    const { task } = renderWithTaskApi();
    act(() => task().open({ kind: "", operationId: "op-1" }));
    const dialog = await screen.findByRole("dialog", { name: "Attach parameter group" });
    expect(dialog).toHaveAccessibleDescription(/Attach parameter group: demo-orders/);
    const description = document.getElementById(dialog.getAttribute("aria-describedby") ?? "");
    if (!description) throw new Error("the dialog has no description");
    expect(within(description).getByText("demo-orders")).toHaveClass("font-mono");
  });

  it.each(["direct", "terraform"])(
    "reviews an operation that names instances to reboot instead of a change set or plan (%s mode)",
    async (mode) => {
      stubApi(
        (path) =>
          path.endsWith("/api/v1/operations/op-1")
            ? Response.json({
                ...base,
                operation_kind: "reboot_instance",
                state: "AWAITING_APPROVAL",
                change_set: null,
                terraform_runs: [],
                requires_reboot: true,
                downtime: "a reboot of demo-orders, demo-orders-replica",
                plan: { reboot_identifiers: ["demo-orders", "demo-orders-replica"] },
              })
            : undefined,
        {
          definitions: [{ ...catalogue.definitions[0], kind: "reboot_instance", title: "Reboot instance" }],
          environments: [{ ...bootstrap.environments[0], change_management: mode }],
        },
      );
      const { task } = renderWithTaskApi();
      act(() => task().open(reviewRequest));
      const targets = within(await screen.findByRole("list", { name: "Instances to reboot" }));
      expect(targets.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
        "demo-orders",
        "demo-orders-replica",
      ]);
      expect(screen.queryByText("Computing…")).not.toBeInTheDocument();
      expect(screen.queryByText("Waiting for the plan…")).not.toBeInTheDocument();
      expect(screen.getByText(/A reboot is required/)).toHaveTextContent(
        "Expected downtime: a reboot of demo-orders, demo-orders-replica.",
      );
      const approve = screen.getByRole("button", { name: "Approve and run" });
      expect(approve).toBeDisabled();
      await userEvent.type(screen.getByLabelText(/Type demo-orders to confirm/), "demo-orders");
      expect(approve).toBeEnabled();
    },
  );
});

function EnvironmentWrapper() {
  return (
    <EnvironmentProvider>
      <TaskProvider>
        <Opener />
      </TaskProvider>
    </EnvironmentProvider>
  );
}

/** How many times the console has asked for bootstrap. */
function bootstrapReads(): number {
  return vi.mocked(fetch).mock.calls.filter(([input]) => String(input).endsWith("/api/v1/bootstrap")).length;
}

describe("TaskPanel and the console's list of environments", () => {
  it("reads bootstrap again when an import's panel closes, whoever opened it", async () => {
    stubApi(() => undefined, { definitions: [importDefinition] });
    const { task } = renderWithTaskApi();
    act(() => task().open({ kind: "environment_import" }));
    await screen.findByRole("dialog", { name: "Import environment" });
    expect(bootstrapReads()).toBe(1);
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(bootstrapReads()).toBe(2));
  });

  it("reads bootstrap again when an import reopened by its operation closes", async () => {
    stubApi(
      (path) =>
        path.endsWith("/api/v1/operations/op-1")
          ? Response.json({ ...base, operation_kind: "environment_import", state: "SUCCEEDED" })
          : undefined,
      { definitions: [importDefinition] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open({ kind: "", operationId: "op-1" }));
    await screen.findByRole("dialog", { name: "Import environment" });
    expect(bootstrapReads()).toBe(1);
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(bootstrapReads()).toBe(2));
  });

  it("does not read bootstrap again when another task's panel closes", async () => {
    stubApi(() => undefined, { definitions: [...catalogue.definitions, importDefinition] });
    const { task } = renderWithTaskApi();
    act(() => task().open(attachRequest));
    await screen.findByRole("dialog", { name: "Attach parameter group" });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    expect(bootstrapReads()).toBe(1);
  });
});

const refreshDefinition = {
  kind: "inventory_refresh",
  title: "Refresh inventory",
  group: "Discovery and adoption",
  summary: "Read the environment's resources from AWS.",
  applies_to: ["environment"],
  modes: ["terraform", "direct"],
  destructive: false,
  disruptive: false,
  needs_target: false,
  pauses_for_approval: false,
  pauses_for_approval_when: null,
  options_schema: { type: "object", properties: {} },
};
const driftResolveDefinition = {
  kind: "drift_resolve",
  title: "Resolve drift",
  group: "Discovery and adoption",
  summary: "Accept reality into the record, or restore the record to AWS.",
  applies_to: ["instance"],
  modes: ["terraform"],
  destructive: false,
  disruptive: false,
  needs_target: false,
  pauses_for_approval: true,
  pauses_for_approval_when: { accept: false, restore: true },
  options_schema: {
    type: "object",
    properties: {
      resource_arn: { type: "string", ui: { widget: "hidden", label: "Resource", order: 1 } },
      choice: {
        type: "string",
        enum: ["accept", "restore"],
        ui: {
          widget: "choice",
          label: "Resolution",
          order: 2,
          labels: { accept: "Accept into the record", restore: "Restore from the record" },
        },
      },
    },
  },
};

/** The labels of the panel's header steps, in order. */
function headerSteps(): string[] {
  return within(screen.getByRole("list", { name: "Steps" }))
    .getAllByRole("listitem")
    .map((item) => item.textContent?.replace(/^\d|✓/, "") ?? "");
}

describe("TaskPanel for tasks that never pause for approval", () => {
  it("runs a task that never pauses straight from its options, without promising a review", async () => {
    let reads = 0;
    const stub = stubApi(
      (path, init) => {
        if (path.endsWith("/api/v1/operations") && init?.method === "POST") {
          return Response.json({ operation_id: "op-1", state: "QUEUED" }, { status: 202 });
        }
        if (path.endsWith("/api/v1/operations/op-1")) {
          reads += 1;
          return Response.json({
            ...base,
            operation_kind: "inventory_refresh",
            title: "Refresh inventory: sandbox",
            approval_required_text: null,
            state: reads === 1 ? "RUNNING" : "SUCCEEDED",
          });
        }
        return undefined;
      },
      { definitions: [refreshDefinition] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open({ kind: "inventory_refresh", environmentSlug: "sandbox" }));
    await screen.findByRole("dialog", { name: "Refresh inventory" });
    expect(headerSteps()).toEqual(["Options", "Run"]);
    expect(screen.queryByRole("button", { name: "Continue to review" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("Succeeded")).toBeInTheDocument();
    expect(stub.posts.map((post) => post.path)).toEqual(["/api/v1/operations"]);
    expect(screen.queryByRole("button", { name: "Approve and run" })).not.toBeInTheDocument();
  });

  it("shows why an import failed in the panel, with its diagnostic and the fix, and the way to its operation", async () => {
    let reads = 0;
    stubApi(
      (path, init) => {
        if (path.endsWith("/api/v1/operations") && init?.method === "POST") {
          return Response.json({ operation_id: "op-1", state: "QUEUED" }, { status: 202 });
        }
        if (path.endsWith("/api/v1/operations/op-1")) {
          reads += 1;
          return Response.json({
            ...base,
            operation_kind: "environment_import",
            environment_slug: null,
            target_key: null,
            resource_arn: null,
            resource_id: null,
            approval_required_text: null,
            title: "Import environment: example-prod",
            state: reads === 1 ? "RUNNING" : "FAILED",
            failure_summary: reads === 1 ? null : "The AWS SSO session of profile example-prod has expired",
            diagnostic_reports:
              reads === 1
                ? []
                : [
                    {
                      id: 3,
                      step_name: "confirm_account",
                      severity: "error",
                      diagnostic_kind: "credentials",
                      title: "The SSO session has expired",
                      detail: "The token cache for example-prod is out of date.",
                      fix_suggestion: "Run aws sso login --profile example-prod, then import again",
                      created_at_utc: "2026-10-10T10:00:01Z",
                    },
                  ],
          });
        }
        return undefined;
      },
      { definitions: [importDefinition] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open({ kind: "environment_import" }));
    await userEvent.type(await screen.findByLabelText("Slug"), "example-prod");
    await userEvent.click(screen.getByRole("button", { name: "Import environment" }));
    expect(
      await screen.findByText(
        "The AWS SSO session of profile example-prod has expired",
        {},
        { timeout: 4000 },
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    const diagnostics = screen.getByRole("heading", { name: "Diagnostics" }).closest("section");
    expect(diagnostics).not.toBeNull();
    const list = within(diagnostics as HTMLElement);
    expect(list.getByText("The SSO session has expired")).toBeInTheDocument();
    expect(
      list.getByText("Fix: Run aws sso login --profile example-prod, then import again"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Preflight checks" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open operation" })).toHaveAttribute("href", "/operations/op-1");
  });

  it("names a drift resolution's button after the choice, and shows the review steps only for a restore", async () => {
    stubApi(() => undefined, { definitions: [driftResolveDefinition] });
    const { task } = renderWithTaskApi();
    act(() =>
      task().open({
        kind: "drift_resolve",
        environmentSlug: "sandbox",
        resourceIdentifier: "demo-subnets",
        initialOptions: { resource_arn: "arn:s", choice: "accept" },
      }),
    );
    await screen.findByRole("dialog", { name: "Resolve drift" });
    expect(screen.getByRole("button", { name: "Accept into the record" })).toBeInTheDocument();
    expect(headerSteps()).toEqual(["Options", "Run"]);
    await userEvent.click(screen.getByRole("radio", { name: "Restore from the record" }));
    expect(screen.getByRole("button", { name: "Continue to review" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept into the record" })).not.toBeInTheDocument();
    expect(headerSteps()).toEqual(["Options", "Review", "Approve"]);
  });
});

const majorUpgradeDefinition = {
  kind: "major_upgrade",
  title: "Major version upgrade",
  group: "Upgrades",
  summary: "Upgrade to a new major version through a blue/green deployment.",
  applies_to: ["instance", "cluster"],
  modes: ["terraform", "direct"],
  destructive: false,
  disruptive: true,
  needs_target: false,
  options_schema: {
    type: "object",
    properties: {
      target_engine_version: {
        type: "string",
        ui: { widget: "engine_version", label: "Target version", order: 1, upgrade_kind: "major" },
      },
    },
  },
};

const GREEN_ENDPOINT = "demo-orders-green-abc123.cluster.eu-west-2.rds.amazonaws.com";

const preflightPassReport = {
  id: 11,
  step_name: "preflight",
  severity: "info",
  diagnostic_kind: "preflight_backups",
  title: "Automated backups",
  detail: "retention 7 days",
  fix_suggestion: "nothing to do",
  created_at_utc: "2026-10-10T10:00:00Z",
};

/** A major upgrade as the operation detail reports it at each of its states. */
function majorUpgradeAt(state: string, stage: "create_green" | "switchover" | null) {
  const atSwitchover = stage === "switchover";
  return {
    ...base,
    operation_kind: "major_upgrade",
    title: "Major version upgrade: demo-orders",
    state,
    approval_required_text: atSwitchover ? "demo-orders" : null,
    diagnostic_reports: [preflightPassReport],
    plan: {
      approval_stage: stage ?? "create_green",
      options: { target_engine_version: "17.2" },
      source_engine_version: "16.4",
      preflight: [{ name: "backups", outcome: "pass", detail: "retention 7 days" }],
      action_review: atSwitchover
        ? {
            what: [
              `switch production traffic from demo-orders to green (${GREEN_ENDPOINT}, 17.2); demo-orders becomes demo-orders-old1 and is kept`,
            ],
            identifiers: ["demo-orders"],
            requires_reboot: false,
            downtime: "about a minute while connections move",
            reconcile: true,
          }
        : {
            what: ["create a green copy of demo-orders on 17.2"],
            identifiers: ["demo-orders"],
            requires_reboot: false,
            downtime: "none",
            reconcile: false,
          },
      ...(stage === "create_green" && state === "AWAITING_APPROVAL"
        ? {}
        : { deployment_id: "bgd-abc123", deployment_name: "rdsc-demo-orders-17-2" }),
      ...(atSwitchover ? { green_endpoint: GREEN_ENDPOINT } : {}),
    },
  };
}

describe("TaskPanel for a major upgrade's two approvals", () => {
  it("shows the review again at the switchover pause, with the green endpoint, the checklist and the identifier to type", async () => {
    const statesBeforeFirstApproval = [majorUpgradeAt("QUEUED", null)];
    const firstPause = majorUpgradeAt("AWAITING_APPROVAL", "create_green");
    const statesAfterFirstApproval = [majorUpgradeAt("RUNNING", "create_green")];
    const switchoverPause = majorUpgradeAt("AWAITING_APPROVAL", "switchover");
    const statesAfterSwitchover = [majorUpgradeAt("RUNNING", "switchover")];
    const finished = majorUpgradeAt("SUCCEEDED", "switchover");
    const stub = stubApi(
      (path, _init, api) => {
        if (path.endsWith("/approve")) return Response.json({ ...base, state: "RUNNING" });
        if (path.endsWith("/api/v1/operations/op-1")) {
          const approvals = api.posts.filter((post) => post.path.endsWith("/approve")).length;
          if (approvals === 0) return Response.json(statesBeforeFirstApproval.shift() ?? firstPause);
          if (approvals === 1) return Response.json(statesAfterFirstApproval.shift() ?? switchoverPause);
          return Response.json(statesAfterSwitchover.shift() ?? finished);
        }
        return undefined;
      },
      { definitions: [majorUpgradeDefinition] },
    );
    const user = userEvent.setup();
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));

    // The first pause: the green copy's review, approved with a click and no green endpoint yet.
    const approveGreen = await screen.findByRole("button", { name: "Approve and run" }, { timeout: 3000 });
    expect(screen.getByText("Create a green copy of demo-orders on 17.2")).toBeInTheDocument();
    expect(screen.queryByText(GREEN_ENDPOINT)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/to confirm/)).not.toBeInTheDocument();
    expect(screen.getByText("Automated backups")).toBeInTheDocument();
    await waitFor(() => expect(approveGreen).toBeEnabled());
    await user.click(approveGreen);

    // The second pause re-opens the review: the green endpoint to test, the checklist, and the identifier.
    const approveSwitchover = await screen.findByRole(
      "button",
      { name: "Approve switchover" },
      { timeout: 3000 },
    );
    const endpoint = screen.getByRole("region", { name: "Green endpoint" });
    expect(within(endpoint).getByText(GREEN_ENDPOINT)).toBeInTheDocument();
    await user.click(within(endpoint).getByRole("button", { name: "Copy" }));
    expect(await navigator.clipboard.readText()).toBe(GREEN_ENDPOINT);
    expect(within(endpoint).getByText("Copied")).toBeInTheDocument();
    expect(screen.getByText("Automated backups")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and run" })).not.toBeInTheDocument();
    expect(approveSwitchover).toBeDisabled();
    await user.type(screen.getByLabelText(/Type demo-orders to confirm/), "demo-orders");
    expect(approveSwitchover).toBeEnabled();
    await user.click(approveSwitchover);
    expect(await screen.findByText("Succeeded", {}, { timeout: 3000 })).toBeInTheDocument();
    const approvalBodies = stub.posts
      .filter((post) => post.path.endsWith("/approve"))
      .map((post) => JSON.parse(post.body) as unknown);
    expect(approvalBodies).toEqual([
      { approval_stage: "create_green", confirmation_text: null },
      { approval_stage: "switchover", confirmation_text: "demo-orders" },
    ]);
  });

  it("shows why an approval of a pause that has passed was refused, with its fix, and brings up the pause the operation is at", async () => {
    const firstPause = majorUpgradeAt("AWAITING_APPROVAL", "create_green");
    const switchoverPause = majorUpgradeAt("AWAITING_APPROVAL", "switchover");
    let operationReadsAfterRefusal = 0;
    const stub = stubApi(
      (path, _init, api) => {
        if (path.endsWith("/approve")) {
          return Response.json(
            {
              detail: {
                diagnostic_kind: "approval_stage_mismatch",
                detail: "the operation waits at switchover, not create_green",
                fix_suggestion: "review the switchover and approve it",
              },
            },
            { status: 409 },
          );
        }
        if (path.endsWith("/api/v1/operations/op-1")) {
          const refused = api.posts.some((post) => post.path.endsWith("/approve"));
          if (!refused) return Response.json(firstPause);
          operationReadsAfterRefusal += 1;
          return Response.json(switchoverPause);
        }
        return undefined;
      },
      { definitions: [majorUpgradeDefinition] },
    );
    const user = userEvent.setup();
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    const approveGreen = await screen.findByRole("button", { name: "Approve and run" }, { timeout: 3000 });
    await waitFor(() => expect(approveGreen).toBeEnabled());
    await user.click(approveGreen);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("the operation waits at switchover, not create_green");
    expect(alert).toHaveTextContent("Fix: review the switchover and approve it");
    expect(
      await screen.findByRole("button", { name: "Approve switchover" }, { timeout: 3000 }),
    ).toBeDisabled();
    expect(operationReadsAfterRefusal).toBeGreaterThan(0);
    expect(stub.posts.filter((post) => post.path.endsWith("/approve"))).toHaveLength(1);
  });

  it("leaves a rejected switchover cancelled", async () => {
    const stub = stubApi(
      (path, _init, api) => {
        if (path.endsWith("/reject")) return Response.json({ ...base, state: "CANCELLED" });
        if (path.endsWith("/api/v1/operations/op-1")) {
          const rejected = api.posts.some((post) => post.path.endsWith("/reject"));
          return Response.json(majorUpgradeAt(rejected ? "CANCELLED" : "AWAITING_APPROVAL", "switchover"));
        }
        return undefined;
      },
      { definitions: [majorUpgradeDefinition] },
    );
    const { task } = renderWithTaskApi();
    act(() => task().open(reviewRequest));
    await screen.findByRole("button", { name: "Approve switchover" });
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve switchover" })).not.toBeInTheDocument();
    expect(stub.posts.map((post) => post.path)).toEqual(["/api/v1/operations/op-1/reject"]);
  });
});
