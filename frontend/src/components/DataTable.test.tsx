import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DataTable } from "./DataTable";

const rows = [
  { id: "b", name: "beta", size: 2 },
  { id: "a", name: "alpha", size: 10 },
];

describe("DataTable", () => {
  it("turns each row into a stacked card at phone width, every value labelled with its column", () => {
    render(
      <DataTable
        rows={rows}
        rowKey={(r) => r.id}
        columns={[
          { key: "name", header: "Name", sortValue: (r) => r.name, render: (r) => r.name },
          { key: "size", header: "Size", sortValue: (r) => r.size, render: (r) => String(r.size) },
        ]}
      />,
    );
    const table = screen.getByRole("table");
    expect(table).toHaveClass("max-sm:block");
    const [headerRow, firstRow] = screen.getAllByRole("row");
    // The header row keeps the sort buttons, wrapped into a line of chips above the cards.
    expect(headerRow).toHaveClass("max-sm:flex", "max-sm:flex-wrap");
    expect(firstRow).toHaveClass("max-sm:block");
    const cell = screen.getByRole("cell", { name: "beta" });
    expect(cell).toHaveAttribute("data-label", "Name");
    expect(cell).toHaveClass("max-sm:flex", "max-sm:before:content-[attr(data-label)]");
  });

  it("sorts, filters and opens rows with the keyboard", async () => {
    const onOpen = vi.fn();
    render(
      <DataTable
        rows={rows}
        rowKey={(r) => r.id}
        onOpen={onOpen}
        filterPlaceholder="Filter"
        columns={[
          { key: "name", header: "Name", sortValue: (r) => r.name, render: (r) => r.name },
          { key: "size", header: "Size", sortValue: (r) => r.size, render: (r) => String(r.size) },
        ]}
      />,
    );
    const cells = () =>
      screen
        .getAllByRole("row")
        .slice(1)
        .map((r) => r.textContent);
    expect(cells()[0]).toContain("beta");
    await userEvent.click(screen.getByRole("button", { name: "Sort by Name" }));
    expect(cells()[0]).toContain("alpha");
    await userEvent.type(screen.getByPlaceholderText("Filter"), "bet");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByText("beta")).toBeInTheDocument();
    expect(screen.queryByText("alpha")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing matches.")).not.toBeInTheDocument();
    await userEvent.clear(screen.getByPlaceholderText("Filter"));
    const first = screen.getAllByRole("row")[1] as HTMLElement;
    first.focus();
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(onOpen).toHaveBeenCalledWith(rows[0]);
  });
  it("shows the empty message when the filter hides every row", async () => {
    render(
      <DataTable
        rows={rows}
        rowKey={(r) => r.id}
        columns={[{ key: "name", header: "Name", sortValue: (r) => r.name, render: (r) => r.name }]}
      />,
    );
    await userEvent.type(screen.getByPlaceholderText("Filter"), "zzz");
    expect(screen.getByText("Nothing matches.")).toBeInTheDocument();
    expect(screen.queryByText("alpha")).not.toBeInTheDocument();
    expect(screen.queryByText("beta")).not.toBeInTheDocument();
  });

  it("does not open the row for a click on a control inside a cell, but does for a plain cell", async () => {
    const onOpen = vi.fn();
    const onDelete = vi.fn();
    render(
      <DataTable
        rows={rows}
        rowKey={(r) => r.id}
        onOpen={onOpen}
        columns={[
          { key: "name", header: "Name", render: (r) => r.name },
          {
            key: "actions",
            header: "Actions",
            render: (r) => (
              <button type="button" onClick={() => onDelete(r.id)}>
                Delete {r.name}
              </button>
            ),
          },
        ]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Delete beta" }));
    expect(onDelete).toHaveBeenCalledWith("b");
    expect(onOpen).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("beta"));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith(rows[0]);
  });

  it("does not open the row when Enter is pressed on a control inside it", async () => {
    const onOpen = vi.fn();
    render(
      <DataTable
        rows={rows}
        rowKey={(r) => r.id}
        onOpen={onOpen}
        columns={[
          { key: "name", header: "Name", render: (r) => r.name },
          {
            key: "actions",
            header: "Actions",
            render: (r) => <button type="button">Delete {r.name}</button>,
          },
        ]}
      />,
    );
    screen.getByRole("button", { name: "Delete beta" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(onOpen).not.toHaveBeenCalled();
  });
});
