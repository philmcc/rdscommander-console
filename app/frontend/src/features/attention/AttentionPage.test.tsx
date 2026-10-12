import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { AttentionInbox, AttentionItem } from "@/api/types";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { OperationDetail } from "@/features/operations/OperationDetail";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider } from "@/features/tasks/TaskContext";
import AttentionPage from "./AttentionPage";

const bootstrap = {
  company_display_name: "T",
  operator_name: "phil",
  engine_version: "0.2.0",
  csrf_token: "t",
  environments: [
    {
      slug: "sandbox",
      display_name: "Sandbox",
      requires_approval: true,
      is_production: false,
      change_management: "terraform",
      allow_direct_changes_to_foreign_owned: false,
      targets: [],
    },
  ],
};

function row(overrides: Partial<AttentionItem>): AttentionItem {
  return {
    kind: "drift",
    title: "",
    detail: "",
    environment_slug: "sandbox",
    operation_id: null,
    resource_id: null,
    resource_identifier: null,
    action_kind: null,
    action_options: {},
    ...overrides,
  };
}

const emptyInbox: AttentionInbox = {
  awaiting_approval: [],
  pending_reboot: [],
  pending_maintenance: [],
  drift: [],
  outside_changes: [],
  failed_recent: [],
  foreign_owned: [],
  unmanaged: [],
  blue_side: [],
};

const inbox = {
  awaiting_approval: [
    {
      kind: "awaiting_approval",
      title: "Adopt resources: demo-orders",
      detail: "created by phil",
      environment_slug: "sandbox",
      operation_id: "op-1",
      resource_id: null,
      resource_identifier: null,
      action_kind: null,
      action_options: {},
    },
  ],
  pending_reboot: [
    {
      kind: "pending_reboot",
      title: "demo-orders needs a reboot",
      detail: "parameter changes are pending a reboot",
      environment_slug: "sandbox",
      resource_id: "r1",
      resource_identifier: "demo-orders",
      operation_id: null,
      action_kind: "reboot_instance",
      action_options: {},
    },
  ],
  pending_maintenance: [],
  drift: [],
  outside_changes: [],
  failed_recent: [],
  foreign_owned: [],
  unmanaged: [
    {
      kind: "unmanaged",
      title: "demo-aurora is not under Terraform",
      detail: "adopt it",
      environment_slug: "sandbox",
      resource_id: "r2",
      resource_identifier: "demo-aurora",
      operation_id: null,
      action_kind: "terraform_adoption",
      action_options: { target_key: "t", resource_arns: ["arn:c"] },
    },
  ],
  blue_side: [],
};

function definition(kind: string, title: string, properties: Record<string, unknown> = {}) {
  return {
    kind,
    title,
    group: "Parameter groups",
    summary: "",
    applies_to: ["instance"],
    modes: ["terraform", "direct"],
    destructive: false,
    disruptive: true,
    needs_target: false,
    pauses_for_approval: true,
    pauses_for_approval_when: kind === "drift_resolve" ? { accept: false, restore: true } : null,
    options_schema: { type: "object", properties },
  };
}

/** Drift resolution's options as the catalogue describes them: the resource, and accept or restore. */
const driftResolveProperties = {
  resource_arn: { type: "string", ui: { widget: "hidden", label: "Resource" } },
  choice: {
    type: "string",
    enum: ["accept", "restore"],
    ui: {
      widget: "choice",
      label: "Resolution",
      labels: { accept: "Accept into the record", restore: "Restore from the record" },
    },
  },
};

function Location() {
  const location = useLocation();
  return <p data-testid="location">{`${location.pathname}${location.hash}`}</p>;
}

function stubFetch(
  attention: () => Response | Promise<Response>,
  definitions = [definition("reboot_instance", "Reboot instance")],
) {
  const posted: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.includes("/api/v1/attention")) return attention();
      if (url.endsWith("/api/v1/catalogue")) return Response.json({ definitions });
      if (url.endsWith("/api/v1/operations") && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return Response.json({ detail: "not now" }, { status: 409 });
      }
      throw new Error(`unexpected ${url}`);
    }),
  );
  return posted;
}

function renderPage() {
  render(
    <MemoryRouter initialEntries={["/attention"]}>
      <EnvironmentProvider>
        <TaskProvider>
          <AttentionPage />
          <Location />
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
});

describe("AttentionPage", () => {
  it("lists sections with counts and opens the resolving action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        if (url.includes("/api/v1/attention")) return Response.json(inbox);
        if (url.endsWith("/api/v1/catalogue"))
          return Response.json({
            definitions: [
              {
                kind: "reboot_instance",
                title: "Reboot instance",
                group: "Parameter groups",
                summary: "",
                applies_to: ["instance"],
                modes: ["terraform", "direct"],
                destructive: false,
                disruptive: true,
                needs_target: false,
                options_schema: { type: "object", properties: {} },
              },
            ],
          });
        throw new Error(`unexpected ${url}`);
      }),
    );
    render(
      <MemoryRouter>
        <EnvironmentProvider>
          <TaskProvider>
            <AttentionPage />
          </TaskProvider>
        </EnvironmentProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Awaiting approval (1)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Pending reboot (1)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Not under Terraform (1)" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reboot instance" }));
    expect(await screen.findByRole("dialog", { name: "Reboot instance" })).toBeInTheDocument();
  });

  it("shows only the sections that have rows, in the inbox's order", async () => {
    stubFetch(() => Response.json(inbox));
    renderPage();
    await screen.findByRole("heading", { name: "Awaiting approval (1)" });
    expect(screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent)).toEqual([
      "Awaiting approval (1)",
      "Pending reboot (1)",
      "Not under Terraform (1)",
    ]);
    expect(screen.queryByText(/Nothing needs attention/)).not.toBeInTheDocument();
  });

  it("says so when nothing needs attention", async () => {
    stubFetch(() => Response.json(emptyInbox));
    renderPage();
    expect(await screen.findByText("Nothing needs attention in this environment.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
  });

  it("says why the inbox could not be loaded", async () => {
    stubFetch(() => new Response("boom", { status: 500 }));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the inbox");
    expect(screen.queryByText(/Nothing needs attention/)).not.toBeInTheDocument();
  });

  it("labels the button with the resolving task's catalogue title", async () => {
    stubFetch(() => Response.json(inbox), [definition("reboot_instance", "Reboot pending instances")]);
    renderPage();
    const button = await screen.findByRole("button", { name: "Reboot pending instances" });
    // The adoption task is not in this catalogue, so its row keeps the section's own label.
    expect(screen.getByRole("button", { name: "Adopt resources" })).toBeInTheDocument();
    await userEvent.click(button);
    expect(await screen.findByRole("dialog", { name: "Reboot pending instances" })).toBeInTheDocument();
  });

  it("describes each button by its row, so repeated labels stay distinguishable", async () => {
    stubFetch(() => Response.json(inbox));
    renderPage();
    const button = await screen.findByRole("button", { name: "Reboot instance" });
    expect(button).toHaveAccessibleDescription(
      "demo-orders needs a reboot parameter changes are pending a reboot",
    );
  });

  it("shows identifiers in monospace", async () => {
    stubFetch(() => Response.json(inbox));
    renderPage();
    const reboot = (await screen.findByRole("button", { name: "Reboot instance" })).closest("li");
    expect(reboot).not.toBeNull();
    expect(within(reboot as HTMLElement).getByText("demo-orders")).toHaveClass("font-mono");
    const approval = screen.getByRole("button", { name: "Review" }).closest("li");
    expect(within(approval as HTMLElement).getByText("demo-orders")).toHaveClass("font-mono");
  });

  it("opens a drift row that has no page with the acceptance ready", async () => {
    const posted = stubFetch(
      () =>
        Response.json({
          ...emptyInbox,
          drift: [
            row({
              title: "Drift on demo-subnets",
              detail: "subnet_group: description",
              resource_identifier: "demo-subnets",
              action_kind: "drift_resolve",
              action_options: { resource_arn: "arn:s", choice: "accept" },
            }),
          ],
        }),
      [definition("drift_resolve", "Resolve drift", driftResolveProperties)],
    );
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Resolve drift" }));
    // Accepting commits the record at once, so the button says so instead of promising a review.
    await userEvent.click(await screen.findByRole("button", { name: "Accept into the record" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      kind: "drift_resolve",
      environment_slug: "sandbox",
      resource_id: null,
      options: { resource_arn: "arn:s", choice: "accept" },
    });
  });

  it("takes rows that have no resolving task to the resource or the operation", async () => {
    stubFetch(() =>
      Response.json({
        ...emptyInbox,
        pending_maintenance: [
          row({ kind: "pending_maintenance", title: "demo-orders: system-update", resource_id: "r8" }),
        ],
        outside_changes: [
          row({ kind: "outside_change", title: "demo-orders: instance_class", resource_id: "r9" }),
        ],
        failed_recent: [
          row({
            kind: "failed_recent",
            title: "Reboot: demo-orders",
            operation_id: "op-f",
            resource_id: "r9",
          }),
        ],
        foreign_owned: [
          row({ kind: "foreign_owned", title: "demo-legacy is foreign terraform", resource_id: "r7" }),
        ],
      }),
    );
    renderPage();
    const location = () => screen.getByTestId("location").textContent;
    const maintenance = await screen.findByRole("region", { name: "Pending maintenance (1)" });
    await userEvent.click(within(maintenance).getByRole("button", { name: "Open resource" }));
    expect(location()).toBe("/resources/r8");
    await userEvent.click(screen.getByRole("button", { name: "Review change" }));
    expect(location()).toBe("/resources/r9#history");
    const foreignOwned = screen.getByRole("region", { name: "Owned by other tooling (1)" });
    await userEvent.click(within(foreignOwned).getByRole("button", { name: "Open resource" }));
    expect(location()).toBe("/resources/r7");
    await userEvent.click(screen.getByRole("button", { name: "Open operation" }));
    expect(location()).toBe("/operations/op-f");
  });

  it("repeats nothing when two rows read the same", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const same = row({
      kind: "pending_maintenance",
      title: "demo-orders: system-update",
      detail: "now",
      resource_id: "r8",
    });
    stubFetch(() => Response.json({ ...emptyInbox, pending_maintenance: [same, same] }));
    renderPage();
    expect(await screen.findByRole("heading", { name: "Pending maintenance (2)" })).toBeInTheDocument();
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });
});

function OperationAtAddress() {
  const { operationId } = useParams();
  return <OperationDetail operationId={operationId ?? ""} />;
}

describe("AttentionPage and operations of no environment", () => {
  it("lists a failed import, which has no environment, and opens its operation where the diagnostic shows", async () => {
    const failedImport = row({
      kind: "failed_recent",
      title: "Import environment: acme-prod",
      detail: "The AWS SSO session of profile acme-prod has expired",
      environment_slug: "",
      operation_id: "op-import",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        if (url.includes("/api/v1/attention"))
          return Response.json({ ...emptyInbox, failed_recent: [failedImport] });
        if (url.endsWith("/api/v1/catalogue")) return Response.json({ definitions: [] });
        if (url.endsWith("/api/v1/operations/op-import")) {
          return Response.json({
            id: "op-import",
            operation_kind: "environment_import",
            title: "Import environment: acme-prod",
            state: "FAILED",
            environment_slug: null,
            target_key: null,
            resource_arn: null,
            resource_id: null,
            approval_required_text: null,
            created_by_operator_name: "phil",
            approved_by_operator_name: null,
            failure_summary: "The AWS SSO session of profile acme-prod has expired",
            created_at_utc: "2026-10-11T08:15:00Z",
            updated_at_utc: "2026-10-11T08:15:01Z",
            finished_at_utc: "2026-10-11T08:15:01Z",
            plan: {},
            step_attempts: [],
            diagnostic_reports: [
              {
                id: 1,
                step_name: "confirm_account",
                severity: "error",
                diagnostic_kind: "credentials",
                title: "The SSO session has expired",
                detail: "",
                fix_suggestion: "Run aws sso login --profile acme-prod, then import again",
                created_at_utc: "2026-10-11T08:15:01Z",
              },
            ],
            execution_records: [],
            terraform_runs: [],
            change_set: null,
            requires_reboot: false,
            downtime: "none",
          });
        }
        throw new Error(`unexpected ${url}`);
      }),
    );
    render(
      <MemoryRouter initialEntries={["/attention"]}>
        <EnvironmentProvider>
          <TaskProvider>
            <Routes>
              <Route path="/attention" element={<AttentionPage />} />
              <Route path="/operations/:operationId" element={<OperationAtAddress />} />
            </Routes>
          </TaskProvider>
        </EnvironmentProvider>
      </MemoryRouter>,
    );
    const failed = await screen.findByRole("region", { name: "Failed in the last seven days (1)" });
    const importRow = within(failed).getByRole("listitem");
    expect(importRow).toHaveTextContent(
      "Import environment: acme-prod The AWS SSO session of profile acme-prod has expired",
    );
    expect(within(importRow).queryByText("Sandbox")).not.toBeInTheDocument();
    await userEvent.click(within(importRow).getByRole("button", { name: "Open operation" }));
    expect(
      await screen.findByText("Fix: Run aws sso login --profile acme-prod, then import again"),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Import environment: acme-prod" })).toBeInTheDocument();
  });
});

describe("AttentionPage after a major upgrade", () => {
  it("lists the blue side after an upgrade, offers its Decommission, and otherwise leads to the upgrade with the reason", async () => {
    const posted = stubFetch(
      () =>
        Response.json({
          ...emptyInbox,
          blue_side: [
            row({
              kind: "blue_side",
              title: "demo-orders-old1 is the blue side of a major upgrade",
              detail: "kept after a major upgrade switched production to the green side",
              resource_id: "r-old",
              resource_identifier: "demo-orders-old1",
              operation_id: "op-upgrade",
              action_kind: "decommission",
            }),
            row({
              kind: "blue_side",
              title: "demo-billing-old1 is the blue side of a major upgrade",
              detail: "kept after a major upgrade; demo-billing-old1 has deletion protection on",
              resource_id: "r-billing-old",
              resource_identifier: "demo-billing-old1",
              operation_id: "op-billing-upgrade",
              action_kind: null,
            }),
          ],
        }),
      [{ ...definition("decommission", "Decommission"), destructive: true }],
    );
    renderPage();
    const blueSide = await screen.findByRole("region", { name: "Blue side after upgrade (2)" });
    const decommission = within(blueSide).getByRole("button", { name: "Decommission" });
    expect(decommission).toHaveAccessibleDescription(/demo-orders-old1 is the blue side of a major upgrade/);
    expect(within(blueSide).getByText("demo-orders-old1")).toHaveClass("font-mono");
    expect(within(blueSide).getByText(/has deletion protection on/)).toBeInTheDocument();
    await userEvent.click(within(blueSide).getByRole("button", { name: "Open operation" }));
    expect(screen.getByTestId("location").textContent).toBe("/operations/op-billing-upgrade");
    await userEvent.click(decommission);
    const panel = await screen.findByRole("dialog", { name: "Decommission" });
    expect(within(panel).getByText("demo-orders-old1")).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Continue to review" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ kind: "decommission", resource_id: "r-old" });
  });

  it("offers to clean up the deployment a rejected switchover left in place, with the deployment filled in", async () => {
    const posted = stubFetch(
      () =>
        Response.json({
          ...emptyInbox,
          failed_recent: [
            row({
              kind: "cancelled_recent",
              title: "Major version upgrade: demo-orders",
              detail: "rejected by phil; blue/green deployment bgd-abc123 is left in place",
              resource_id: "r1",
              resource_identifier: "demo-orders",
              operation_id: "op-upgrade",
              action_kind: "blue_green_cleanup",
              action_options: { deployment_id: "bgd-abc123", delete_green: true },
            }),
          ],
        }),
      [
        {
          ...definition("blue_green_cleanup", "Clean up blue/green deployment", {
            delete_green: {
              type: "boolean",
              default: true,
              ui: { widget: "boolean", label: "Delete the green copy", order: 1 },
            },
            deployment_id: { type: "string", ui: { widget: "hidden", label: "Deployment", order: 2 } },
          }),
          destructive: true,
        },
      ],
    );
    renderPage();
    const failed = await screen.findByRole("region", { name: "Failed in the last seven days (1)" });
    expect(within(failed).getByText(/bgd-abc123 is left in place/)).toBeInTheDocument();
    await userEvent.click(within(failed).getByRole("button", { name: "Clean up blue/green deployment" }));
    const panel = await screen.findByRole("dialog", { name: "Clean up blue/green deployment" });
    expect(within(panel).getByRole("switch", { name: "Delete the green copy" })).toBeChecked();
    await userEvent.click(within(panel).getByRole("button", { name: "Continue to review" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      kind: "blue_green_cleanup",
      resource_id: "r1",
      options: { deployment_id: "bgd-abc123", delete_green: true },
    });
  });
});
