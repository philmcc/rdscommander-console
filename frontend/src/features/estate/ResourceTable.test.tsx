import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ResourceRecord } from "@/api/types";
import { ResourceTable } from "./ResourceTable";

const navigate = vi.fn();
vi.mock("react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router")>()),
  useNavigate: () => navigate,
}));

function record(overrides: Partial<ResourceRecord>): ResourceRecord {
  return {
    kind: "instance",
    arn: "arn:x",
    identifier: "x",
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
    resource_id: "eA",
    ...overrides,
  };
}

const instance = record({
  kind: "instance",
  arn: "arn:i",
  identifier: "demo-orders",
  facts: { engine: "postgres", engine_version: "16.4", db_instance_class: "db.m6g.large", multi_az: true },
  relationships: { parameter_group_name: "demo-postgres16-tuned" },
  parameter_apply_status: "pending-reboot",
  resource_id: "YXJuOmk",
});

function headers(): string[] {
  return screen.getAllByRole("columnheader").map((header) => header.textContent ?? "");
}

beforeEach(() => navigate.mockClear());

describe("ResourceTable", () => {
  it("shows instance columns with apply status and opens the resource page", async () => {
    render(
      <MemoryRouter>
        <ResourceTable kind="instance" resources={[instance]} />
      </MemoryRouter>,
    );
    expect(headers()).toEqual([
      "Identifier",
      "Status",
      "Version",
      "Class",
      "Multi-AZ",
      "Parameter group",
      "Ownership",
    ]);
    expect(screen.getByText("db.m6g.large")).toBeInTheDocument();
    expect(screen.getByText("demo-postgres16-tuned")).toBeInTheDocument();
    expect(screen.getByText("pending-reboot")).toBeInTheDocument();
    expect(screen.getByText("16.4").className).toContain("font-mono");
    await userEvent.click(screen.getByText("demo-orders"));
    expect(navigate).toHaveBeenCalledWith("/resources/YXJuOmk");
  });

  it("says under an instance whether it is a replica or a cluster member, and that Multi-AZ does not apply to members", () => {
    const replica = record({
      kind: "instance",
      arn: "arn:r",
      identifier: "demo-orders-replica",
      facts: { engine_version: "16.4", multi_az: false },
      relationships: { read_replica_source_identifier: "demo-orders", cluster_identifier: null },
      resource_id: "cg",
    });
    const member = record({
      kind: "instance",
      arn: "arn:m",
      identifier: "demo-aurora-writer",
      facts: { engine_version: "16.4", multi_az: false },
      relationships: { cluster_identifier: "demo-aurora", read_replica_source_identifier: null },
      resource_id: "bQ",
    });
    render(
      <MemoryRouter>
        <ResourceTable kind="instance" resources={[instance, replica, member]} />
      </MemoryRouter>,
    );
    const replicaRow = screen.getByText("demo-orders-replica").closest("tr") as HTMLElement;
    expect(within(replicaRow).getByText("replica of demo-orders").className).toContain("font-mono");
    expect(within(replicaRow).getByText("replica of demo-orders").className).toContain("text-muted");
    expect(within(replicaRow).getByText("no")).toBeInTheDocument();
    const memberRow = screen.getByText("demo-aurora-writer").closest("tr") as HTMLElement;
    expect(within(memberRow).getByText("member of demo-aurora").className).toContain("font-mono");
    expect(within(memberRow).getByTitle("Aurora cluster member")).toHaveTextContent("—");
    expect(within(memberRow).queryByText("no")).not.toBeInTheDocument();
    const plainRow = screen.getByText("demo-orders").closest("tr") as HTMLElement;
    expect(within(plainRow).queryByText(/replica of|member of/)).not.toBeInTheDocument();
    expect(within(plainRow).queryByTitle("Aurora cluster member")).not.toBeInTheDocument();
  });

  it("shows no apply status for an instance whose group is in sync", () => {
    render(
      <MemoryRouter>
        <ResourceTable kind="instance" resources={[{ ...instance, parameter_apply_status: "in-sync" }]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("demo-postgres16-tuned")).toBeInTheDocument();
    expect(screen.queryByText("in-sync")).not.toBeInTheDocument();
  });

  it("shows cluster members and the cluster parameter group from the relationships", () => {
    const cluster = record({
      kind: "cluster",
      arn: "arn:c",
      identifier: "demo-aurora",
      facts: { engine: "aurora-postgresql", engine_version: "16.4" },
      relationships: {
        cluster_parameter_group_name: "demo-aurora-pg16-tuned",
        member_identifiers: ["demo-aurora-writer", "demo-aurora-reader"],
      },
    });
    render(
      <MemoryRouter>
        <ResourceTable kind="cluster" resources={[cluster]} />
      </MemoryRouter>,
    );
    expect(headers()).toEqual(["Identifier", "Status", "Version", "Members", "Cluster group", "Ownership"]);
    const row = screen.getByText("demo-aurora").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("demo-aurora-pg16-tuned")).toBeInTheDocument();
  });

  it("shows parameter group scope, family, changed count and users", () => {
    const group = record({
      kind: "parameter_group",
      arn: "arn:pg",
      identifier: "demo-postgres16-tuned",
      facts: {
        group_scope: "instance",
        parameter_group_family: "postgres16",
        user_set_parameter_count: 3,
        used_by: ["demo-orders", "demo-orders-replica"],
      },
    });
    render(
      <MemoryRouter>
        <ResourceTable kind="parameter_group" resources={[group]} />
      </MemoryRouter>,
    );
    expect(headers()).toEqual(["Identifier", "Scope", "Family", "Changed", "Used by", "Ownership"]);
    expect(screen.getByText("postgres16").className).toContain("font-mono");
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("demo-orders, demo-orders-replica")).toBeInTheDocument();
  });

  it("shows snapshot source, type, size and creation time", () => {
    const snapshot = record({
      kind: "snapshot",
      arn: "arn:s",
      identifier: "demo-orders-nightly",
      facts: {
        snapshot_scope: "instance",
        snapshot_type: "automated",
        engine_version: "16.4",
        allocated_storage_gb: 100,
        created_at_source_utc: "2026-10-09T02:00:00+00:00",
      },
      relationships: { source_identifier: "demo-orders" },
    });
    render(
      <MemoryRouter>
        <ResourceTable kind="snapshot" resources={[snapshot]} />
      </MemoryRouter>,
    );
    expect(headers()).toEqual(["Identifier", "Source", "Type", "Status", "Version", "Size (GB)", "Created"]);
    expect(screen.getByText("demo-orders")).toBeInTheDocument();
    expect(screen.getByText("automated")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("2026-10-09T02:00:00+00:00")).toBeInTheDocument();
  });

  it("filters rows by identifier and by fact values", async () => {
    const replica = record({
      ...instance,
      arn: "arn:r",
      identifier: "demo-orders-replica",
      resource_id: "cg",
    });
    const other = record({
      kind: "instance",
      arn: "arn:o",
      identifier: "demo-billing",
      facts: { engine_version: "15.8" },
      resource_id: "bw",
    });
    render(
      <MemoryRouter>
        <ResourceTable kind="instance" resources={[instance, replica, other]} />
      </MemoryRouter>,
    );
    await userEvent.type(screen.getByRole("textbox", { name: "Filter instances" }), "15.8");
    expect(screen.getByText("demo-billing")).toBeInTheDocument();
    expect(screen.queryByText("demo-orders")).not.toBeInTheDocument();
  });
});
