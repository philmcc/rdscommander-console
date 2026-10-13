import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ParameterComparison } from "@/api/types";
import { ParameterCompare } from "./ParameterCompare";

afterEach(() => vi.unstubAllGlobals());

function row(overrides: Partial<ParameterComparison["rows"][number]>): ParameterComparison["rows"][number] {
  return {
    name: "work_mem",
    left: "16384",
    right: "8192",
    default: "4096",
    differs: true,
    apply_type: "dynamic",
    data_type: "integer",
    description: "",
    ...overrides,
  };
}

const comparison: ParameterComparison = {
  left_name: "demo-postgres16-tuned",
  right_name: "demo-postgres16-other",
  family_left: "postgres16",
  family_right: "postgres16",
  rows: [
    row({ name: "work_mem" }),
    row({
      name: "shared_buffers",
      left: "999",
      right: "999",
      default: "128",
      differs: false,
      apply_type: "static",
    }),
    row({ name: "log_statement", left: "ddl", right: null, default: "none" }),
  ],
};

/** Records every request and answers the comparison with `answer`. */
function serve(answer: () => Response = () => Response.json(comparison)) {
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      requested.push(String(input));
      return answer();
    }),
  );
  return requested;
}

describe("ParameterCompare", () => {
  it("compares two groups and shows only the parameters that differ until asked for all", async () => {
    const requested = serve();
    render(<ParameterCompare leftResourceId="bGVmdA" rightResourceId="cmlnaHQ" />);
    expect(await screen.findByText("work_mem")).toBeInTheDocument();
    expect(requested).toEqual(["/api/v1/parameters/compare?left=bGVmdA&right=cmlnaHQ"]);
    expect(screen.getByRole("columnheader", { name: /demo-postgres16-tuned/ })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /demo-postgres16-other/ })).toBeInTheDocument();
    expect(screen.getByText("log_statement")).toBeInTheDocument();
    expect(screen.queryByText("shared_buffers")).not.toBeInTheDocument();
    expect(screen.getByText("2 of 3 parameters")).toBeInTheDocument();
    const differencesOnly = screen.getByRole("switch", { name: "Differences only" });
    expect(differencesOnly).toBeChecked();
    await userEvent.click(differencesOnly);
    expect(screen.getByText("shared_buffers")).toBeInTheDocument();
    expect(screen.getByText("3 of 3 parameters")).toBeInTheDocument();
  });

  it("shows values in monospace, and the default in muted type where a group leaves the parameter at it", async () => {
    serve();
    render(<ParameterCompare leftResourceId="bGVmdA" rightResourceId="cmlnaHQ" />);
    const logStatement = (await screen.findByText("log_statement")).closest("tr") as HTMLElement;
    const cells = within(logStatement).getAllByRole("cell");
    // name, left, right, default, apply
    expect(cells).toHaveLength(5);
    expect(within(cells[1] as HTMLElement).getByText("ddl").className).toContain("font-mono");
    const atDefault = within(cells[2] as HTMLElement).getByText("none");
    expect(atDefault.className).toContain("font-mono");
    expect(atDefault.className).toContain("text-muted");
    expect(within(cells[3] as HTMLElement).getByText("none")).toBeInTheDocument();
    expect(within(cells[4] as HTMLElement).getByText("dynamic")).toBeInTheDocument();
  });

  it("marks static parameters as needing a reboot", async () => {
    serve();
    render(<ParameterCompare leftResourceId="bGVmdA" rightResourceId="cmlnaHQ" />);
    await screen.findByText("work_mem");
    await userEvent.click(screen.getByRole("switch", { name: "Differences only" }));
    expect(screen.getByText("static (reboot)")).toBeInTheDocument();
  });

  it("compares with the family defaults when asked to", async () => {
    const requested = serve(() =>
      Response.json({ ...comparison, right_name: "postgres16 defaults", rows: [row({ right: "4096" })] }),
    );
    render(<ParameterCompare leftResourceId="bGVmdA" againstDefaults />);
    expect(await screen.findByRole("columnheader", { name: /postgres16 defaults/ })).toBeInTheDocument();
    expect(requested).toEqual(["/api/v1/parameters/compare?left=bGVmdA&against_defaults=true"]);
  });

  it("says when nothing differs", async () => {
    serve(() => Response.json({ ...comparison, rows: [comparison.rows[1]] }));
    render(<ParameterCompare leftResourceId="bGVmdA" rightResourceId="cmlnaHQ" />);
    expect(await screen.findByText("No differences between the two groups.")).toBeInTheDocument();
  });

  it("names the families when the two groups are of different families", async () => {
    serve(() => Response.json({ ...comparison, family_right: "postgres17" }));
    render(<ParameterCompare leftResourceId="bGVmdA" rightResourceId="cmlnaHQ" />);
    expect(await screen.findByRole("columnheader", { name: /postgres17/ })).toBeInTheDocument();
  });

  it("shows why the comparison could not be made", async () => {
    serve(() =>
      Response.json({ detail: "demo-postgres16-other is not in the latest inventory" }, { status: 404 }),
    );
    render(<ParameterCompare leftResourceId="bGVmdA" rightResourceId="cmlnaHQ" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "demo-postgres16-other is not in the latest inventory",
    );
  });
});
