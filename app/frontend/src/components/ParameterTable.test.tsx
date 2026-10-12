import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ParameterTable } from "./ParameterTable";

const rows = [
  {
    name: "work_mem",
    value: "16384",
    default: "4096",
    data_type: "integer",
    apply_type: "dynamic",
    is_modifiable: true,
    description: "memory",
    changed: true,
  },
  {
    name: "log_statement",
    value: null,
    default: "none",
    data_type: "string",
    apply_type: "dynamic",
    is_modifiable: true,
    description: "",
    changed: false,
  },
];

describe("ParameterTable", () => {
  it("filters by search and by changed-from-default, and edits values", async () => {
    const onChange = vi.fn();
    render(
      <ParameterTable
        rows={rows}
        editable
        onChange={onChange}
        problems={{ work_mem: "work_mem must be an integer" }}
      />,
    );
    expect(screen.getAllByRole("row")).toHaveLength(3);
    await userEvent.click(screen.getByRole("checkbox", { name: "Changed from default" }));
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByText("work_mem")).toBeInTheDocument();
    expect(screen.queryByText("log_statement")).not.toBeInTheDocument();
    expect(screen.queryByText("No parameters match.")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "Changed from default" }));
    await userEvent.type(screen.getByPlaceholderText("Search parameters"), "log_");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByText("log_statement")).toBeInTheDocument();
    expect(screen.queryByText("work_mem")).not.toBeInTheDocument();
    expect(screen.queryByText("No parameters match.")).not.toBeInTheDocument();
    await userEvent.clear(screen.getByPlaceholderText("Search parameters"));
    await userEvent.type(screen.getByLabelText("Value of work_mem"), "0");
    expect(onChange).toHaveBeenLastCalledWith("work_mem", "163840");
    expect(screen.getByText("work_mem must be an integer")).toBeInTheDocument();
  });

  it("offers Discard edit only for rows with a pending edit, even when other rows differ from their default", async () => {
    const onReset = vi.fn();
    render(
      <ParameterTable
        rows={rows.map((row) =>
          row.name === "work_mem" ? { ...row, edited: true } : { ...row, value: "all", changed: true },
        )}
        editable
        onChange={vi.fn()}
        onReset={onReset}
      />,
    );
    expect(screen.getAllByRole("button", { name: /Discard edit/ })).toHaveLength(1);
    expect(screen.queryByText(/reset to default/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard edit for work_mem" }));
    expect(onReset).toHaveBeenCalledWith("work_mem");
  });

  it("offers Reset to default only on resettable rows and Undo reset only on rows that will reset", async () => {
    const onResetToDefault = vi.fn();
    const onUndoReset = vi.fn();
    render(
      <ParameterTable
        rows={rows.map((row) =>
          row.name === "work_mem" ? { ...row, resettable: true } : { ...row, willReset: true },
        )}
        editable
        onChange={vi.fn()}
        onResetToDefault={onResetToDefault}
        onUndoReset={onUndoReset}
      />,
    );
    expect(screen.getAllByRole("button", { name: /to default/ })).toHaveLength(1);
    expect(screen.getAllByText("will reset")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Reset work_mem to default" }));
    expect(onResetToDefault).toHaveBeenCalledWith("work_mem");
    await userEvent.click(screen.getByRole("button", { name: "Undo reset of log_statement" }));
    expect(onUndoReset).toHaveBeenCalledWith("log_statement");
  });

  it("offers neither reset action without a handler", () => {
    render(
      <ParameterTable rows={rows.map((row) => ({ ...row, resettable: true }))} editable onChange={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: /to default/ })).not.toBeInTheDocument();
  });
});
