import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { EnvironmentComparison } from "@/api/types";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import ComparePage from "./ComparePage";

function environment(slug: string, displayName: string, isProduction: boolean) {
  return {
    slug,
    display_name: displayName,
    requires_approval: true,
    is_production: isProduction,
    change_management: "terraform",
    allow_direct_changes_to_foreign_owned: false,
    targets: [],
  };
}
const bootstrap = {
  company_display_name: "T",
  operator_name: "phil",
  engine_version: "0.2.0",
  csrf_token: "t",
  environments: [environment("sandbox", "Sandbox", false), environment("production", "Production", true)],
};

const comparison: EnvironmentComparison = {
  left_slug: "sandbox",
  right_slug: "production",
  pairs: [
    {
      kind: "instance",
      left_identifier: "demo-orders",
      right_identifier: "demo-orders-prod",
      left_resource_id: "a",
      right_resource_id: "b",
      differences: [{ attribute: "db_instance_class", left: "db.m6g.large", right: "db.m6g.xlarge" }],
    },
  ],
  unmatched_left: [{ kind: "cluster", identifier: "demo-aurora", resource_id: "c" }],
  unmatched_right: [],
};

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
});

/** Answers bootstrap and the comparison; every comparison request is kept in `compareUrls`. */
function stubApi(answer: (url: URL) => Response = () => Response.json(comparison)) {
  const compareUrls: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://console.test");
      if (url.pathname === "/api/v1/bootstrap") return Response.json(bootstrap);
      if (url.pathname === "/api/v1/environments/compare") {
        compareUrls.push(url);
        return answer(url);
      }
      throw new Error(`unexpected ${url.pathname}`);
    }),
  );
  return compareUrls;
}

/** Shows the address, so a test can see what Compare put in it. */
function AddressProbe() {
  const { search } = useLocation();
  return <output aria-label="address">{search}</output>;
}

function renderAt(address: string) {
  render(
    <MemoryRouter initialEntries={[address]}>
      <EnvironmentProvider>
        <ComparePage />
        <AddressProbe />
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

describe("ComparePage", () => {
  it("compares two environments and shows differences and unmatched resources", async () => {
    stubApi();
    renderAt("/environments/compare?left=sandbox&right=production&strip=-prod");
    await userEvent.click(await screen.findByRole("button", { name: "Compare" }));
    expect(await screen.findByText("db.m6g.xlarge")).toBeInTheDocument();
    expect(screen.getByText("demo-orders-prod")).toBeInTheDocument();
    expect(screen.getByText("demo-aurora")).toBeInTheDocument();
  });

  it("defaults to the selected environment against the next one, and waits to be asked", async () => {
    const compareUrls = stubApi();
    renderAt("/environments/compare");
    await waitFor(() => expect(screen.getByLabelText("Left environment")).toHaveValue("sandbox"));
    expect(screen.getByLabelText("Right environment")).toHaveValue("production");
    expect(compareUrls).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(await screen.findByText("db.m6g.xlarge")).toBeInTheDocument();
    expect(compareUrls).toHaveLength(1);
    expect(compareUrls[0]?.searchParams.get("left")).toBe("sandbox");
    expect(compareUrls[0]?.searchParams.get("right")).toBe("production");
    expect(compareUrls[0]?.searchParams.has("strip")).toBe(false);
  });

  it("compares by itself when the address names both environments", async () => {
    const compareUrls = stubApi();
    renderAt("/environments/compare?left=sandbox&right=production&strip=-prod");
    expect(await screen.findByText("db.m6g.xlarge")).toBeInTheDocument();
    expect(compareUrls).toHaveLength(1);
    expect(compareUrls[0]?.searchParams.get("strip")).toBe("-prod");
    expect(screen.getByLabelText("Strip suffixes or prefixes")).toHaveValue("-prod");
  });

  it("puts the chosen environments and the strip list in the address and compares again", async () => {
    const compareUrls = stubApi();
    renderAt("/environments/compare?left=sandbox&right=production");
    await screen.findByText("db.m6g.xlarge");
    await userEvent.selectOptions(screen.getByLabelText("Left environment"), "production");
    await userEvent.selectOptions(screen.getByLabelText("Right environment"), "sandbox");
    await userEvent.type(screen.getByLabelText("Strip suffixes or prefixes"), "-prod, -prd");
    await userEvent.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(compareUrls).toHaveLength(2));
    expect(compareUrls[1]?.searchParams.get("left")).toBe("production");
    expect(compareUrls[1]?.searchParams.get("right")).toBe("sandbox");
    expect(compareUrls[1]?.searchParams.get("strip")).toBe("-prod, -prd");
    const address = new URLSearchParams(screen.getByLabelText("address").textContent ?? "");
    expect(address.get("left")).toBe("production");
    expect(address.get("right")).toBe("sandbox");
    expect(address.get("strip")).toBe("-prod, -prd");
  });

  it("reads the comparison again when Compare is pressed with nothing changed", async () => {
    const compareUrls = stubApi();
    renderAt("/environments/compare?left=sandbox&right=production");
    await screen.findByText("db.m6g.xlarge");
    await userEvent.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(compareUrls).toHaveLength(2));
    expect(await screen.findByText("db.m6g.xlarge")).toBeInTheDocument();
  });

  it("names the identifiers in the column labels and links both to their resource pages", async () => {
    stubApi();
    renderAt("/environments/compare?left=sandbox&right=production");
    const left = await screen.findByRole("link", { name: "demo-orders in Sandbox" });
    const right = screen.getByRole("link", { name: "demo-orders-prod in Production" });
    expect(left).toHaveAttribute("href", "/resources/a");
    expect(right).toHaveAttribute("href", "/resources/b");
    expect(screen.getByRole("columnheader", { name: "demo-orders in Sandbox" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "demo-orders-prod in Production" })).toBeInTheDocument();
    expect(screen.getByText("demo-orders-prod")).toHaveClass("font-mono");
    expect(screen.getByText("db_instance_class")).toHaveClass("font-mono");
  });

  it("tells the two columns apart when both sides have the same identifier, and marks production", async () => {
    stubApi(() =>
      Response.json({
        ...comparison,
        pairs: [
          {
            kind: "parameter_group",
            left_identifier: "pg-orders",
            right_identifier: "pg-orders",
            left_resource_id: "d",
            right_resource_id: "e",
            differences: [{ attribute: "work_mem", left: "4096", right: "16384" }],
          },
        ],
      }),
    );
    renderAt("/environments/compare?left=sandbox&right=production");
    const sandboxHeader = await screen.findByRole("columnheader", { name: "pg-orders in Sandbox" });
    const productionHeader = screen.getByRole("columnheader", { name: "pg-orders in Production" });
    expect(within(sandboxHeader).getByRole("link", { name: "pg-orders in Sandbox" })).toHaveAttribute(
      "href",
      "/resources/d",
    );
    expect(within(productionHeader).getByRole("link", { name: "pg-orders in Production" })).toHaveAttribute(
      "href",
      "/resources/e",
    );
    expect(sandboxHeader).toHaveTextContent("Sandbox");
    expect(within(sandboxHeader).queryByText("production")).not.toBeInTheDocument();
    expect(productionHeader).toHaveTextContent("Production");
    expect(within(productionHeader).getByText("production")).toBeInTheDocument();
  });

  it("shows a pair that has no differences as one line naming both environments", async () => {
    stubApi(() =>
      Response.json({
        ...comparison,
        pairs: [
          { ...comparison.pairs[0], differences: [] },
          {
            kind: "parameter_group",
            left_identifier: "pg-orders",
            right_identifier: "pg-orders",
            left_resource_id: "d",
            right_resource_id: "e",
            differences: [],
          },
        ],
      }),
    );
    renderAt("/environments/compare?left=sandbox&right=production");
    const row = (await screen.findByRole("link", { name: "pg-orders in Sandbox" })).closest("li");
    expect(row).not.toBeNull();
    expect(row).toHaveTextContent("no differences");
    expect(row).toHaveTextContent("Sandbox");
    expect(row).toHaveTextContent("Production");
    expect(row).toHaveTextContent("production");
    expect(within(row as HTMLElement).getByRole("link", { name: "pg-orders in Sandbox" })).toHaveAttribute(
      "href",
      "/resources/d",
    );
    expect(within(row as HTMLElement).getByRole("link", { name: "pg-orders in Production" })).toHaveAttribute(
      "href",
      "/resources/e",
    );
    expect(screen.getByRole("link", { name: "demo-orders in Sandbox" })).toHaveAttribute(
      "href",
      "/resources/a",
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("lists what has no counterpart on each side, linked to its page", async () => {
    stubApi(() =>
      Response.json({
        ...comparison,
        unmatched_right: [{ kind: "snapshot", identifier: "orders-final", resource_id: "f" }],
      }),
    );
    renderAt("/environments/compare?left=sandbox&right=production");
    const onlyLeft = await screen.findByRole("region", { name: "Only in Sandbox" });
    expect(within(onlyLeft).getByRole("link", { name: "demo-aurora" })).toHaveAttribute(
      "href",
      "/resources/c",
    );
    const onlyRight = screen.getByRole("region", { name: "Only in Production" });
    expect(within(onlyRight).getByRole("link", { name: "orders-final" })).toHaveAttribute(
      "href",
      "/resources/f",
    );
  });

  it("says when nothing is left over on a side", async () => {
    stubApi();
    renderAt("/environments/compare?left=sandbox&right=production");
    const onlyRight = await screen.findByRole("region", { name: "Only in Production" });
    expect(onlyRight).toHaveTextContent("Nothing");
  });

  it("says why a comparison failed", async () => {
    stubApi(() => Response.json({ detail: "no environment named nowhere" }, { status: 404 }));
    renderAt("/environments/compare?left=nowhere&right=production");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not compare: no environment named nowhere",
    );
  });

  it("does not compare an environment with itself", async () => {
    const compareUrls = stubApi();
    renderAt("/environments/compare?left=sandbox&right=sandbox");
    const button = await screen.findByRole("button", { name: "Compare" });
    expect(button).toBeDisabled();
    expect(screen.getByText("Choose two different environments.")).toBeInTheDocument();
    expect(compareUrls).toHaveLength(0);
  });
});
