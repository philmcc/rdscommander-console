import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { ResourceRecord } from "@/api/types";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { initialOptionsFor } from "@/features/launcher/launchOptions";
import { SchemaForm } from "./SchemaForm";

const schema = {
  type: "object",
  properties: {
    name: {
      type: "string",
      title: "Name",
      ui: { widget: "text", help: "Lowercase letters.", group: "Group", order: 2 },
    },
    scope: {
      type: "string",
      enum: ["instance", "cluster"],
      ui: {
        widget: "choice",
        help: "Scope.",
        group: "Group",
        order: 1,
        labels: { instance: "Instance", cluster: "Cluster" },
      },
    },
    reboot: {
      type: "boolean",
      default: false,
      ui: { widget: "boolean", help: "Reboot now.", group: "After", order: 3 },
    },
    hidden_arn: { type: "string", ui: { widget: "hidden", help: "", order: 4 } },
  },
  required: ["name", "scope"],
};

describe("SchemaForm", () => {
  it("renders grouped, ordered widgets with help and reports changes", async () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={schema}
        value={{ scope: "instance", hidden_arn: "x" }}
        onChange={onChange}
        problems={{ name: "name: required" }}
        context={{}}
      />,
    );
    const groups = screen.getAllByRole("group").map((g) => g.getAttribute("aria-label"));
    expect(groups).toEqual(["Group", "After"]);
    expect(screen.getByText("Lowercase letters.")).toBeInTheDocument();
    expect(screen.getByText("name: required")).toBeInTheDocument();
    expect(screen.queryByLabelText("hidden_arn")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Name"), "a");
    expect(onChange).toHaveBeenLastCalledWith({ scope: "instance", hidden_arn: "x", name: "a" });
    await userEvent.click(screen.getByRole("radio", { name: "Cluster" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ scope: "cluster" }));
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
});

describe("SchemaForm widgets", () => {
  it("labels each field with its ui label, then its schema title, then its name, and describes it by its help", () => {
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            slug: {
              type: "string",
              title: "Slug",
              ui: { widget: "text", label: "Short name", help: "2 to 40 lowercase letters.", order: 1 },
            },
            display_name: { type: "string", title: "Display Name", ui: { widget: "text", order: 2 } },
            note: { type: "string", ui: { widget: "text", order: 3 } },
            is_production: {
              type: "boolean",
              title: "Is Production",
              ui: { widget: "boolean", label: "Production environment", order: 4 },
            },
            choice: {
              type: "string",
              title: "Choice",
              enum: ["accept", "restore"],
              ui: { widget: "choice", label: "Resolution", order: 5 },
            },
          },
        }}
        value={{}}
        onChange={() => undefined}
        problems={{}}
        context={{}}
      />,
    );
    expect(screen.getByLabelText("Short name")).toHaveAccessibleDescription("2 to 40 lowercase letters.");
    expect(screen.queryByLabelText("Slug")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Display Name")).toBeInTheDocument();
    expect(screen.getByLabelText("note")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Production environment" })).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Resolution" })).toBeInTheDocument();
  });

  it("chooses a widget from the JSON type when the hint names none that is known", async () => {
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            workers: { type: "integer", title: "Workers", ui: { widget: "mystery", order: 1 } },
            enabled: { type: "boolean", title: "Enabled", ui: { order: 2 } },
          },
        }}
        value={{}}
        onChange={onChange}
        problems={{}}
        context={{}}
      />,
    );
    await userEvent.type(screen.getByLabelText("Workers"), "3");
    expect(onChange).toHaveBeenLastCalledWith({ workers: 3 });
    await userEvent.click(screen.getByRole("switch", { name: "Enabled" }));
    expect(onChange).toHaveBeenLastCalledWith({ enabled: true });
  });

  it("lists only the resources a resource picker's hints allow, keeps prefilled ones and adds on toggle", async () => {
    const record = (arn: string, identifier: string, kind: string, ownership: string, facts = {}) => ({
      arn,
      identifier,
      kind,
      ownership,
      facts,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/v1/environments/sandbox/resources") {
          return Response.json({
            resources: [
              record("arn:pg", "pg-one", "parameter_group", "unmanaged", {
                group_scope: "instance",
                parameter_group_family: "postgres16",
              }),
              record("arn:db1", "db-one", "instance", "unmanaged"),
              record("arn:db2", "db-two", "instance", "adopted"),
              record("arn:snap", "snap-one", "snapshot", "unmanaged"),
            ],
          });
        }
        throw new Error(`unexpected ${String(input)}`);
      }),
    );
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            resource_arns: {
              type: "array",
              title: "Resources",
              ui: {
                widget: "resource_picker",
                kinds: ["parameter_group", "instance"],
                ownership: ["unmanaged"],
                order: 1,
              },
            },
          },
        }}
        value={{ resource_arns: ["arn:gone"] }}
        onChange={onChange}
        problems={{}}
        context={{ environmentSlug: "sandbox" }}
      />,
    );
    expect(await screen.findByRole("checkbox", { name: "db-one (instance)" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "pg-one (instance, postgres16)" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "arn:gone" })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: /db-two/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /snap-one/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "db-one (instance)" }));
    expect(onChange).toHaveBeenLastCalledWith({ resource_arns: ["arn:gone", "arn:db1"] });
  });

  it("offers the environment's unmanaged subnet groups when the picker asks for them", async () => {
    const subnetGroup = (name: string, ownership: string) => ({
      id: 1,
      db_subnet_group_name: name,
      db_subnet_group_arn: `arn:subgrp:${name}`,
      vpc_id: "vpc-1",
      subnet_ids: [],
      description: "",
      ownership,
      tags: {},
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/v1/environments/sandbox/resources") {
          return Response.json({
            resources: [
              { arn: "arn:db1", identifier: "db-one", kind: "instance", ownership: "unmanaged", facts: {} },
            ],
          });
        }
        if (String(input) === "/api/v1/environments/sandbox/inventory") {
          return Response.json({
            environment_slug: "sandbox",
            display_name: "Sandbox",
            requires_approval: true,
            targets: [
              {
                target_key: "t",
                aws_account_id: "0",
                aws_region: "eu-west-2",
                aws_profile_name: null,
                simulated: true,
                snapshot: null,
                instances: [],
                clusters: [],
                parameter_groups: [],
                subnet_groups: [subnetGroup("private-subnets", "unmanaged"), subnetGroup("old", "adopted")],
              },
            ],
          });
        }
        throw new Error(`unexpected ${String(input)}`);
      }),
    );
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            resource_arns: {
              type: "array",
              title: "Resources",
              ui: {
                widget: "resource_picker",
                kinds: ["subnet_group", "instance"],
                ownership: ["unmanaged"],
                order: 1,
              },
            },
          },
        }}
        value={{ resource_arns: [] }}
        onChange={onChange}
        problems={{}}
        context={{ environmentSlug: "sandbox" }}
      />,
    );
    expect(await screen.findByRole("checkbox", { name: "private-subnets (subnet group)" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "db-one (instance)" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /old/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "private-subnets (subnet group)" }));
    expect(onChange).toHaveBeenLastCalledWith({ resource_arns: ["arn:subgrp:private-subnets"] });
  });

  it("offers the environment's targets as checkboxes for a list and as a select for one key", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          company_display_name: "Test Co",
          operator_name: "phil",
          engine_version: "0.2.0",
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
        }),
      ),
    );
    const onChange = vi.fn();
    render(
      <EnvironmentProvider>
        <SchemaForm
          schema={{
            type: "object",
            properties: {
              target_keys: { type: "array", title: "Targets", ui: { widget: "target_picker", order: 1 } },
              target_key: { type: "string", title: "Target", ui: { widget: "target", order: 2 } },
            },
          }}
          value={{}}
          onChange={onChange}
          problems={{}}
          context={{ environmentSlug: "sandbox" }}
        />
      </EnvironmentProvider>,
    );
    await userEvent.click(await screen.findByRole("checkbox", { name: "0 / eu-west-2 (simulated)" }));
    expect(onChange).toHaveBeenLastCalledWith({ target_keys: ["sandbox/0/eu-west-2"] });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Target" }), "sandbox/0/eu-west-2");
    expect(onChange).toHaveBeenLastCalledWith({ target_key: "sandbox/0/eu-west-2" });
  });

  describe("parameter group picker limited to the resource's family and scope", () => {
    const group = (identifier: string, scope: string, family: string) => ({
      arn: `arn:aws:rds:eu-west-2:0:${scope === "cluster" ? "cluster-pg" : "pg"}:${identifier}`,
      identifier,
      kind: "parameter_group",
      ownership: "unmanaged",
      facts: { group_scope: scope, parameter_group_family: family },
    });
    const groups = [
      group("pg16-current", "instance", "postgres16"),
      group("pg16-tuned", "instance", "postgres16"),
      group("pg15-old", "instance", "postgres15"),
      group("aurora16-current", "cluster", "aurora-postgresql16"),
      group("aurora16-tuned", "cluster", "aurora-postgresql16"),
      group("aurora15-old", "cluster", "aurora-postgresql15"),
    ];
    const pickerSchema = {
      type: "object",
      properties: {
        parameter_group_arn: {
          type: "string",
          title: "Group",
          ui: {
            widget: "resource",
            resource_kind: "parameter_group",
            same_family_and_scope_as_resource: true,
            order: 1,
          },
        },
      },
    };

    /** Answers the group list at once and the resource page when `releaseResource` is called. */
    function stubResource(record: object) {
      let releaseResource: () => void = () => undefined;
      const resourceReleased = new Promise<void>((resolve) => {
        releaseResource = resolve;
      });
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const path = String(input);
          if (path === "/api/v1/environments/sandbox/resources?kind=parameter_group") {
            return Response.json({ resources: groups });
          }
          if (path === "/api/v1/resources/cmVz") {
            await resourceReleased;
            return Response.json({ actions: [], recent_operations: [], record });
          }
          throw new Error(`unexpected ${path}`);
        }),
      );
      return releaseResource;
    }

    function renderPicker() {
      render(
        <SchemaForm
          schema={pickerSchema}
          value={{}}
          onChange={vi.fn()}
          problems={{}}
          context={{ environmentSlug: "sandbox", resourceId: "cmVz" }}
        />,
      );
    }

    const optionNames = () => screen.getAllByRole("option").map((option) => option.textContent);

    it("lists instance groups of the attached group's family for an instance, and none before it has loaded", async () => {
      const releaseResource = stubResource({
        kind: "instance",
        identifier: "demo-orders",
        relationships: { parameter_group_name: "pg16-current" },
      });
      renderPicker();
      await screen.findByRole("combobox", { name: "Group" });
      expect(optionNames()).toEqual(["Choose…"]);
      releaseResource();
      await screen.findByRole("option", { name: "pg16-tuned (instance, postgres16)" });
      expect(optionNames()).toEqual([
        "Choose…",
        "pg16-current (instance, postgres16)",
        "pg16-tuned (instance, postgres16)",
      ]);
    });

    it("lists cluster groups of the attached cluster group's family for a cluster", async () => {
      const releaseResource = stubResource({
        kind: "cluster",
        identifier: "demo-aurora",
        relationships: { cluster_parameter_group_name: "aurora16-current" },
      });
      renderPicker();
      releaseResource();
      await screen.findByRole("option", { name: "aurora16-tuned (cluster, aurora-postgresql16)" });
      expect(optionNames()).toEqual([
        "Choose…",
        "aurora16-current (cluster, aurora-postgresql16)",
        "aurora16-tuned (cluster, aurora-postgresql16)",
      ]);
    });
  });
});

describe("SchemaForm sibling updates", () => {
  it("merges a widget's patch into the form value, so a widget can keep a sibling field", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/resources/cmVz") {
          return Response.json({
            actions: [],
            recent_operations: [],
            record: {
              facts: { parameter_group_family: "postgres16", parameters: { work_mem: { value: "8192" } } },
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
                description: "",
                is_modifiable: true,
              },
            },
          });
        }
        throw new Error(`unexpected ${path}`);
      }),
    );
    const onChange = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            parameters: { type: "object", ui: { widget: "parameters", order: 1 } },
            reset: { type: "array", default: [], ui: { widget: "hidden", order: 2 } },
          },
        }}
        value={{ parameters: { work_mem: "16384" } }}
        onChange={onChange}
        problems={{}}
        context={{ environmentSlug: "sandbox", resourceId: "cmVz" }}
      />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Reset work_mem to default" }));
    expect(onChange).toHaveBeenLastCalledWith({ parameters: {}, reset: ["work_mem"] });
  });
});

describe("SchemaForm number bounds", () => {
  it("passes the schema's minimum and maximum and the hint's step to the number input", () => {
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            min_capacity: {
              type: "number",
              title: "Minimum capacity",
              minimum: 0.5,
              maximum: 128,
              ui: { widget: "number", step: 0.5 },
            },
          },
        }}
        value={{}}
        onChange={() => {}}
        problems={{}}
        context={{}}
      />,
    );
    const input = screen.getByLabelText("Minimum capacity");
    expect(input).toHaveAttribute("min", "0.5");
    expect(input).toHaveAttribute("max", "128");
    expect(input).toHaveAttribute("step", "0.5");
  });

  it("picks the window, tags and instance class widgets by name", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ classes: [] })),
    );
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            window: { type: "string", title: "Window", ui: { widget: "window", window_kind: "daily" } },
            tags: { type: "object", title: "Tag set", ui: { widget: "tags" } },
            klass: { type: "string", title: "Class", ui: { widget: "instance_class" } },
          },
        }}
        value={{ window: "01:00-02:00" }}
        onChange={() => {}}
        problems={{}}
        context={{}}
      />,
    );
    expect(screen.getByLabelText("Window start time")).toHaveValue("01:00");
    expect(screen.getByRole("button", { name: "Add tag" })).toBeInTheDocument();
    expect(screen.getByLabelText("Class")).toBeInTheDocument();
  });
});

describe("SchemaForm lifecycle widgets", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders the date-time, security-group and subnet-group widgets, with help under each", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.startsWith("/api/v1/lifecycle/restore-window/"))
          return Response.json({
            earliest_utc: null,
            latest_restorable_utc: null,
            unknown_reason: "no backups",
          });
        if (path.endsWith("/inventory")) return Response.json({ environment_slug: "sandbox", targets: [] });
        if (path.endsWith("/resources")) return Response.json({ resources: [] });
        return new Response("not found", { status: 404 });
      }),
    );
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            restore_time_utc: {
              type: "string",
              ui: { widget: "datetime", label: "Restore to", help: "A time in UTC.", order: 1 },
            },
            vpc_security_group_ids: {
              type: "array",
              ui: { widget: "security_groups", label: "Security groups", help: "At least one.", order: 2 },
            },
            subnet_group_name: {
              type: "string",
              ui: { widget: "subnet_group", label: "Subnet group", help: "Where it goes.", order: 3 },
            },
          },
        }}
        value={{}}
        onChange={vi.fn()}
        problems={{ subnet_group_name: "Choose a subnet group." }}
        context={{ environmentSlug: "sandbox", resourceId: "abc", resourceKind: "instance" }}
      />,
    );
    expect(screen.getByLabelText("Restore to (UTC)")).toHaveAccessibleDescription(/A time in UTC\./);
    expect(screen.getByRole("group", { name: "Security groups" })).toBeInTheDocument();
    expect(screen.getByText("At least one.")).toBeInTheDocument();
    const subnetSelect = screen.getByRole("combobox", { name: "Subnet group" });
    expect(subnetSelect).toHaveAttribute("aria-invalid", "true");
    expect(subnetSelect).toHaveAccessibleDescription(/Choose a subnet group\./);
    expect(await screen.findByText(/no backups/)).toBeInTheDocument();
  });
});

describe("SchemaForm started on a resource", () => {
  it("shows the backup settings form with the resource's retention and tag copying", () => {
    const backupSchema = {
      type: "object",
      properties: {
        backup_retention_period_days: {
          type: "integer",
          ui: { widget: "number", label: "Retention (days)", order: 1 },
        },
        copy_tags_to_snapshot: {
          anyOf: [{ type: "boolean" }, { type: "null" }],
          ui: { widget: "boolean", label: "Copy tags to snapshots", order: 3 },
        },
      },
      required: ["backup_retention_period_days"],
    };
    const instance = {
      kind: "instance",
      arn: "arn:aws:rds:eu-west-2:0:db:demo-orders",
      facts: { backup_retention_period: 7 },
      raw: { BackupRetentionPeriod: 7, CopyTagsToSnapshot: true },
    } as unknown as ResourceRecord;
    const backupDefinition = {
      kind: "backup_settings_change",
      options_schema: backupSchema,
    } as unknown as Parameters<typeof initialOptionsFor>[0];
    render(
      <SchemaForm
        schema={backupSchema}
        value={initialOptionsFor(backupDefinition, instance)}
        onChange={() => undefined}
        problems={{}}
        context={{}}
      />,
    );
    expect(screen.getByLabelText("Retention (days)")).toHaveValue(7);
    expect(screen.getByRole("switch", { name: "Copy tags to snapshots" })).toBeChecked();
  });
});
