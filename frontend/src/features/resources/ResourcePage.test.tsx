import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider, type TaskRequest } from "@/features/tasks/TaskContext";
import ResourcePage from "./ResourcePage";

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
      change_management: "terraform",
      allow_direct_changes_to_foreign_owned: false,
      targets: [],
    },
  ],
};

const catalogue = {
  definitions: ["parameter_group_attach", "drift_resolve"].map((kind) => ({
    kind,
    title: kind === "drift_resolve" ? "Resolve drift" : "Attach parameter group",
    group: "Parameter groups",
    summary: "",
    applies_to: ["instance"],
    modes: ["terraform", "direct"],
    destructive: false,
    disruptive: kind === "parameter_group_attach",
    needs_target: false,
    options_schema: { type: "object", properties: {} },
  })),
};

/** `resourceIdFromArn` of the ARNs below, so link targets can be asserted. */
const ID_OF_ARN: Record<string, string> = {
  "arn:i": "YXJuOmk",
  "arn:replica": "YXJuOnJlcGxpY2E",
  "arn:cluster": "YXJuOmNsdXN0ZXI",
  "arn:group": "YXJuOmdyb3Vw",
  "arn:writer": "YXJuOndyaXRlcg",
  "arn:reader": "YXJuOnJlYWRlcg",
  "arn:snap": "YXJuOnNuYXA",
};

type Attributes = Record<string, unknown>;

function record(
  kind: string,
  identifier: string,
  arn: string,
  overrides: Partial<{
    status: string;
    ownership: string;
    facts: Attributes;
    relationships: Attributes;
    raw: Attributes;
    terraform_address: string | null;
    parameter_apply_status: string | null;
  }> = {},
) {
  return {
    kind,
    arn,
    identifier,
    environment_slug: "sandbox",
    target_key: "t",
    ownership: "unmanaged",
    status: "available",
    facts: {},
    relationships: {},
    raw: {},
    snapshot_id: 1,
    terraform_address: null,
    parameter_apply_status: null,
    resource_id: ID_OF_ARN[arn] ?? arn,
    ...overrides,
  };
}

const instanceRecord = record("instance", "demo-orders", "arn:i", {
  ownership: "adopted",
  terraform_address: "aws_db_instance.demo_orders",
  parameter_apply_status: "pending-reboot",
  facts: {
    engine: "postgres",
    engine_version: "16.4",
    db_instance_class: "db.m6g.large",
    multi_az: true,
    allocated_storage_gb: 100,
    max_allocated_storage_gb: 200,
    storage_type: "gp3",
    backup_retention_period: 7,
    preferred_backup_window: "03:00-04:00",
    preferred_maintenance_window: "sun:05:00-sun:06:00",
    performance_insights_enabled: true,
    monitoring_interval: 60,
    storage_encrypted: true,
    deletion_protection: false,
  },
  relationships: {
    parameter_group_name: "demo-postgres16-tuned",
    cluster_identifier: null,
    read_replica_source_identifier: null,
    replica_identifiers: ["demo-orders-replica"],
  },
});

const instanceActions = [
  {
    kind: "parameter_group_attach",
    title: "Attach parameter group",
    group: "Parameter groups",
    summary: "",
    destructive: false,
    disruptive: true,
    applicable: true,
    reason: "",
  },
  {
    kind: "terraform_adoption",
    title: "Adopt resources",
    group: "Discovery and adoption",
    summary: "",
    destructive: false,
    disruptive: false,
    applicable: false,
    reason: "already adopted",
  },
];

const outsideChange = {
  id: 1,
  resource_arn: "arn:i",
  resource_id: "YXJuOmk",
  environment_slug: "sandbox",
  target_key: "t",
  attribute_path: "db_instance_class",
  value_before: "db.m6g.large",
  value_after: "db.r6g.large",
  explanation: "outside_console",
  operation_id: null,
  detected_at_utc: "2026-10-10T10:00:00Z",
  reviewed_by_operator_name: null,
  reviewed_at_utc: null,
};

const ordersMaintenance = [
  {
    action: "system-update",
    description: "New OS patch",
    auto_applied_after_utc: "2026-11-01T03:00:00Z",
    current_apply_utc: "2026-10-20T02:00:00Z",
    opt_in_status: "next-maintenance",
  },
  {
    action: "db-upgrade",
    description: "Minor version upgrade",
    auto_applied_after_utc: null,
    current_apply_utc: null,
    opt_in_status: null,
  },
];

const instancePage = {
  record: instanceRecord,
  actions: instanceActions,
  pending_maintenance: ordersMaintenance,
  recent_operations: [
    {
      id: "0b0f6a4e-0000-4000-8000-000000000001",
      title: "Reboot instance: demo-orders",
      state: "SUCCEEDED",
      operation_kind: "reboot_instance",
      created_at_utc: "2026-10-09T10:00:00Z",
    },
  ],
  change_events: [outsideChange],
  drift: [
    {
      finding_kind: "record",
      attributes: [
        {
          address: "aws_db_instance.demo_orders",
          attribute: "instance_class",
          recorded: "db.m6g.large",
          live: "db.r6g.large",
        },
      ],
      detected_at_utc: "2026-10-10T10:00:00Z",
    },
  ],
};

const snapshotOfOrders = record("snapshot", "orders-nightly", "arn:snap", {
  facts: {
    snapshot_scope: "instance",
    snapshot_type: "automated",
    engine_version: "16.4",
    allocated_storage_gb: 100,
    created_at_source_utc: "2026-10-09T03:00:00+00:00",
    tags: { team: "orders", tier: "gold" },
  },
  relationships: { source_identifier: "demo-orders" },
});
const snapshotOfAnotherDatabase = record("snapshot", "billing-nightly", "arn:billing-snap", {
  facts: { snapshot_scope: "instance", snapshot_type: "automated" },
  relationships: { source_identifier: "demo-billing" },
});
const attachedGroup = record("parameter_group", "demo-postgres16-tuned", "arn:group", {
  facts: { group_scope: "instance", parameter_group_family: "postgres16", used_by: ["demo-orders"] },
});
const replicaInstance = record("instance", "demo-orders-replica", "arn:replica", {
  facts: { db_instance_class: "db.m6g.large" },
  relationships: { read_replica_source_identifier: "demo-orders" },
});

type Answer = unknown | ((request: { url: URL; init?: RequestInit }) => unknown);

/**
 * Answers by `METHOD /path`; anything not listed fails loudly. An answer that is a `Response` is sent as it
 * is (a refusal, say), any other is sent as JSON. The returned list records every request.
 */
function serve(routes: Record<string, Answer>) {
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      const key = `${init?.method ?? "GET"} ${url.pathname}`;
      requested.push(`${key}${url.search}`);
      const answer = routes[key];
      if (answer === undefined) throw new Error(`unexpected ${key}`);
      const body = typeof answer === "function" ? answer({ url, init }) : answer;
      return body instanceof Response ? body : Response.json(body);
    }),
  );
  return requested;
}

const commonRoutes = {
  "GET /api/v1/bootstrap": bootstrap,
  "GET /api/v1/catalogue": catalogue,
};

/** Routes that answer the lists an instance page reads next to its own record. */
const instanceRelatedRoutes = {
  "GET /api/v1/environments/sandbox/resources": {
    resources: [instanceRecord, replicaInstance, attachedGroup, snapshotOfOrders, snapshotOfAnotherDatabase],
  },
};

function renderPage(resourceId: string, hash = "") {
  return render(
    <MemoryRouter initialEntries={[`/resources/${resourceId}${hash}`]}>
      <EnvironmentProvider>
        <TaskProvider>
          <Routes>
            <Route path="/resources/:resourceId" element={<ResourcePage />} />
          </Routes>
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
  openTask.mockClear();
});

describe("ResourcePage", () => {
  it("shows the header, facts, actions with reasons, drift and change history", async () => {
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk");
    expect(await screen.findByRole("heading", { name: "demo-orders" })).toBeInTheDocument();
    expect(screen.getByText("aws_db_instance.demo_orders")).toBeInTheDocument();
    expect(screen.getByText("Sandbox", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText("instance", { selector: "span.uppercase" })).toBeInTheDocument();
    expect(screen.getByText("adopted")).toBeInTheDocument();
    expect(screen.getByText("db.m6g.large", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("instance_class")).toBeInTheDocument();
    expect(screen.getByText(/db_instance_class/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reboot instance: demo-orders" })).toHaveAttribute(
      "href",
      "/operations/0b0f6a4e-0000-4000-8000-000000000001",
    );
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    const adopt = await screen.findByRole("menuitem", { name: /Adopt resources/ });
    expect(adopt).toHaveAttribute("aria-disabled", "true");
    expect(adopt).toHaveTextContent("already adopted");
    await userEvent.click(screen.getByRole("menuitem", { name: /Attach parameter group/ }));
    expect(await screen.findByRole("dialog", { name: "Attach parameter group" })).toBeInTheDocument();
    expect(openTask).toHaveBeenCalledWith({
      kind: "parameter_group_attach",
      environmentSlug: "sandbox",
      resourceId: "YXJuOmk",
      resourceIdentifier: "demo-orders",
      resourceKind: "instance",
      targetKey: "t",
      initialOptions: {}, // attaching a group takes nothing from the resource beyond its id
    });
  });

  it("shows an instance's facts, parameter group, relationships, snapshots and pending maintenance", async () => {
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk");
    await screen.findByRole("heading", { name: "demo-orders" });
    const facts = within(screen.getByRole("region", { name: "Facts" }));
    expect(facts.getByText("postgres 16.4")).toBeInTheDocument();
    expect(facts.getByText("100 GB (autoscaling to 200 GB) gp3")).toBeInTheDocument();
    expect(facts.getByText("7 days, window 03:00-04:00")).toBeInTheDocument();
    expect(facts.getByText("sun:05:00-sun:06:00")).toBeInTheDocument();
    expect(facts.getByText("Performance Insights on, enhanced monitoring every 60 s")).toBeInTheDocument();

    const group = within(screen.getByRole("region", { name: "Parameter group" }));
    expect(await group.findByRole("link", { name: "demo-postgres16-tuned" })).toHaveAttribute(
      "href",
      "/resources/YXJuOmdyb3Vw",
    );
    expect(group.getByText("pending-reboot")).toBeInTheDocument();

    const relationships = within(screen.getByRole("region", { name: "Relationships" }));
    expect(await relationships.findByRole("link", { name: "demo-orders-replica" })).toHaveAttribute(
      "href",
      "/resources/YXJuOnJlcGxpY2E",
    );

    const snapshots = within(screen.getByRole("region", { name: "Snapshots" }));
    expect(await snapshots.findByRole("link", { name: "orders-nightly" })).toHaveAttribute(
      "href",
      "/resources/YXJuOnNuYXA",
    );
    expect(snapshots.queryByText("billing-nightly")).not.toBeInTheDocument();

    const maintenance = within(screen.getByRole("region", { name: "Pending maintenance" }));
    const systemUpdate = within(maintenance.getByText("system-update").closest("li") as HTMLElement);
    expect(systemUpdate.getByText("New OS patch")).toBeInTheDocument();
    expect(systemUpdate.getByText("applies 2026-10-20 02:00 UTC")).toBeInTheDocument();
    expect(systemUpdate.getByText("auto-applied after 2026-11-01 03:00 UTC")).toBeInTheDocument();
    expect(systemUpdate.getByText("opt-in: next-maintenance")).toBeInTheDocument();
    // An action AWS gives no dates or opt-in for shows only what it is.
    const dbUpgrade = within(maintenance.getByText("db-upgrade").closest("li") as HTMLElement);
    expect(dbUpgrade.getByText("Minor version upgrade")).toBeInTheDocument();
    expect(dbUpgrade.queryByText(/applies|auto-applied|opt-in/)).not.toBeInTheDocument();
  });

  it("lays its cards out in columns that may shrink, so a long value never widens the page on a phone", async () => {
    // At 390px an endpoint address made the single implicit grid column 810px wide and the page scrolled
    // sideways; `grid-cols-1` sizes the column minmax(0, 1fr), so long values wrap inside their card.
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    const { container } = renderPage("YXJuOmk");
    await screen.findByRole("heading", { name: "demo-orders" });
    const grids = [...container.querySelectorAll(".md\\:grid-cols-2")];
    expect(grids.length).toBeGreaterThan(0);
    for (const grid of grids) expect(grid).toHaveClass("grid-cols-1");
  });

  it("names the cluster and the source of an instance that belongs to one", async () => {
    const clusterRecord = record("cluster", "demo-aurora", "arn:cluster");
    const member = record("instance", "demo-aurora-1", "arn:member", {
      facts: { engine: "aurora-postgresql", engine_version: "16.4" },
      relationships: {
        parameter_group_name: "default.aurora-postgresql16",
        cluster_identifier: "demo-aurora",
        read_replica_source_identifier: "demo-orders",
        replica_identifiers: [],
      },
    });
    serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [clusterRecord, instanceRecord, member] },
      "GET /api/v1/resources/YXJuOm1lbWJlcg": {
        ...instancePage,
        record: member,
        drift: [],
        change_events: [],
        pending_maintenance: [],
      },
    });
    renderPage("YXJuOm1lbWJlcg");
    const relationships = within(await screen.findByRole("region", { name: "Relationships" }));
    expect(await relationships.findByRole("link", { name: "demo-aurora" })).toHaveAttribute(
      "href",
      "/resources/YXJuOmNsdXN0ZXI",
    );
    expect(relationships.getByRole("link", { name: "demo-orders" })).toHaveAttribute(
      "href",
      "/resources/YXJuOmk",
    );
    expect(
      within(screen.getByRole("region", { name: "Snapshots" })).getByText("No snapshots."),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Pending maintenance" })).getByText("None pending."),
    ).toBeInTheDocument();
  });

  it("lists a cluster's members with role and tier, its capacity, snapshots and pending maintenance", async () => {
    const clusterRecord = record("cluster", "demo-aurora", "arn:cluster", {
      ownership: "adopted",
      facts: {
        engine: "aurora-postgresql",
        engine_version: "16.4",
        engine_mode: "provisioned",
        backup_retention_period: 14,
        preferred_backup_window: "02:00-03:00",
        preferred_maintenance_window: "mon:04:00-mon:05:00",
        storage_encrypted: true,
        deletion_protection: true,
        serverless_v2_min_capacity: 0.5,
        serverless_v2_max_capacity: 8,
      },
      relationships: {
        cluster_parameter_group_name: "demo-aurora-pg16",
        member_identifiers: ["demo-aurora-1", "demo-aurora-2", "demo-aurora-gone"],
        writer_identifier: "demo-aurora-1",
      },
      raw: {
        DBClusterMembers: [
          { DBInstanceIdentifier: "demo-aurora-1", IsClusterWriter: true, PromotionTier: 0 },
          { DBInstanceIdentifier: "demo-aurora-2", IsClusterWriter: false, PromotionTier: 1 },
          { DBInstanceIdentifier: "demo-aurora-gone", IsClusterWriter: false, PromotionTier: 2 },
        ],
      },
    });
    const writer = record("instance", "demo-aurora-1", "arn:writer", {
      status: "available",
      facts: { db_instance_class: "db.r6g.large" },
    });
    const reader = record("instance", "demo-aurora-2", "arn:reader", {
      status: "rebooting",
      facts: { db_instance_class: "db.r6g.xlarge" },
    });
    const clusterGroup = record("parameter_group", "demo-aurora-pg16", "arn:group", {
      facts: { group_scope: "cluster", parameter_group_family: "aurora-postgresql16" },
    });
    const clusterSnapshot = record("snapshot", "aurora-nightly", "arn:snap", {
      facts: { snapshot_scope: "cluster", snapshot_type: "automated", allocated_storage_gb: 50 },
      relationships: { source_identifier: "demo-aurora" },
    });
    const instanceSnapshotWithTheSameName = record("snapshot", "wrong-scope", "arn:wrong", {
      facts: { snapshot_scope: "instance" },
      relationships: { source_identifier: "demo-aurora" },
    });
    serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": {
        resources: [
          clusterRecord,
          writer,
          reader,
          clusterGroup,
          clusterSnapshot,
          instanceSnapshotWithTheSameName,
        ],
      },
      "GET /api/v1/resources/YXJuOmNsdXN0ZXI": {
        ...instancePage,
        record: clusterRecord,
        drift: [],
        change_events: [],
        pending_maintenance: [ordersMaintenance[0]],
      },
    });
    renderPage("YXJuOmNsdXN0ZXI");
    expect(await screen.findByRole("heading", { name: "demo-aurora" })).toBeInTheDocument();

    const facts = within(screen.getByRole("region", { name: "Facts" }));
    expect(facts.getByText("aurora-postgresql 16.4")).toBeInTheDocument();
    expect(facts.getByText("14 days, window 02:00-03:00")).toBeInTheDocument();

    expect(
      await within(screen.getByRole("region", { name: "Cluster parameter group" })).findByRole("link", {
        name: "demo-aurora-pg16",
      }),
    ).toHaveAttribute("href", "/resources/YXJuOmdyb3Vw");

    const capacity = within(screen.getByRole("region", { name: "Capacity" }));
    expect(capacity.getByText("0.5 to 8 ACU")).toBeInTheDocument();

    const members = within(screen.getByRole("region", { name: "Members" }));
    const writerRow = within(await members.findByRole("row", { name: /demo-aurora-1/ }));
    expect(writerRow.getByRole("link", { name: "demo-aurora-1" })).toHaveAttribute(
      "href",
      "/resources/YXJuOndyaXRlcg",
    );
    expect(writerRow.getByText("writer")).toBeInTheDocument();
    expect(writerRow.getByText("0")).toBeInTheDocument();
    expect(writerRow.getByText("db.r6g.large")).toBeInTheDocument();
    const readerRow = within(members.getByRole("row", { name: /demo-aurora-2/ }));
    expect(readerRow.getByText("reader")).toBeInTheDocument();
    expect(readerRow.getByText("1")).toBeInTheDocument();
    expect(readerRow.getByText("rebooting")).toBeInTheDocument();
    // A member the inventory does not hold is named but not linked.
    const goneRow = within(members.getByRole("row", { name: /demo-aurora-gone/ }));
    expect(goneRow.queryByRole("link")).not.toBeInTheDocument();
    expect(goneRow.getByText("reader")).toBeInTheDocument();

    const snapshots = within(screen.getByRole("region", { name: "Snapshots" }));
    expect(await snapshots.findByRole("link", { name: "aurora-nightly" })).toBeInTheDocument();
    expect(snapshots.queryByText("wrong-scope")).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Pending maintenance" })).getByText("system-update"),
    ).toBeInTheDocument();
  });

  it("shows no serverless capacity for a cluster that has none", async () => {
    const provisioned = record("cluster", "demo-aurora", "arn:cluster", {
      facts: { engine: "aurora-postgresql", engine_version: "16.4" },
      relationships: { member_identifiers: [], writer_identifier: null },
    });
    serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [provisioned] },
      "GET /api/v1/resources/YXJuOmNsdXN0ZXI": {
        ...instancePage,
        record: provisioned,
        pending_maintenance: [],
      },
    });
    renderPage("YXJuOmNsdXN0ZXI");
    const capacity = within(await screen.findByRole("region", { name: "Capacity" }));
    expect(capacity.getByText("Serverless v2 scaling is not configured.")).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Members" })).getByText("No members."),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Pending maintenance" })).getByText("None pending."),
    ).toBeInTheDocument();
  });

  it("shows a parameter group's family, users, parameters with the changed filter, compare links and provenance", async () => {
    const group = record("parameter_group", "demo-postgres16-tuned", "arn:group", {
      ownership: "adopted",
      terraform_address: "aws_db_parameter_group.tuned",
      facts: {
        group_scope: "instance",
        parameter_group_family: "postgres16",
        description: "Tuned for orders",
        is_default: false,
        used_by: ["demo-orders"],
        pending_reboot_instances: ["demo-orders"],
        parameters: {
          work_mem: { value: "65536", apply_method: "immediate" },
          retired_setting: { value: "on", apply_method: "pending-reboot" },
        },
        provenance: {
          source_group_names: ["demo-postgres15-tuned"],
          target_family: "postgres16",
          classification: [
            { name: "work_mem", value: "65536", outcome: "carried", note: "" },
            { name: "checkpoint_segments", value: "3", outcome: "removed", note: "not in postgres16" },
          ],
          operation_id: "0b0f6a4e-0000-4000-8000-000000000002",
        },
      },
    });
    const requested = serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [group, instanceRecord] },
      "GET /api/v1/parameters/families/postgres16": {
        family: "postgres16",
        fetched_at_utc: "2026-10-10T10:00:00Z",
        source: "bundled",
        parameters: {
          work_mem: {
            name: "work_mem",
            default_value: "4096",
            description: "Memory for sorts",
            data_type: "integer",
            allowed_values: "64-2147483647",
            is_modifiable: true,
            apply_type: "dynamic",
            apply_method_default: "immediate",
          },
          shared_buffers: {
            name: "shared_buffers",
            default_value: "{DBInstanceClassMemory/32768}",
            description: "Shared memory",
            data_type: "integer",
            allowed_values: null,
            is_modifiable: true,
            apply_type: "static",
            apply_method_default: "pending-reboot",
          },
        },
      },
      "GET /api/v1/resources/YXJuOmdyb3Vw": {
        ...instancePage,
        record: group,
        drift: [],
        change_events: [],
        actions: [],
      },
    });
    renderPage("YXJuOmdyb3Vw");
    expect(await screen.findByRole("heading", { name: "demo-postgres16-tuned" })).toBeInTheDocument();
    expect(requested).toContain(
      "GET /api/v1/parameters/families/postgres16?environment_slug=sandbox&scope=instance",
    );

    const overview = within(screen.getByRole("region", { name: "Parameter group" }));
    expect(overview.getByText("postgres16")).toBeInTheDocument();
    expect(overview.getByText("instance", { selector: "dd" })).toBeInTheDocument();
    expect(overview.getByText("Tuned for orders")).toBeInTheDocument();
    const users = await overview.findAllByRole("link", { name: "demo-orders" });
    expect(users[0]).toHaveAttribute("href", "/resources/YXJuOmk");
    expect(overview.getByRole("link", { name: "Compare with defaults" })).toHaveAttribute(
      "href",
      "/estate/parameter-groups?compare=YXJuOmdyb3Vw&defaults=1",
    );
    expect(overview.getByRole("link", { name: "Compare with another group" })).toHaveAttribute(
      "href",
      "/estate/parameter-groups?compare=YXJuOmdyb3Vw",
    );

    const provenance = within(screen.getByRole("region", { name: /Built by the console for postgres16/ }));
    expect(provenance.getByText(/demo-postgres15-tuned/)).toBeInTheDocument();
    expect(provenance.getByText(/checkpoint_segments: removed \(not in postgres16\)/)).toBeInTheDocument();
    expect(provenance.queryByText(/work_mem: carried/)).not.toBeInTheDocument();

    const parameters = within(screen.getByRole("region", { name: "Parameters" }));
    expect(await parameters.findByText("shared_buffers")).toBeInTheDocument();
    expect(parameters.getByText("3 of 3")).toBeInTheDocument();
    await userEvent.click(parameters.getByRole("checkbox", { name: "Changed from default" }));
    expect(parameters.getByText("2 of 3")).toBeInTheDocument();
    expect(parameters.queryByText("shared_buffers")).not.toBeInTheDocument();
    expect(parameters.getByText("retired_setting")).toBeInTheDocument();
    expect(parameters.getByText("65536")).toBeInTheDocument();
  });

  it("says which carried values the operator changed on the review of the upgrade build", async () => {
    const group = record("parameter_group", "demo-aurora-pg16-tuned-pg17", "arn:group", {
      facts: {
        group_scope: "cluster",
        parameter_group_family: "aurora-postgresql17",
        description: "Built for 17",
        is_default: false,
        used_by: [],
        parameters: { log_min_duration_statement: { value: "2000", apply_method: "immediate" } },
        provenance: {
          source_group_names: ["demo-aurora-pg16-tuned"],
          target_family: "aurora-postgresql17",
          classification: [
            {
              name: "log_min_duration_statement",
              value: "1000",
              final_value: "2000",
              outcome: "carried",
              note: "",
            },
            { name: "rds.force_ssl", value: "1", final_value: "1", outcome: "carried", note: "" },
          ],
          operation_id: "0b0f6a4e-0000-4000-8000-000000000003",
        },
      },
    });
    serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [group] },
      "GET /api/v1/resources/YXJuOmdyb3Vw": {
        record: group,
        actions: [],
        recent_operations: [],
        change_events: [],
      },
    });
    renderPage("YXJuOmdyb3Vw");
    const provenance = within(
      await screen.findByRole("region", { name: /Built by the console for aurora-postgresql17/ }),
    );
    expect(
      provenance.getByText(/log_min_duration_statement: set to 2000 on review \(carried value 1000\)/),
    ).toBeInTheDocument();
    expect(provenance.queryByText(/Every override carried unchanged/)).not.toBeInTheDocument();
    expect(provenance.queryByText(/rds\.force_ssl/)).not.toBeInTheDocument();
  });

  it("reads an Aurora cluster parameter group's defaults from the family's cluster-scope metadata", async () => {
    const group = record("parameter_group", "demo-aurora-pg16", "arn:group", {
      facts: {
        group_scope: "cluster",
        parameter_group_family: "aurora-postgresql16",
        used_by: [],
        parameters: {},
      },
    });
    const metadataParameter = (name: string, description: string) => ({
      name,
      default_value: "0",
      description,
      data_type: "boolean",
      allowed_values: "0,1",
      is_modifiable: true,
      apply_type: "static",
      apply_method_default: "pending-reboot",
    });
    serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [group] },
      "GET /api/v1/resources/YXJuOmdyb3Vw": { ...instancePage, record: group, drift: [], change_events: [] },
      "GET /api/v1/parameters/families/aurora-postgresql16": ({ url }: { url: URL }) => ({
        family: "aurora-postgresql16",
        fetched_at_utc: "2026-10-10T10:00:00Z",
        source: "bundled",
        parameters:
          url.searchParams.get("scope") === "cluster"
            ? { "rds.logical_replication": metadataParameter("rds.logical_replication", "Logical decoding") }
            : { shared_buffers: metadataParameter("shared_buffers", "Instance memory") },
      }),
    });
    renderPage("YXJuOmdyb3Vw");
    const parameters = within(await screen.findByRole("region", { name: "Parameters" }));
    expect(await parameters.findByText("rds.logical_replication")).toBeInTheDocument();
    expect(parameters.queryByText("shared_buffers")).not.toBeInTheDocument();
  });

  it("shows the values set on a parameter group even when the family metadata cannot be read", async () => {
    const group = record("parameter_group", "demo-postgres16-tuned", "arn:group", {
      facts: {
        group_scope: "instance",
        parameter_group_family: "postgres16",
        used_by: [],
        parameters: { work_mem: { value: "65536", apply_method: "immediate" } },
      },
    });
    serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [group] },
      "GET /api/v1/resources/YXJuOmdyb3Vw": { ...instancePage, record: group, drift: [], change_events: [] },
    });
    renderPage("YXJuOmdyb3Vw");
    const parameters = within(await screen.findByRole("region", { name: "Parameters" }));
    expect(await parameters.findByText(/Defaults and descriptions could not be loaded/)).toBeInTheDocument();
    expect(parameters.getByText("work_mem")).toBeInTheDocument();
    expect(parameters.getByText("65536")).toBeInTheDocument();
  });

  it("shows a snapshot's source, engine version, size, creation time and tags", async () => {
    serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [instanceRecord, snapshotOfOrders] },
      "GET /api/v1/resources/YXJuOnNuYXA": {
        ...instancePage,
        record: snapshotOfOrders,
        drift: [],
        change_events: [],
        actions: [],
      },
    });
    renderPage("YXJuOnNuYXA");
    expect(await screen.findByRole("heading", { name: "orders-nightly" })).toBeInTheDocument();
    const facts = within(screen.getByRole("region", { name: "Snapshot" }));
    expect(await facts.findByRole("link", { name: "demo-orders" })).toHaveAttribute(
      "href",
      "/resources/YXJuOmk",
    );
    expect(facts.getByText("automated")).toBeInTheDocument();
    expect(facts.getByText("16.4")).toBeInTheDocument();
    expect(facts.getByText("100 GB")).toBeInTheDocument();
    expect(facts.getByText("2026-10-09T03:00:00+00:00")).toBeInTheDocument();
    const tags = within(screen.getByRole("region", { name: "Tags" }));
    expect(tags.getByText("team")).toBeInTheDocument();
    expect(tags.getByText("orders")).toBeInTheDocument();
    expect(tags.getByText("gold")).toBeInTheDocument();
  });

  it("starts a snapshot's restore from the page body on the snapshot", async () => {
    serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [instanceRecord, snapshotOfOrders] },
      "GET /api/v1/resources/YXJuOnNuYXA": {
        ...instancePage,
        record: snapshotOfOrders,
        drift: [],
        change_events: [],
        actions: [
          {
            kind: "snapshot_restore",
            title: "Restore to new instance or cluster",
            group: "Snapshots",
            summary: "",
            destructive: false,
            disruptive: false,
            applicable: true,
            reason: "",
          },
          {
            kind: "snapshot_delete",
            title: "Delete snapshot",
            group: "Snapshots",
            summary: "",
            destructive: true,
            disruptive: false,
            applicable: false,
            reason: "automated snapshots are deleted by AWS when they expire",
          },
        ],
      },
    });
    renderPage("YXJuOnNuYXA");
    const use = within(await screen.findByRole("region", { name: "Use this snapshot" }));
    expect(use.getByRole("button", { name: "Delete snapshot" })).toBeDisabled();
    await userEvent.click(use.getByRole("button", { name: "Restore to a new instance" }));
    await waitFor(() =>
      expect(openTask).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "snapshot_restore",
          resourceId: "YXJuOnNuYXA",
          resourceIdentifier: "orders-nightly",
          resourceKind: "snapshot",
        }),
      ),
    );
  });

  it("offers a member's own actions, such as Remove reader, from its row on the cluster page", async () => {
    const clusterRecord = record("cluster", "demo-aurora", "arn:cluster", {
      relationships: {
        member_identifiers: ["demo-aurora-1", "demo-aurora-2"],
        writer_identifier: "demo-aurora-1",
      },
    });
    const writer = record("instance", "demo-aurora-1", "arn:writer", {
      relationships: { cluster_identifier: "demo-aurora" },
    });
    const reader = record("instance", "demo-aurora-2", "arn:reader", {
      relationships: { cluster_identifier: "demo-aurora" },
    });
    const removeReader = {
      kind: "cluster_reader_remove",
      title: "Remove reader",
      group: "Create and remove",
      summary: "",
      destructive: true,
      disruptive: false,
      applicable: true,
      reason: "",
    };
    const requested = serve({
      ...commonRoutes,
      "GET /api/v1/environments/sandbox/resources": { resources: [clusterRecord, writer, reader] },
      "GET /api/v1/resources/YXJuOmNsdXN0ZXI": {
        ...instancePage,
        record: clusterRecord,
        drift: [],
        change_events: [],
        pending_maintenance: [],
      },
      "GET /api/v1/resources/YXJuOnJlYWRlcg": {
        ...instancePage,
        record: reader,
        actions: [removeReader],
        drift: [],
        change_events: [],
      },
    });
    renderPage("YXJuOmNsdXN0ZXI");
    const members = within(await screen.findByRole("region", { name: "Members" }));
    const readerRow = within(await members.findByRole("row", { name: /demo-aurora-2/ }));
    expect(requested).not.toContain("GET /api/v1/resources/YXJuOnJlYWRlcg");
    await userEvent.click(readerRow.getByRole("button", { name: "Actions for demo-aurora-2" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Remove reader/ }));
    await waitFor(() =>
      expect(openTask).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "cluster_reader_remove",
          resourceId: "YXJuOnJlYWRlcg",
          resourceIdentifier: "demo-aurora-2",
          resourceKind: "instance",
        }),
      ),
    );
  });

  it("opens the drift resolution with the resource and the choice when the operator accepts or restores", async () => {
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk");
    const drift = within(await screen.findByRole("region", { name: "Drift" }));
    await userEvent.click(drift.getByRole("button", { name: "Accept into the record" }));
    expect(openTask).toHaveBeenLastCalledWith({
      kind: "drift_resolve",
      environmentSlug: "sandbox",
      resourceId: "YXJuOmk",
      resourceIdentifier: "demo-orders",
      resourceKind: "instance",
      targetKey: "t",
      initialOptions: { resource_arn: "arn:i", choice: "accept" },
    });
    expect(await screen.findByRole("dialog", { name: "Resolve drift" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await userEvent.click(drift.getByRole("button", { name: "Restore from the record" }));
    expect(openTask).toHaveBeenLastCalledWith(
      expect.objectContaining({
        kind: "drift_resolve",
        initialOptions: { resource_arn: "arn:i", choice: "restore" },
      }),
    );
    expect(await screen.findByRole("dialog", { name: "Resolve drift" })).toBeInTheDocument();
  });

  it("resolves drift from the Actions menu with the resource's ARN, so accepting it submits", async () => {
    const driftResolve = {
      ...catalogue.definitions[1],
      pauses_for_approval: true,
      pauses_for_approval_when: { accept: false, restore: true },
      options_schema: {
        type: "object",
        required: ["resource_arn", "choice"],
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
      },
    };
    const posted: { kind?: string; options?: Record<string, unknown> }[] = [];
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/catalogue": { definitions: [driftResolve] },
      "GET /api/v1/resources/YXJuOmk": {
        ...instancePage,
        actions: [
          {
            kind: "drift_resolve",
            title: "Resolve drift",
            group: "Drift",
            summary: "",
            destructive: false,
            disruptive: false,
            applicable: true,
            reason: "",
          },
        ],
      },
      "POST /api/v1/operations": ({ init }: { init?: RequestInit }) => {
        const body = JSON.parse(String(init?.body)) as (typeof posted)[number];
        posted.push(body);
        // As the backend does: the hidden resource_arn is required like any other option.
        if (typeof body.options?.resource_arn !== "string") {
          return Response.json(
            { detail: "the options are not valid", problems: ["resource_arn: Field required"] },
            { status: 422 },
          );
        }
        return Response.json({ operation_id: "op-drift", state: "QUEUED" }, { status: 202 });
      },
      "GET /api/v1/operations/op-drift": {
        id: "op-drift",
        operation_kind: "drift_resolve",
        environment_slug: "sandbox",
        target_key: "t",
        created_by_operator_name: "phil",
        approved_by_operator_name: null,
        failure_summary: null,
        created_at_utc: "2026-10-11T10:00:00Z",
        updated_at_utc: "2026-10-11T10:00:01Z",
        finished_at_utc: "2026-10-11T10:00:01Z",
        title: "Resolve drift: demo-orders",
        resource_arn: "arn:i",
        resource_id: "YXJuOmk",
        approval_required_text: null,
        plan: {},
        state: "SUCCEEDED",
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
      },
    });
    renderPage("YXJuOmk");
    await screen.findByRole("heading", { name: "demo-orders" });
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Resolve drift/ }));
    const panel = within(await screen.findByRole("dialog", { name: "Resolve drift" }));
    await userEvent.click(panel.getByRole("radio", { name: "Accept into the record" }));
    await userEvent.click(panel.getByRole("button", { name: "Accept into the record" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      kind: "drift_resolve",
      options: { resource_arn: "arn:i", choice: "accept" },
    });
    expect(await panel.findByRole("link", { name: "Open operation" })).toHaveAttribute(
      "href",
      "/operations/op-drift",
    );
    expect(panel.queryByText("Fix the options marked below.")).not.toBeInTheDocument();
  });

  it("says the resource matches the record when no drift is open", async () => {
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": {
        ...instancePage,
        drift: [],
        change_events: [],
        recent_operations: [],
      },
    });
    renderPage("YXJuOmk");
    const drift = within(await screen.findByRole("region", { name: "Drift" }));
    expect(drift.getByText("Matches the record.")).toBeInTheDocument();
    expect(drift.queryByRole("button")).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Change history" })).getByText("No changes recorded yet."),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Recent operations" })).getByText("None yet."),
    ).toBeInTheDocument();
    await within(screen.getByRole("region", { name: "Snapshots" })).findByRole("link", {
      name: "orders-nightly",
    });
  });

  it("marks a change reviewed and reads the page again", async () => {
    let reviewed = false;
    const requested = serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": () => ({
        ...instancePage,
        change_events: [
          reviewed
            ? { ...outsideChange, reviewed_by_operator_name: "phil", reviewed_at_utc: "2026-10-10T11:00:00Z" }
            : outsideChange,
        ],
      }),
      "POST /api/v1/history/changes/1/review": () => {
        reviewed = true;
        return {};
      },
    });
    renderPage("YXJuOmk");
    const history = within(await screen.findByRole("region", { name: "Change history" }));
    await userEvent.click(history.getByRole("button", { name: /Mark reviewed/ }));
    expect(await history.findByText("reviewed by phil")).toBeInTheDocument();
    expect(history.queryByRole("button", { name: /Mark reviewed/ })).not.toBeInTheDocument();
    expect(requested.filter((r) => r === "GET /api/v1/resources/YXJuOmk")).toHaveLength(2);
    expect(requested).toContain("POST /api/v1/history/changes/1/review");
  });

  it("reports why a change could not be marked reviewed and keeps the button", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "http://console.test").pathname;
        if (path === "/api/v1/bootstrap") return Response.json(bootstrap);
        if (path === "/api/v1/resources/YXJuOmk") return Response.json(instancePage);
        if (init?.method === "POST")
          return Response.json({ detail: "the change is already reviewed" }, { status: 409 });
        return new Response(null, { status: 500 });
      }),
    );
    renderPage("YXJuOmk");
    const history = within(await screen.findByRole("region", { name: "Change history" }));
    await userEvent.click(history.getByRole("button", { name: /Mark reviewed/ }));
    expect(await history.findByRole("alert")).toHaveTextContent("the change is already reviewed");
    expect(history.getByRole("button", { name: /Mark reviewed/ })).toBeInTheDocument();
  });

  it("reads the page again when the task panel closes", async () => {
    const requested = serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk");
    await screen.findByRole("heading", { name: "demo-orders" });
    const pageReads = () => requested.filter((r) => r === "GET /api/v1/resources/YXJuOmk").length;
    expect(pageReads()).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Attach parameter group/ }));
    await screen.findByRole("dialog", { name: "Attach parameter group" });
    expect(pageReads()).toBe(1);
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(pageReads()).toBe(2));
  });

  it("starts an adoption from the Actions menu with the resource and its target selected", async () => {
    const unadoptedPage = {
      ...instancePage,
      record: { ...instanceRecord, ownership: "unmanaged", terraform_address: null },
      actions: instanceActions.map((action) =>
        action.kind === "terraform_adoption" ? { ...action, applicable: true, reason: "" } : action,
      ),
    };
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": unadoptedPage,
    });
    renderPage("YXJuOmk");
    await screen.findByRole("heading", { name: "demo-orders" });
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Adopt resources/ }));
    expect(openTask).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "terraform_adoption",
        resourceId: "YXJuOmk",
        targetKey: "t",
        initialOptions: { target_key: "t", resource_arns: ["arn:i"] },
      }),
    );
  });

  it("does not open a task for a disabled action", async () => {
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk");
    await screen.findByRole("heading", { name: "demo-orders" });
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Adopt resources/ }));
    expect(openTask).not.toHaveBeenCalled();
  });

  it("shows why the resource could not be read", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), "http://console.test").pathname;
        if (path === "/api/v1/bootstrap") return Response.json(bootstrap);
        return Response.json({ detail: "resource arn:i is not in the latest inventory" }, { status: 404 });
      }),
    );
    renderPage("YXJuOmk");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "resource arn:i is not in the latest inventory",
    );
    expect(screen.getByRole("link", { name: "Back to the overview" })).toHaveAttribute("href", "/");
  });

  it("shows the page with plain names when the related lists cannot be read", async () => {
    serve({
      ...commonRoutes,
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk");
    expect(await screen.findByRole("heading", { name: "demo-orders" })).toBeInTheDocument();
    const relationships = within(screen.getByRole("region", { name: "Relationships" }));
    expect(await relationships.findByText("demo-orders-replica")).toBeInTheDocument();
    expect(relationships.queryByRole("link")).not.toBeInTheDocument();
    expect(
      await within(screen.getByRole("region", { name: "Snapshots" })).findByRole("alert"),
    ).toHaveTextContent("Could not load snapshots");
    // Maintenance comes with the page, so it does not depend on the lists.
    expect(
      within(screen.getByRole("region", { name: "Pending maintenance" })).getByText("system-update"),
    ).toBeInTheDocument();
  });

  it("scrolls to the change history when the address asks for it", async () => {
    const scrolled = vi.spyOn(Element.prototype, "scrollIntoView");
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk", "#history");
    await screen.findByRole("heading", { name: "demo-orders" });
    await waitFor(() => expect(scrolled).toHaveBeenCalled());
    expect(scrolled.mock.contexts[0]).toBe(screen.getByRole("region", { name: "Change history" }));
    scrolled.mockRestore();
    await within(screen.getByRole("region", { name: "Snapshots" })).findByRole("link", {
      name: "orders-nightly",
    });
  });
});

describe("ResourcePage and engine upgrades", () => {
  /** The engine-versions table as the picker reads it: 16.4 has two minor targets and one major. */
  function engineVersionsRoute(engine: string, targets: { engine_version: string; is_major: boolean }[]) {
    return {
      "GET /api/v1/parameters/engine-versions": ({ url }: { url: URL }) => {
        expect(url.searchParams.get("environment_slug")).toBe("sandbox");
        expect(url.searchParams.get("engine")).toBe(engine);
        return {
          versions: [
            {
              engine,
              engine_version: "15.8",
              parameter_group_family: `${engine}15`,
              valid_upgrade_targets: [],
            },
            {
              engine,
              engine_version: "16.4",
              parameter_group_family: `${engine}16`,
              valid_upgrade_targets: targets,
            },
          ],
        };
      },
    };
  }

  const upgradeTargets = [
    { engine_version: "16.6", is_major: false },
    { engine_version: "17.2", is_major: true },
    { engine_version: "16.8", is_major: false },
  ];

  it("says which versions an instance can be upgraded to, newest first, when the table lists targets", async () => {
    serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      ...engineVersionsRoute("postgres", upgradeTargets),
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk");
    const facts = within(await screen.findByRole("region", { name: "Facts" }));
    expect(await facts.findByText(/Upgrade available to/)).toHaveTextContent(
      "Upgrade available to 17.2 (major), 16.8 (minor), 16.6 (minor)",
    );
  });

  it("says which versions a cluster can be upgraded to", async () => {
    const clusterRecord = record("cluster", "demo-aurora", "arn:cluster", {
      facts: { engine: "aurora-postgresql", engine_version: "16.4" },
      relationships: { member_identifiers: [] },
    });
    serve({
      ...commonRoutes,
      ...engineVersionsRoute("aurora-postgresql", [{ engine_version: "17.2", is_major: true }]),
      "GET /api/v1/environments/sandbox/resources": { resources: [clusterRecord] },
      "GET /api/v1/resources/YXJuOmNsdXN0ZXI": {
        ...instancePage,
        record: clusterRecord,
        drift: [],
        change_events: [],
        pending_maintenance: [],
      },
    });
    renderPage("YXJuOmNsdXN0ZXI");
    const facts = within(await screen.findByRole("region", { name: "Facts" }));
    expect(await facts.findByText(/Upgrade available to/)).toHaveTextContent(
      "Upgrade available to 17.2 (major)",
    );
  });

  it("says nothing about upgrades when the table lists no target for the current version", async () => {
    const requested = serve({
      ...commonRoutes,
      ...instanceRelatedRoutes,
      ...engineVersionsRoute("postgres", []),
      "GET /api/v1/resources/YXJuOmk": instancePage,
    });
    renderPage("YXJuOmk");
    await screen.findByRole("region", { name: "Facts" });
    await waitFor(() =>
      expect(requested.some((request) => request.startsWith("GET /api/v1/parameters/engine-versions"))).toBe(
        true,
      ),
    );
    expect(screen.queryByText(/Upgrade available/)).not.toBeInTheDocument();
  });

  it("offers no upgrade on an Aurora member, which runs the version of its cluster", async () => {
    const member = record("instance", "demo-aurora-1", "arn:member", {
      facts: { engine: "aurora-postgresql", engine_version: "16.4" },
      relationships: { cluster_identifier: "demo-aurora", replica_identifiers: [] },
    });
    const requested = serve({
      ...commonRoutes,
      ...engineVersionsRoute("aurora-postgresql", upgradeTargets),
      "GET /api/v1/environments/sandbox/resources": { resources: [member] },
      "GET /api/v1/resources/YXJuOm1lbWJlcg": {
        ...instancePage,
        record: member,
        drift: [],
        change_events: [],
        pending_maintenance: [],
      },
    });
    renderPage("YXJuOm1lbWJlcg");
    await screen.findByRole("region", { name: "Facts" });
    await screen.findByRole("region", { name: "Snapshots" });
    expect(screen.queryByText(/Upgrade available/)).not.toBeInTheDocument();
    expect(requested.some((request) => request.startsWith("GET /api/v1/parameters/engine-versions"))).toBe(
      false,
    );
  });
});
