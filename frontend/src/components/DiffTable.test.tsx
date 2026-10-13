import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiffTable } from "./DiffTable";

describe("DiffTable", () => {
  it("renders attribute, before, after and flags in monospace cells", () => {
    render(
      <DiffTable
        rows={[
          { attribute: "parameters.work_mem", before: "16384", after: "65536", flags: ["reboot"] },
          { attribute: "deletion_protection", before: false, after: true },
        ]}
      />,
    );
    expect(screen.getByRole("cell", { name: "parameters.work_mem" }).className).toContain("font-mono");
    expect(screen.getByText("65536")).toBeInTheDocument();
    expect(screen.getByText("reboot")).toBeInTheDocument();
    expect(screen.getByText("false")).toBeInTheDocument();
  });

  it("lets values and flags wrap so the flags column stays inside a narrow panel", () => {
    render(
      <DiffTable
        rows={[
          {
            attribute: "parameter_group_name",
            before: "demo-postgres16-tuned",
            after: "orders-pg16-logging",
            flags: ["reboot", "reboot now"],
          },
        ]}
      />,
    );
    for (const name of ["parameter_group_name", "demo-postgres16-tuned", "orders-pg16-logging"]) {
      expect(screen.getByRole("cell", { name })).toHaveClass("whitespace-normal");
    }
    // Group names wrap at their hyphens; a name with nowhere to wrap may break anywhere.
    expect(screen.getByRole("cell", { name: "demo-postgres16-tuned" })).toHaveClass(
      "[overflow-wrap:break-word]",
    );
    expect(screen.getByRole("cell", { name: "parameter_group_name" })).toHaveClass(
      "[overflow-wrap:anywhere]",
    );
    expect(screen.getByText("reboot now").closest("td")).toHaveClass("whitespace-normal");
  });

  it("keeps a five-digit value whole in a narrow panel, and lets only a word too long for a column break anywhere", () => {
    render(
      <div style={{ width: "240px" }}>
        <DiffTable
          rows={[
            { attribute: "parameters.work_mem", before: "16384", after: "65536", flags: ["reboot"] },
            { attribute: "db_instance_class", before: "db.r6g.large", after: "db.r6g.2xlarge" },
            {
              attribute: "parameters.shared_preload_libraries",
              before: null,
              after: "pg_stat_statements,auto_explain",
            },
          ]}
          beforeLabel="Current"
          afterLabel="New"
        />
      </div>,
    );
    // `overflow-wrap: anywhere` lets the table squeeze a column below its longest word, which split 65536 as
    // 6553 / 6; `break-word` keeps the column at least as wide as the word.
    for (const name of ["16384", "65536", "db.r6g.large", "db.r6g.2xlarge"]) {
      const cell = screen.getByRole("cell", { name });
      expect(cell).toHaveClass("whitespace-normal", "[overflow-wrap:break-word]");
      expect(cell).not.toHaveClass("[overflow-wrap:anywhere]");
    }
    for (const name of ["parameters.shared_preload_libraries", "pg_stat_statements,auto_explain"]) {
      expect(screen.getByRole("cell", { name })).toHaveClass("whitespace-normal", "[overflow-wrap:anywhere]");
    }
  });
});
