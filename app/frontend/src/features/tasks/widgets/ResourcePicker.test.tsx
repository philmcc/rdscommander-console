import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ObjectSchema, UiHints, WidgetProps } from "../SchemaForm";
import { ResourcePicker } from "./ResourcePicker";

afterEach(() => vi.unstubAllGlobals());

function member(identifier: string, clusterIdentifier: string | null, isWriter = false) {
  return {
    kind: "instance",
    arn: `arn:aws:rds:eu-west-2:0:db:${identifier}`,
    identifier,
    ownership: "unmanaged",
    facts: { is_cluster_writer: isWriter },
    relationships: { cluster_identifier: clusterIdentifier },
  };
}

function group(identifier: string, scope: "instance" | "cluster", family: string) {
  return {
    kind: "parameter_group",
    arn: `arn:aws:rds:eu-west-2:0:${scope === "cluster" ? "cluster-pg" : "pg"}:${identifier}`,
    identifier,
    ownership: "unmanaged",
    facts: { group_scope: scope, parameter_group_family: family },
    relationships: {},
  };
}

const GROUPS = [
  group("pg16-tuned", "instance", "postgres16"),
  group("pg17-new", "instance", "postgres17"),
  group("default.postgres16", "instance", "postgres16"),
  group("aurora16-instance", "instance", "aurora-postgresql16"),
  group("aurora16-cluster", "cluster", "aurora-postgresql16"),
  group("aurora17-cluster", "cluster", "aurora-postgresql17"),
];

/** Serves the instance and group lists and one resource page; returns the requested paths. */
function serve(resourceRecord: object | null = null) {
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      requested.push(path);
      if (path === "/api/v1/environments/sandbox/resources?kind=instance")
        return Response.json({
          resources: [
            member("demo-aurora-writer", "demo-aurora", true),
            member("demo-aurora-reader", "demo-aurora"),
            member("other-reader", "other-aurora"),
            member("demo-orders", null),
          ],
        });
      if (path === "/api/v1/environments/sandbox/resources?kind=parameter_group")
        return Response.json({ resources: GROUPS });
      if (path === "/api/v1/resources/res" && resourceRecord)
        return Response.json({ actions: [], recent_operations: [], record: resourceRecord });
      return new Response("not found", { status: 404 });
    }),
  );
  return requested;
}

function renderPicker(ui: UiHints, overrides: Partial<WidgetProps> = {}) {
  const onChange = vi.fn();
  render(
    <ResourcePicker
      name="choice"
      schema={{ type: "string" }}
      ui={{ widget: "resource", label: "Choice", ...ui }}
      value=""
      onChange={onChange}
      context={{ environmentSlug: "sandbox", resourceId: "res", resourceIdentifier: "demo-aurora" }}
      formValue={{}}
      {...overrides}
    />,
  );
  return onChange;
}

const optionNames = () => screen.getAllByRole("option").map((option) => option.textContent);

describe("ResourcePicker with members_of_resource", () => {
  it("offers only the members of the task's cluster, by identifier", async () => {
    const requested = serve();
    const onChange = renderPicker({ resource_kind: "instance", members_of_resource: true });
    await screen.findByRole("option", { name: /demo-aurora-reader/ });
    expect(optionNames()).toEqual([
      "Let AWS choose",
      "demo-aurora-writer (current writer)",
      "demo-aurora-reader (reader)",
    ]);
    expect(screen.getByRole("option", { name: /demo-aurora-writer/ })).toBeDisabled();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Choice" }), "demo-aurora-reader");
    expect(onChange).toHaveBeenLastCalledWith("demo-aurora-reader");
    expect(requested).toContain("/api/v1/environments/sandbox/resources?kind=instance");
  });
});

describe("ResourcePicker narrowing parameter groups", () => {
  it("offers instance groups of the cluster's family for a reader of a cluster", async () => {
    serve({
      kind: "cluster",
      identifier: "demo-aurora",
      facts: { engine: "aurora-postgresql", engine_version: "16.4" },
      relationships: { cluster_parameter_group_name: "aurora16-cluster" },
    });
    renderPicker({ resource_kind: "parameter_group", instance_group_of_cluster_family: true });
    await screen.findByRole("option", { name: /aurora16-instance/ });
    expect(optionNames()).toEqual(["Choose…", "aurora16-instance (instance, aurora-postgresql16)"]);
  });

  it("offers groups of a cluster snapshot's family and scope for a restore", async () => {
    serve({
      kind: "snapshot",
      identifier: "demo-aurora-snap",
      facts: { snapshot_scope: "cluster", engine_version: "16.4" },
      raw: { Engine: "aurora-postgresql" },
      relationships: {},
    });
    renderPicker({ resource_kind: "parameter_group", group_of_restored_family: true });
    await screen.findByRole("option", { name: /aurora16-cluster/ });
    expect(optionNames()).toEqual(["Choose…", "aurora16-cluster (cluster, aurora-postgresql16)"]);
  });

  it("offers instance groups of an instance's family for a point-in-time restore", async () => {
    serve({
      kind: "instance",
      identifier: "demo-orders",
      facts: { engine: "postgres", engine_version: "16.4" },
      relationships: {},
    });
    renderPicker({ resource_kind: "parameter_group", group_of_restored_family: true });
    await screen.findByRole("option", { name: /pg16-tuned/ });
    expect(optionNames()).toEqual([
      "Choose…",
      "pg16-tuned (instance, postgres16)",
      "default.postgres16 (instance, postgres16)",
    ]);
  });

  it("offers groups of the version chosen in a create form, without AWS's default groups", async () => {
    serve();
    const formSchema: ObjectSchema = {
      type: "object",
      properties: {
        engine_version: { type: "string", ui: { widget: "engine_version", engine: "postgres" } },
      },
    };
    const { rerender } = render(<div />);
    const props = {
      name: "parameter_group_name",
      schema: { type: "string" },
      ui: {
        widget: "resource",
        label: "Parameter group",
        resource_kind: "parameter_group",
        group_scope: "instance" as const,
        group_of_chosen_version_family: true,
      },
      value: "",
      onChange: vi.fn(),
      context: { environmentSlug: "sandbox" },
      formSchema,
    };
    rerender(<ResourcePicker {...props} formValue={{}} />);
    expect(await screen.findByText("Choose an engine version first.")).toBeInTheDocument();
    await waitFor(() => expect(optionNames()).toEqual(["Choose…"]));
    rerender(<ResourcePicker {...props} formValue={{ engine_version: "17.2" }} />);
    await screen.findByRole("option", { name: /pg17-new/ });
    expect(optionNames()).toEqual(["Choose…", "pg17-new (instance, postgres17)"]);
    expect(screen.queryByText("Choose an engine version first.")).toBeNull();
  });

  it("offers groups of the target version chosen in an upgrade form, of the engine of the task's resource", async () => {
    serve({
      kind: "instance",
      identifier: "demo-orders",
      facts: { engine: "postgres", engine_version: "16.6" },
      relationships: { parameter_group_name: "pg16-tuned" },
    });
    const upgradeFormSchema: ObjectSchema = {
      type: "object",
      properties: {
        target_engine_version: { type: "string", ui: { widget: "engine_version", upgrade_kind: "major" } },
      },
    };
    renderPicker(
      { resource_kind: "parameter_group", group_scope: "instance", group_of_chosen_version_family: true },
      { formSchema: upgradeFormSchema, formValue: { target_engine_version: "17.2" } },
    );
    await screen.findByRole("option", { name: /pg17-new/ });
    expect(optionNames()).toEqual(["Choose…", "pg17-new (instance, postgres17)"]);
    expect(screen.queryByText("Choose an engine version first.")).toBeNull();
  });

  it("offers cluster groups of the target version chosen in an Aurora cluster's upgrade form", async () => {
    serve({
      kind: "cluster",
      identifier: "demo-aurora",
      facts: { engine: "aurora-postgresql", engine_version: "16.4" },
      relationships: { cluster_parameter_group_name: "aurora16-cluster" },
    });
    const upgradeFormSchema: ObjectSchema = {
      type: "object",
      properties: {
        target_engine_version: { type: "string", ui: { widget: "engine_version", upgrade_kind: "major" } },
      },
    };
    renderPicker(
      { resource_kind: "parameter_group", group_scope: "cluster", group_of_chosen_version_family: true },
      { formSchema: upgradeFormSchema, formValue: { target_engine_version: "17.2" } },
    );
    await screen.findByRole("option", { name: /aurora17-cluster/ });
    expect(optionNames()).toEqual(["Choose…", "aurora17-cluster (cluster, aurora-postgresql17)"]);
  });
});

describe("ResourcePicker when the choice no longer fits", () => {
  it("clears a group picked for one engine version when another version is chosen, and says so", async () => {
    serve();
    const onChange = vi.fn();
    const formSchema: ObjectSchema = {
      type: "object",
      properties: {
        engine_version: { type: "string", ui: { widget: "engine_version", engine: "postgres" } },
      },
    };
    function Harness() {
      const [form, setForm] = useState<Record<string, unknown>>({ engine_version: "17.2" });
      return (
        <>
          <button type="button" onClick={() => setForm({ ...form, engine_version: "16.4" })}>
            Choose 16.4
          </button>
          <ResourcePicker
            name="parameter_group_name"
            schema={{ type: "string" }}
            ui={{
              widget: "resource",
              label: "Parameter group",
              resource_kind: "parameter_group",
              group_scope: "instance",
              group_of_chosen_version_family: true,
            }}
            value={form.parameter_group_name}
            onChange={(next) => {
              onChange(next);
              setForm((current) => ({ ...current, parameter_group_name: next }));
            }}
            context={{ environmentSlug: "sandbox" }}
            formValue={form}
            formSchema={formSchema}
          />
        </>
      );
    }
    render(<Harness />);
    const select = screen.getByRole("combobox", { name: "Parameter group" });
    await screen.findByRole("option", { name: /pg17-new/ });
    await userEvent.selectOptions(select, "arn:aws:rds:eu-west-2:0:pg:pg17-new");
    expect(screen.queryByText(/no longer fits/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Choose 16.4" }));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(undefined));
    expect(select).toHaveValue("");
    expect(select).toHaveAccessibleDescription(/the earlier choice no longer fits; pick again/i);
    await userEvent.selectOptions(select, "arn:aws:rds:eu-west-2:0:pg:pg16-tuned");
    expect(screen.queryByText(/no longer fits/)).toBeNull();
  });

  it("keeps a prefilled value it has never offered", async () => {
    serve();
    const onChange = renderPicker(
      { resource_kind: "instance", members_of_resource: true },
      { value: "demo-aurora-gone" },
    );
    await screen.findByRole("option", { name: /demo-aurora-reader/ });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox", { name: "Choice" })).toHaveValue("demo-aurora-gone");
  });
});
