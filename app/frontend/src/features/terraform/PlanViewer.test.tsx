import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PlanViewer } from "./PlanViewer";

afterEach(() => vi.unstubAllGlobals());

describe("PlanViewer", () => {
  it("renders counts and attribute diffs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          run_id: 9,
          run_kind: "plan",
          plan_summary: {
            create_count: 0,
            update_count: 1,
            delete_count: 0,
            replace_count: 0,
            import_count: 2,
            no_op_count: 1,
            resource_changes: [
              {
                address: "aws_db_instance.orders",
                resource_type: "aws_db_instance",
                actions: ["update"],
                is_import: true,
                import_id: "orders",
                before: { tags_all: {} },
                after: { tags_all: { "rdscommander:managed": "true" } },
              },
              {
                address: "aws_db_parameter_group.demo",
                resource_type: "aws_db_parameter_group",
                actions: ["no-op"],
                is_import: true,
                import_id: "demo",
                before: {},
                after: {},
              },
            ],
          },
          plan_json: null,
        }),
      ),
    );
    render(<PlanViewer runId={9} />);
    expect(await screen.findByText(/2 to import/)).toBeInTheDocument();
    expect(screen.getByText("aws_db_instance.orders")).toBeInTheDocument();
    expect(screen.getByText("tags_all")).toBeInTheDocument();
  });

  it("gives the attribute, before and after columns fixed shares so short values are never split", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          run_id: 9,
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
                before: { skip_final_snapshot: true },
                after: { skip_final_snapshot: false },
              },
            ],
          },
          plan_json: null,
        }),
      ),
    );
    render(<PlanViewer runId={9} />);
    const attribute = await screen.findByText("skip_final_snapshot");
    const table = attribute.closest("table");
    expect(table).toHaveClass("table-fixed");
    const widths = Array.from(table?.querySelectorAll("col") ?? []).map((column) => column.className);
    expect(widths).toEqual(["w-[38%]", "w-[22%]", "w-[40%]"]);
    expect(screen.getByText("true").closest("td")).toHaveClass("[overflow-wrap:anywhere]");
  });

  it("shows a parameter edit as added, removed and changed parameters, not two arrays of objects", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          run_id: 12,
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
                address: "aws_db_parameter_group.orders_pg16",
                resource_type: "aws_db_parameter_group",
                actions: ["update"],
                is_import: false,
                import_id: null,
                before: {
                  description: "Orders",
                  parameter: [
                    { name: "log_min_duration_statement", value: "500", apply_method: "immediate" },
                    { name: "max_connections", value: "200", apply_method: "pending-reboot" },
                    { name: "work_mem", value: "16384", apply_method: "immediate" },
                  ],
                },
                after: {
                  description: "Orders, tuned",
                  parameter: [
                    { name: "max_connections", value: "200", apply_method: "pending-reboot" },
                    { name: "statement_timeout", value: "30000", apply_method: "immediate" },
                    { name: "work_mem", value: "65536", apply_method: "immediate" },
                  ],
                },
              },
            ],
          },
          plan_json: null,
        }),
      ),
    );
    render(<PlanViewer runId={12} />);
    const changes = await screen.findByRole("table", { name: "parameter changes" });
    const changed = within(changes).getByRole("row", { name: /work_mem/ });
    expect(changed).toHaveTextContent("changed");
    expect(within(changed).getByText("16384")).toBeInTheDocument();
    expect(within(changed).getByText("65536")).toBeInTheDocument();
    const added = within(changes).getByRole("row", { name: /statement_timeout/ });
    expect(added).toHaveTextContent("added");
    expect(within(added).getByText("30000")).toBeInTheDocument();
    expect(added).toHaveTextContent("apply_method: immediate");
    // A parameter left out of the set goes back to its default; how it used to apply no longer matters.
    const removed = within(changes).getByRole("row", { name: /log_min_duration_statement/ });
    expect(removed).toHaveTextContent("reset to default");
    expect(within(removed).getByText("500")).toBeInTheDocument();
    expect(removed).not.toHaveTextContent("apply_method");
    expect(within(changes).queryByRole("row", { name: /max_connections/ })).not.toBeInTheDocument();
    expect(screen.getByText("1 changed, 1 added, 1 reset to default, 1 unchanged")).toBeInTheDocument();
    expect(screen.queryByText(/"name":/)).not.toBeInTheDocument();
    // Attributes that are not a named set keep the attribute, before and after table.
    expect(screen.getByText("description")).toBeInTheDocument();
    expect(screen.getByText('"Orders, tuned"')).toBeInTheDocument();
  });

  it("reports the run once its summary is on screen, and not before", async () => {
    const onLoaded = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          run_id: 9,
          run_kind: "plan",
          plan_summary: {
            create_count: 0,
            update_count: 0,
            delete_count: 0,
            replace_count: 0,
            import_count: 0,
            no_op_count: 1,
            resource_changes: [],
          },
          plan_json: null,
        }),
      ),
    );
    render(<PlanViewer runId={9} onLoaded={onLoaded} />);
    expect(screen.getByText("Loading plan…")).toBeInTheDocument();
    expect(onLoaded).not.toHaveBeenCalled();
    await screen.findByText(/1 unchanged/);
    expect(onLoaded).toHaveBeenCalledWith(9);
  });

  it("shows why the plan could not be loaded instead of loading forever", async () => {
    const onLoaded = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ detail: "terraform run 9 not found" }, { status: 404 })),
    );
    render(<PlanViewer runId={9} onLoaded={onLoaded} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the plan: terraform run 9 not found",
    );
    expect(screen.queryByText("Loading plan…")).not.toBeInTheDocument();
    expect(onLoaded).not.toHaveBeenCalled();
  });
});
