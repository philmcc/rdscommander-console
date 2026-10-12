import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { FamilyMetadata } from "@/api/types";
import { ParameterEditor } from "./ParameterEditor";

const family: FamilyMetadata = {
  family: "postgres16",
  fetched_at_utc: "2026-10-10T00:00:00Z",
  source: "bundled",
  parameters: {
    work_mem: {
      name: "work_mem",
      default_value: "4096",
      description: "",
      data_type: "integer",
      allowed_values: "64-2147483647",
      is_modifiable: true,
      apply_type: "dynamic",
      apply_method_default: "immediate",
      minimum_engine_version: null,
    },
    shared_buffers: {
      name: "shared_buffers",
      default_value: "{DBInstanceClassMemory/32768}",
      description: "",
      data_type: "integer",
      allowed_values: "16-1073741823",
      is_modifiable: true,
      apply_type: "static",
      apply_method_default: "pending-reboot",
      minimum_engine_version: null,
    },
    log_statement: {
      name: "log_statement",
      default_value: "none",
      description: "",
      data_type: "string",
      allowed_values: "none,ddl,mod,all",
      is_modifiable: true,
      apply_type: "dynamic",
      apply_method_default: "immediate",
      minimum_engine_version: null,
    },
  },
};

const current = { work_mem: { value: "16384", apply_method: "immediate" } };

describe("ParameterEditor", () => {
  it("validates like the backend and sums the change", async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <ParameterEditor family={family} current={current} edits={{}} onChange={onChange} />,
    );
    await userEvent.type(screen.getByLabelText("Value of log_statement"), "x");
    expect(onChange).toHaveBeenLastCalledWith({ log_statement: "x" });
    rerender(
      <ParameterEditor
        family={family}
        current={current}
        edits={{ log_statement: "x", shared_buffers: "{DBInstanceClassMemory/16384}", work_mem: "10" }}
        onChange={onChange}
      />,
    );
    expect(screen.getByText("log_statement must be one of none, ddl, mod, all")).toBeInTheDocument();
    expect(screen.getByText("work_mem must be between 64 and 2147483647")).toBeInTheDocument();
    expect(screen.getByText("3 parameters change, 1 needs a reboot")).toBeInTheDocument();
  });

  it("says nothing changes and nothing needs a reboot before the first edit", () => {
    render(<ParameterEditor family={family} current={current} edits={{}} onChange={vi.fn()} />);
    expect(screen.getByText("0 parameters change, 0 need a reboot")).toBeInTheDocument();
  });

  it("singularises the summary for one dynamic edit", () => {
    render(
      <ParameterEditor family={family} current={current} edits={{ work_mem: "8192" }} onChange={vi.fn()} />,
    );
    expect(screen.getByText("1 parameter changes, 0 need a reboot")).toBeInTheDocument();
  });

  it("shows a message the server gave for a parameter when the value itself looks fine", () => {
    render(
      <ParameterEditor
        family={family}
        current={current}
        edits={{ work_mem: "8192" }}
        problems={{ work_mem: "work_mem cannot be both set and reset" }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("work_mem cannot be both set and reset")).toBeInTheDocument();
  });

  it("offers no reset when the caller does not keep a list of resets", () => {
    render(<ParameterEditor family={family} current={current} edits={{}} onChange={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Reset .* to default/ })).not.toBeInTheDocument();
  });

  describe("reset to default", () => {
    it("is offered only on rows whose current value is set", () => {
      render(<ParameterEditor family={family} current={current} edits={{}} resets={[]} onChange={vi.fn()} />);
      expect(screen.getByRole("button", { name: "Reset work_mem to default" })).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Reset shared_buffers to default" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Reset log_statement to default" }),
      ).not.toBeInTheDocument();
    });

    it("queues the reset and drops a pending edit of the same parameter", async () => {
      const onChange = vi.fn();
      render(
        <ParameterEditor
          family={family}
          current={current}
          edits={{ work_mem: "8192", log_statement: "ddl" }}
          resets={["shared_buffers"]}
          onChange={onChange}
        />,
      );
      await userEvent.click(screen.getByRole("button", { name: "Reset work_mem to default" }));
      expect(onChange).toHaveBeenLastCalledWith({ log_statement: "ddl" }, ["shared_buffers", "work_mem"]);
    });

    it("shows the family default with a marker, counts the reset, and counts a static one as a reboot", () => {
      render(
        <ParameterEditor
          family={family}
          current={{ ...current, shared_buffers: { value: "999", apply_method: "pending-reboot" } }}
          edits={{ log_statement: "ddl" }}
          resets={["work_mem", "shared_buffers"]}
          onChange={vi.fn()}
        />,
      );
      expect(screen.getByLabelText("Value of work_mem")).toHaveValue("4096");
      expect(screen.getByLabelText("Value of shared_buffers")).toHaveValue("{DBInstanceClassMemory/32768}");
      expect(screen.getAllByText("will reset")).toHaveLength(2);
      expect(screen.queryByRole("button", { name: "Reset work_mem to default" })).not.toBeInTheDocument();
      expect(
        screen.getByText("1 parameter changes, 2 reset to default, 1 needs a reboot"),
      ).toBeInTheDocument();
    });

    it("is undone by Undo reset, which keeps the edits", async () => {
      const onChange = vi.fn();
      render(
        <ParameterEditor
          family={family}
          current={current}
          edits={{ log_statement: "ddl" }}
          resets={["work_mem"]}
          onChange={onChange}
        />,
      );
      await userEvent.click(screen.getByRole("button", { name: "Undo reset of work_mem" }));
      expect(onChange).toHaveBeenLastCalledWith({ log_statement: "ddl" }, []);
    });

    it("is cancelled by typing a value for the parameter", async () => {
      const onChange = vi.fn();
      render(
        <ParameterEditor
          family={family}
          current={current}
          edits={{}}
          resets={["work_mem", "shared_buffers"]}
          onChange={onChange}
        />,
      );
      await userEvent.type(screen.getByLabelText("Value of work_mem"), "1");
      expect(onChange).toHaveBeenLastCalledWith({ work_mem: "40961" }, ["shared_buffers"]);
    });

    it("keeps Discard edit for pending edits", async () => {
      const onChange = vi.fn();
      render(
        <ParameterEditor
          family={family}
          current={current}
          edits={{ work_mem: "8192" }}
          resets={[]}
          onChange={onChange}
        />,
      );
      await userEvent.click(screen.getByRole("button", { name: "Discard edit for work_mem" }));
      expect(onChange).toHaveBeenLastCalledWith({});
    });
  });
});
