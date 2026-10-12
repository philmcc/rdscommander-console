import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ParameterTableField, parameterProblemsByName } from "./ParameterTableField";

afterEach(() => vi.unstubAllGlobals());

const names = ["work_mem", "work_mem_extra", "max_connections"];

describe("parameterProblemsByName", () => {
  it("gives a message to the parameter it starts with", () => {
    expect(parameterProblemsByName(["max_connections must be an integer"], names)).toEqual({
      max_connections: "max_connections must be an integer",
    });
  });

  it("does not let a name claim a longer name that merely begins with it", () => {
    expect(parameterProblemsByName(["work_mem_extra cannot be modified"], names)).toEqual({
      work_mem_extra: "work_mem_extra cannot be modified",
    });
  });

  it("joins several messages for one parameter and ignores messages that name none", () => {
    expect(
      parameterProblemsByName(
        ["work_mem must be an integer", "work_mem cannot be both set and reset", "something else failed"],
        names,
      ),
    ).toEqual({ work_mem: "work_mem must be an integer; work_mem cannot be both set and reset" });
  });
});

function parameter(name: string, defaultValue: string) {
  return {
    name,
    allowed_values: null,
    apply_method_default: "immediate",
    apply_type: "dynamic",
    data_type: "integer",
    default_value: defaultValue,
    description: "",
    is_modifiable: true,
  };
}

function stubGroupAndFamily() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/resources/cmVz") {
        return Response.json({
          actions: [],
          recent_operations: [],
          record: {
            facts: {
              parameter_group_family: "postgres16",
              // `shared_buffers` differs from its default in the group but has no pending edit.
              parameters: { shared_buffers: { value: "999" }, work_mem: { value: "4096" } },
            },
          },
        });
      }
      if (path.startsWith("/api/v1/parameters/families/postgres16")) {
        return Response.json({
          family: "postgres16",
          fetched_at_utc: "2026-10-10T10:00:00Z",
          source: "bundled",
          parameters: {
            shared_buffers: parameter("shared_buffers", "128"),
            work_mem: parameter("work_mem", "4096"),
            max_connections: parameter("max_connections", "100"),
          },
        });
      }
      throw new Error(`unexpected ${path}`);
    }),
  );
}

describe("ParameterTableField", () => {
  it("offers Discard edit only on rows with a pending edit, and discarding removes just that edit", async () => {
    stubGroupAndFamily();
    const onChange = vi.fn();
    render(
      <ParameterTableField
        name="parameters"
        schema={{ type: "object" }}
        ui={{ widget: "parameters" }}
        value={{ work_mem: "8192", max_connections: "200" }}
        onChange={onChange}
        context={{ environmentSlug: "sandbox", resourceId: "cmVz" }}
        formValue={{}}
      />,
    );
    await screen.findByLabelText("Value of work_mem");
    expect(screen.getAllByRole("button", { name: /Discard edit/ })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Discard edit for shared_buffers" })).not.toBeInTheDocument();
    expect(screen.queryByText(/reset to default/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard edit for work_mem" }));
    expect(onChange).toHaveBeenLastCalledWith({ max_connections: "200" });
  });

  it("validates typed values against the family and sums the change", async () => {
    stubGroupAndFamily();
    render(
      <ParameterTableField
        name="parameters"
        schema={{ type: "object" }}
        ui={{ widget: "parameters" }}
        value={{ max_connections: "200" }}
        onChange={vi.fn()}
        context={{ environmentSlug: "sandbox", resourceId: "cmVz" }}
        formValue={{}}
      />,
    );
    await screen.findByLabelText("Value of work_mem");
    expect(screen.getByText("1 parameter changes, 0 need a reboot")).toBeInTheDocument();
  });

  it("queues a reset in the sibling reset field together with the edits, in one update", async () => {
    stubGroupAndFamily();
    const onChange = vi.fn();
    const onFormChange = vi.fn();
    render(
      <ParameterTableField
        name="parameters"
        schema={{ type: "object" }}
        ui={{ widget: "parameters" }}
        value={{ work_mem: "8192", max_connections: "200" }}
        onChange={onChange}
        onFormChange={onFormChange}
        context={{ environmentSlug: "sandbox", resourceId: "cmVz" }}
        formValue={{ reset: ["shared_buffers"] }}
      />,
    );
    await screen.findByLabelText("Value of work_mem");
    // `shared_buffers` is already queued; the group sets `work_mem` and nothing else.
    expect(screen.getByLabelText("Value of shared_buffers")).toHaveValue("128");
    expect(screen.queryByRole("button", { name: "Reset shared_buffers to default" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Reset max_connections to default" }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reset work_mem to default" }));
    expect(onFormChange).toHaveBeenLastCalledWith({
      parameters: { max_connections: "200" },
      reset: ["shared_buffers", "work_mem"],
    });
    expect(onChange).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Undo reset of shared_buffers" }));
    expect(onFormChange).toHaveBeenLastCalledWith({
      parameters: { work_mem: "8192", max_connections: "200" },
      reset: [],
    });
  });

  it("keeps plain edits on the field's own value", async () => {
    stubGroupAndFamily();
    const onChange = vi.fn();
    const onFormChange = vi.fn();
    render(
      <ParameterTableField
        name="parameters"
        schema={{ type: "object" }}
        ui={{ widget: "parameters" }}
        value={{}}
        onChange={onChange}
        onFormChange={onFormChange}
        context={{ environmentSlug: "sandbox", resourceId: "cmVz" }}
        formValue={{}}
      />,
    );
    await userEvent.type(await screen.findByLabelText("Value of max_connections"), "5");
    expect(onChange).toHaveBeenLastCalledWith({ max_connections: "5" });
    expect(onFormChange).not.toHaveBeenCalled();
  });
});

/** A cluster group of an Aurora family, and that family's metadata answered by the scope asked for. */
function stubClusterGroupAndFamily() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://console.test");
      if (url.pathname === "/api/v1/resources/Y2x1") {
        return Response.json({
          actions: [],
          recent_operations: [],
          record: {
            facts: { parameter_group_family: "aurora-postgresql16", group_scope: "cluster", parameters: {} },
          },
        });
      }
      if (url.pathname === "/api/v1/parameters/families/aurora-postgresql16") {
        return Response.json({
          family: "aurora-postgresql16",
          fetched_at_utc: "2026-10-10T10:00:00Z",
          source: "bundled",
          parameters:
            url.searchParams.get("scope") === "cluster"
              ? { "rds.logical_replication": parameter("rds.logical_replication", "0") }
              : { shared_buffers: parameter("shared_buffers", "128") },
        });
      }
      throw new Error(`unexpected ${url.pathname}`);
    }),
  );
}

describe("ParameterTableField for an Aurora cluster parameter group", () => {
  it("edits an existing cluster group against the family's cluster-scope parameters", async () => {
    stubClusterGroupAndFamily();
    render(
      <ParameterTableField
        name="parameters"
        schema={{ type: "object" }}
        ui={{ widget: "parameters" }}
        value={{}}
        onChange={vi.fn()}
        context={{ environmentSlug: "sandbox", resourceId: "Y2x1" }}
        formValue={{}}
      />,
    );
    expect(await screen.findByLabelText("Value of rds.logical_replication")).toBeInTheDocument();
    expect(screen.queryByLabelText("Value of shared_buffers")).not.toBeInTheDocument();
  });

  it("creates a cluster group against the cluster-scope parameters of the chosen family", async () => {
    stubClusterGroupAndFamily();
    render(
      <ParameterTableField
        name="parameters"
        schema={{ type: "object" }}
        ui={{ widget: "parameters" }}
        value={{}}
        onChange={vi.fn()}
        context={{ environmentSlug: "sandbox" }}
        formValue={{ family: "aurora-postgresql16", scope: "cluster" }}
      />,
    );
    expect(await screen.findByLabelText("Value of rds.logical_replication")).toBeInTheDocument();
    expect(screen.queryByLabelText("Value of shared_buffers")).not.toBeInTheDocument();
  });
});
