import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider, type TaskRequest } from "@/features/tasks/TaskContext";
import EnvironmentPage from "./EnvironmentPage";

const openTask = vi.hoisted(() => vi.fn());
vi.mock("@/features/tasks/TaskContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/tasks/TaskContext")>();
  return {
    ...actual,
    useTask: () => {
      const task = actual.useTask();
      return {
        ...task,
        open: (request: TaskRequest) => {
          openTask(request);
          task.open(request);
        },
      };
    },
  };
});

function environment(slug: string, displayName: string, isProduction: boolean, requiresApproval: boolean) {
  return {
    slug,
    display_name: displayName,
    requires_approval: requiresApproval,
    is_production: isProduction,
    change_management: "terraform",
    allow_direct_changes_to_foreign_owned: false,
    targets: [],
  };
}
const bootstrap = {
  operator_name: "phil",
  console_version: "0.2.0",
  csrf_token: "t",
  environments: [
    environment("sandbox", "Sandbox", false, false),
    environment("production", "Production", true, true),
  ],
};

function overviewOf(slug: string, displayName: string, isProduction: boolean) {
  return {
    slug,
    display_name: displayName,
    is_production: isProduction,
    change_management: "terraform",
    allow_direct_changes_to_foreign_owned: false,
    inventory_refresh_minutes: 15,
    drift_check_hours: 24,
    counts: { instance: 4, cluster: 1, parameter_group: 2, snapshot: 3 },
    ownership_counts: { unmanaged: 7 },
    engine_versions: { "16.4": 5 },
    last_refresh_at_utc: "2026-10-10T10:00:00Z",
    running_operation_count: 1,
    attention_count: 3,
  };
}

function action(kind: string, title: string, group = "Discovery and adoption") {
  return {
    kind,
    title,
    group,
    summary: "",
    destructive: false,
    disruptive: false,
    applicable: true,
    reason: "",
  };
}

const ordersArn = "arn:aws:rds:eu-west-2:000000000000:db:demo-orders";

function pageOf(slug: string, displayName: string, isProduction = false) {
  return {
    overview: overviewOf(slug, displayName, isProduction),
    targets: [
      {
        target_key: `${slug}/000000000000/eu-west-2`,
        aws_account_id: "000000000000",
        aws_region: "eu-west-2",
        aws_profile_name: null,
        simulated: true,
        last_snapshot_at_utc: "2026-10-10T09:00:00Z",
        workspace_relative_dir: `terraform/${slug}/eu-west-2`,
        last_applied_commit_sha: "abcdef1234567890",
      },
    ],
    actions: [
      action("inventory_refresh", "Refresh inventory"),
      action("drift_resolve", "Resolve drift"),
      action("environment_import", "Import environment", "Environments"),
      action("instance_create", "Create instance", "Create and remove"),
      action("cluster_create", "Create cluster", "Create and remove"),
    ],
    recent_operations: [
      {
        id: "0b0f6a4e-0000-4000-8000-000000000001",
        title: "Refresh inventory: sandbox",
        state: "SUCCEEDED",
        operation_kind: "inventory_refresh",
        environment_slug: slug,
        target_key: null,
        created_by_operator_name: "phil",
        approved_by_operator_name: null,
        failure_summary: null,
        created_at_utc: "2026-10-10T10:00:00Z",
        updated_at_utc: "2026-10-10T10:00:00Z",
        progress: null,
      },
    ],
    change_events: [
      {
        id: 1,
        resource_arn: ordersArn,
        resource_id: "orders-id",
        environment_slug: slug,
        target_key: "t",
        attribute_path: "db_instance_class",
        value_before: "db.m6g.large",
        value_after: "db.r6g.large",
        explanation: "outside_console",
        operation_id: null,
        detected_at_utc: "2026-10-10T10:00:00Z",
        reviewed_by_operator_name: null,
        reviewed_at_utc: null,
      },
    ],
    drift: [
      {
        resource_arn: ordersArn,
        resource_id: "orders-id",
        finding_kind: "record",
        attributes: [
          {
            address: "aws_db_instance.demo_orders",
            attribute: "db_instance_class",
            recorded: "db.m6g.large",
            live: "db.r6g.large",
          },
          { address: "aws_db_instance.demo_orders", attribute: "multi_az", recorded: false, live: true },
        ],
        detected_at_utc: "2026-10-10T10:00:00Z",
      },
      {
        resource_arn: "aws_db_instance.demo_gone",
        resource_id: null,
        finding_kind: "state",
        attributes: [
          { address: "aws_db_instance.demo_gone", attribute: "presence", recorded: true, live: false },
        ],
        detected_at_utc: "2026-10-10T11:00:00Z",
      },
    ],
    first_steps: { imported: true, refreshed: true, adopted_or_direct: true, mode_set: true },
  };
}

const catalogue = {
  definitions: [
    { kind: "inventory_refresh", title: "Refresh inventory" },
    { kind: "drift_resolve", title: "Resolve drift" },
  ].map(({ kind, title }) => ({
    kind,
    title,
    group: "Discovery and adoption",
    summary: "",
    applies_to: ["environment"],
    modes: ["terraform", "direct"],
    destructive: false,
    disruptive: false,
    needs_target: false,
    pauses_for_approval: kind !== "inventory_refresh",
    pauses_for_approval_when: kind === "drift_resolve" ? { accept: false, restore: true } : null,
    options_schema:
      kind === "drift_resolve"
        ? {
            type: "object",
            properties: {
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
            },
          }
        : { type: "object", properties: {} },
  })),
};

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
  openTask.mockClear();
});

/** Answers `/environments/{slug}` from `pages`; the number of reads of each is kept in `reads`. */
function stubApi(pages: Record<string, () => Response | ReturnType<typeof pageOf>>) {
  const reads: Record<string, number> = {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), "http://console.test").pathname;
      if (path === "/api/v1/bootstrap") return Response.json(bootstrap);
      if (path === "/api/v1/catalogue") return Response.json(catalogue);
      const slug = path.replace("/api/v1/environments/", "");
      const page = pages[slug];
      if (page) {
        reads[slug] = (reads[slug] ?? 0) + 1;
        const answer = page();
        return answer instanceof Response ? answer : Response.json(answer);
      }
      throw new Error(`unexpected ${path}`);
    }),
  );
  return reads;
}

function renderAt(address: string) {
  render(
    <MemoryRouter initialEntries={[address]}>
      <EnvironmentProvider>
        <TaskProvider>
          <Routes>
            <Route path="/environments/:slug" element={<EnvironmentPage />} />
          </Routes>
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

describe("EnvironmentPage", () => {
  it("shows the mode, settings, counts and last refresh, with the environment marked", async () => {
    stubApi({ production: () => pageOf("production", "Production", true) });
    renderAt("/environments/production");
    const title = await screen.findByRole("heading", { level: 1, name: "production" });
    expect(within(title).getByText("production")).toHaveClass("font-mono");
    expect(screen.getByText("production", { ignore: "h1 span" })).toBeInTheDocument();
    const settings = screen.getByRole("region", { name: "Settings" });
    expect(within(settings).getByText("Change management")).toBeInTheDocument();
    expect(within(settings).getByText("terraform")).toBeInTheDocument();
    expect(within(settings).getByText("Foreign-owned changes allowed")).toBeInTheDocument();
    expect(within(settings).getByText("Inventory refresh every")).toBeInTheDocument();
    expect(within(settings).getByText("15 minutes")).toBeInTheDocument();
    expect(within(settings).getByText("Drift check every")).toBeInTheDocument();
    expect(within(settings).getByText("24 hours")).toBeInTheDocument();
    expect(within(settings).getByText("Approval required")).toBeInTheDocument();
    expect(within(settings).getByText("yes")).toBeInTheDocument();
    expect(within(settings).getByText(new Date("2026-10-10T10:00:00Z").toLocaleString())).toBeInTheDocument();
    const counts = screen.getByRole("region", { name: "Resources" });
    expect(within(counts).getByText("Instances").nextSibling).toHaveTextContent("4");
    expect(within(counts).getByText("Snapshots").nextSibling).toHaveTextContent("3");
  });

  it("lists the targets with account, region, profile, snapshot, workspace and commit", async () => {
    stubApi({ sandbox: () => pageOf("sandbox", "Sandbox") });
    renderAt("/environments/sandbox");
    const targets = await screen.findByRole("region", { name: "Targets" });
    const row = within(targets).getByRole("row", { name: /sandbox\/000000000000\/eu-west-2/ });
    expect(within(row).getByText("sandbox/000000000000/eu-west-2")).toHaveClass("font-mono");
    expect(within(row).getByText("000000000000")).toBeInTheDocument();
    expect(within(row).getByText("eu-west-2")).toBeInTheDocument();
    expect(within(row).getByText("simulated")).toBeInTheDocument();
    expect(within(row).getByText("terraform/sandbox/eu-west-2")).toBeInTheDocument();
    expect(within(row).getByText("abcdef12")).toHaveAttribute("title", "abcdef1234567890");
    expect(within(row).getByText(new Date("2026-10-10T09:00:00Z").toLocaleString())).toBeInTheDocument();
  });

  it("opens environment-level tasks on this environment, whichever is selected", async () => {
    stubApi({ production: () => pageOf("production", "Production", true) });
    renderAt("/environments/production");
    await screen.findByRole("heading", { level: 1, name: "production" });
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Refresh inventory/ }));
    expect(openTask).toHaveBeenCalledWith({ kind: "inventory_refresh", environmentSlug: "production" });
  });

  it("creates an instance or a cluster in this environment from its Actions", async () => {
    stubApi({ production: () => pageOf("production", "Production", true) });
    renderAt("/environments/production");
    await screen.findByRole("heading", { level: 1, name: "production" });
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    expect(await screen.findByRole("menuitem", { name: /Create cluster/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: /Create instance/ }));
    expect(openTask).toHaveBeenCalledWith({ kind: "instance_create", environmentSlug: "production" });
  });

  it("leaves out the tasks that are started from elsewhere", async () => {
    stubApi({ production: () => pageOf("production", "Production", true) });
    renderAt("/environments/production");
    await screen.findByRole("heading", { level: 1, name: "production" });
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    expect(await screen.findByRole("menuitem", { name: /Refresh inventory/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Resolve drift/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Import environment/ })).not.toBeInTheDocument();
  });

  it("links each drift finding to its resource, and offers to accept one that has left the inventory", async () => {
    stubApi({ sandbox: () => pageOf("sandbox", "Sandbox") });
    renderAt("/environments/sandbox");
    const drift = await screen.findByRole("region", { name: "Open drift" });
    expect(within(drift).getByRole("link", { name: "demo-orders" })).toHaveAttribute(
      "href",
      "/resources/orders-id",
    );
    expect(within(drift).getByText("db_instance_class, multi_az")).toHaveClass("font-mono");
    expect(within(drift).getByText("aws_db_instance.demo_gone")).toHaveClass("font-mono");
    expect(within(drift).getByText("In the record, missing from AWS")).toBeInTheDocument();
    expect(within(drift).queryByRole("link", { name: "aws_db_instance.demo_gone" })).not.toBeInTheDocument();
    expect(within(drift).getAllByRole("button", { name: /Accept into the record/ })).toHaveLength(1);
    await userEvent.click(
      within(drift).getByRole("button", { name: "Accept into the record: aws_db_instance.demo_gone" }),
    );
    expect(openTask).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "drift_resolve",
        environmentSlug: "sandbox",
        initialOptions: { resource_arn: "aws_db_instance.demo_gone", choice: "accept" },
      }),
    );
  });

  it("names the finding's resource in the panel that accepts it, and says the button commits it", async () => {
    stubApi({ sandbox: () => pageOf("sandbox", "Sandbox") });
    renderAt("/environments/sandbox");
    const drift = await screen.findByRole("region", { name: "Open drift" });
    await userEvent.click(
      within(drift).getByRole("button", { name: "Accept into the record: aws_db_instance.demo_gone" }),
    );
    const panel = await screen.findByRole("dialog", { name: "Resolve drift" });
    expect(panel).toHaveAccessibleDescription(/aws_db_instance\.demo_gone/);
    expect(await within(panel).findByRole("button", { name: "Accept into the record" })).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Continue to review" })).not.toBeInTheDocument();
  });

  it("says when there is no drift", async () => {
    stubApi({ sandbox: () => ({ ...pageOf("sandbox", "Sandbox"), drift: [] }) });
    renderAt("/environments/sandbox");
    expect(await screen.findByText("Matches the record.")).toBeInTheDocument();
  });

  it("shows recent operations, and changes with the resource each belongs to", async () => {
    stubApi({ sandbox: () => pageOf("sandbox", "Sandbox") });
    renderAt("/environments/sandbox");
    const operations = await screen.findByRole("region", { name: "Recent operations" });
    expect(within(operations).getByRole("link", { name: "Refresh inventory: sandbox" })).toHaveAttribute(
      "href",
      "/operations/0b0f6a4e-0000-4000-8000-000000000001",
    );
    const changes = screen.getByRole("region", { name: "Change feed" });
    expect(within(changes).getByRole("link", { name: "demo-orders" })).toHaveAttribute(
      "href",
      "/resources/orders-id",
    );
    expect(within(changes).getByText("db_instance_class")).toBeInTheDocument();
    expect(within(changes).getByText("outside the console")).toBeInTheDocument();
  });

  it("offers a comparison starting from this environment", async () => {
    stubApi({ sandbox: () => pageOf("sandbox", "Sandbox") });
    renderAt("/environments/sandbox");
    expect(await screen.findByRole("link", { name: "Compare with…" })).toHaveAttribute(
      "href",
      "/environments/compare?left=sandbox",
    );
  });

  it("reads the page again when the task panel closes", async () => {
    const reads = stubApi({ sandbox: () => pageOf("sandbox", "Sandbox") });
    renderAt("/environments/sandbox");
    await screen.findByRole("heading", { level: 1, name: "sandbox" });
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Refresh inventory/ }));
    const dialog = await screen.findByRole("dialog", { name: "Refresh inventory" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(reads.sandbox).toBe(2));
  });

  it("says why the environment could not be loaded and points back to the list", async () => {
    stubApi({ nowhere: () => Response.json({ detail: "no environment named nowhere" }, { status: 404 }) });
    renderAt("/environments/nowhere");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load this environment: no environment named nowhere",
    );
    expect(screen.getByRole("link", { name: "Back to the environments" })).toHaveAttribute(
      "href",
      "/environments",
    );
  });
});
