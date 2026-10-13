import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { OperationSummary } from "@/api/types";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import OperationsPage from "./OperationsPage";

vi.mock("@/features/tasks/TaskContext", () => ({
  useTask: () => ({ current: null, open: vi.fn(), close: vi.fn() }),
}));

const bootstrap = {
  operator_name: "phil",
  console_version: "0.2.0",
  csrf_token: "t",
  environments: [
    {
      slug: "sandbox",
      display_name: "Sandbox",
      requires_approval: true,
      is_production: false,
      change_management: "terraform",
      allow_direct_changes_to_foreign_owned: false,
      targets: [],
    },
    {
      slug: "production",
      display_name: "Production",
      requires_approval: true,
      is_production: true,
      change_management: "terraform",
      allow_direct_changes_to_foreign_owned: false,
      targets: [],
    },
  ],
};

function operation(overrides: Partial<OperationSummary>): OperationSummary {
  return {
    id: "op-1",
    operation_kind: "inventory_refresh",
    title: "Refresh inventory: sandbox",
    state: "SUCCEEDED",
    environment_slug: "sandbox",
    target_key: null,
    resource_arn: null,
    resource_id: null,
    approval_required_text: null,
    created_by_operator_name: "phil",
    approved_by_operator_name: null,
    failure_summary: null,
    created_at_utc: "2026-10-10T10:00:00Z",
    updated_at_utc: "2026-10-10T10:00:00Z",
    finished_at_utc: null,
    progress: null,
    ...overrides,
  };
}

const operations = [
  operation({ id: "op-1", title: "Refresh inventory: sandbox" }),
  operation({
    id: "op-2",
    operation_kind: "parameter_group_edit",
    title: "Edit parameters: demo-pg",
    state: "RUNNING",
    created_by_operator_name: "sam",
    progress: { completed_steps: 1, total_steps: 4, current_step: "apply_parameters" },
  }),
  operation({
    id: "op-3",
    operation_kind: "instance_reboot",
    title: "Reboot instance: demo-orders",
    state: "FAILED",
  }),
  operation({
    id: "op-prod",
    operation_kind: "instance_reboot",
    title: "Reboot instance: prod-orders",
    environment_slug: "production",
  }),
  operation({
    id: "op-import",
    operation_kind: "environment_import",
    title: "Import environment: staging",
    state: "FAILED",
    environment_slug: null,
  }),
];

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  window.localStorage.clear();
});

function serve() {
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.startsWith("/api/v1/operations?")) {
        const query = new URL(url, "http://console").searchParams;
        const states = query.get("state");
        return Response.json({
          operations: operations.filter(
            (entry) =>
              (!query.has("environment_slug") || entry.environment_slug === query.get("environment_slug")) &&
              (!states || states.split(",").includes(entry.state)),
          ),
        });
      }
      const detail = operations.find((entry) => url.endsWith(`/api/v1/operations/${entry.id}`));
      if (detail)
        return Response.json({
          ...detail,
          plan: {},
          step_attempts: [],
          diagnostic_reports: [],
          execution_records: [],
          terraform_runs: [],
          change_set: [],
          requires_reboot: false,
          downtime: "none",
        });
      throw new Error(`unexpected ${url}`);
    }),
  );
  return requested;
}

function Where() {
  return <output aria-label="Location">{useLocation().pathname}</output>;
}

function renderPage(entry = "/operations") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <EnvironmentProvider>
        <Routes>
          <Route path="operations" element={<OperationsPage />} />
          <Route path="operations/:operationId" element={<OperationsPage />} />
        </Routes>
        <Where />
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

describe("OperationsPage", () => {
  it("lists the selected environment's operations with state, progress, kind and author", async () => {
    const requested = serve();
    renderPage();
    const table = within(await screen.findByRole("table"));
    expect(await table.findByRole("link", { name: "Refresh inventory: sandbox" })).toBeInTheDocument();
    expect(requested).toContain("/api/v1/operations?environment_slug=sandbox&limit=100");
    const running = table.getByRole("row", { name: /Edit parameters: demo-pg/ });
    expect(within(running).getByText("RUNNING")).toBeInTheDocument();
    expect(within(running).getByText(/step 2 of 4/)).toBeInTheDocument();
    expect(within(running).getByText("parameter_group_edit")).toHaveClass("font-mono");
    expect(within(running).getByText("sam")).toBeInTheDocument();
    for (const header of ["Operation", "State", "Kind", "Created by", "Created"])
      expect(table.getByRole("button", { name: `Sort by ${header}` })).toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("opens an operation's detail in the right column when its row is opened", async () => {
    serve();
    renderPage();
    // Anywhere on the row opens it, not only the title link.
    await userEvent.click(await screen.findByText("parameter_group_edit"));
    expect(screen.getByLabelText("Location")).toHaveTextContent("/operations/op-2");
    expect(await screen.findByRole("heading", { name: "Edit parameters: demo-pg" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Edit parameters: demo-pg" })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("shows the operation the address names", async () => {
    serve();
    renderPage("/operations/op-3");
    expect(await screen.findByRole("heading", { name: "Reboot instance: demo-orders" })).toBeInTheDocument();
  });

  it("lays the list and the open operation out in a column that may shrink, so a long step detail never widens a phone-width page", async () => {
    // At 390px a major upgrade's step detail (the green endpoint, kept on one line) made the implicit grid
    // column 1237px wide and the page scrolled sideways; `grid-cols-1` sizes the column minmax(0, 1fr).
    serve();
    const { container } = renderPage("/operations/op-3");
    await screen.findByRole("heading", { name: "Reboot instance: demo-orders" });
    const pageGrid = container.querySelector(".lg\\:grid-cols-\\[minmax\\(0\\,5fr\\)_minmax\\(0\\,6fr\\)\\]");
    expect(pageGrid).not.toBeNull();
    expect(pageGrid).toHaveClass("grid-cols-1");
  });

  it("switches to the operation's environment when it is opened from another one", async () => {
    const requested = serve();
    renderPage("/operations/op-prod");
    expect(await screen.findByRole("heading", { name: "Reboot instance: prod-orders" })).toBeInTheDocument();
    await waitFor(() =>
      expect(requested).toContain("/api/v1/operations?environment_slug=production&limit=100"),
    );
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("Production");
    expect(await screen.findByRole("link", { name: "Reboot instance: prod-orders" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Refresh inventory: sandbox" })).not.toBeInTheDocument();
  });

  it("narrows the list to the operation and its state while one is open, and still filters on every field", async () => {
    serve();
    renderPage("/operations/op-3");
    const table = within(await screen.findByRole("table"));
    expect(table.getByRole("button", { name: "Sort by Operation" })).toBeInTheDocument();
    expect(table.getByRole("button", { name: "Sort by State" })).toBeInTheDocument();
    expect(table.queryByRole("button", { name: "Sort by Kind" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "Filter operations" }), "parameter_group_edit");
    expect(screen.getByRole("link", { name: "Edit parameters: demo-pg" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Refresh inventory: sandbox" })).not.toBeInTheDocument();
  });

  it("filters the list to active or failed operations on the server and back to all", async () => {
    const requested = serve();
    renderPage();
    await screen.findByRole("link", { name: "Refresh inventory: sandbox" });
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(screen.getByRole("button", { name: "Active" }));
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Refresh inventory: sandbox" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("link", { name: "Edit parameters: demo-pg" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Active" })).toHaveAttribute("aria-pressed", "true");
    expect(requested.at(-1)).toContain("state=QUEUED%2CRUNNING%2CAWAITING_APPROVAL");

    await userEvent.click(screen.getByRole("button", { name: "Failed" }));
    expect(await screen.findByRole("link", { name: "Reboot instance: demo-orders" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit parameters: demo-pg" })).not.toBeInTheDocument();
    expect(requested.at(-1)).toContain("state=FAILED");

    await userEvent.click(screen.getByRole("button", { name: "All" }));
    expect(await screen.findByRole("link", { name: "Refresh inventory: sandbox" })).toBeInTheDocument();
  });

  it("lists every environment's operations, and imports that belong to none, under All environments", async () => {
    const requested = serve();
    renderPage();
    await screen.findByRole("link", { name: "Refresh inventory: sandbox" });
    expect(screen.getByRole("button", { name: "Sandbox" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("link", { name: "Import environment: staging" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "All environments" }));
    expect(screen.getByRole("button", { name: "All environments" })).toHaveAttribute("aria-pressed", "true");
    const imported = await screen.findByRole("row", { name: /Import environment: staging/ });
    expect(requested.at(-1)).toBe("/api/v1/operations?limit=100");
    expect(within(imported).getByText("No environment")).toBeInTheDocument();
    expect(within(imported).getByRole("link", { name: "Import environment: staging" })).toHaveAttribute(
      "href",
      "/operations/op-import",
    );
    const production = screen.getByRole("row", { name: /Reboot instance: prod-orders/ });
    expect(within(production).getByText("Production")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("All environments");
  });

  it("narrows the rows with the filter box", async () => {
    serve();
    renderPage();
    await screen.findByRole("link", { name: "Refresh inventory: sandbox" });
    await userEvent.type(screen.getByRole("textbox", { name: "Filter operations" }), "reboot");
    expect(screen.queryByRole("link", { name: "Refresh inventory: sandbox" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Reboot instance: demo-orders" })).toBeInTheDocument();
  });

  it("says in words why a mistyped operation address cannot be read, instead of blanking the page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        if (url.startsWith("/api/v1/operations?")) return Response.json({ operations: [] });
        if (url.endsWith("/api/v1/operations/not-a-uuid")) {
          return Response.json(
            {
              detail: [
                {
                  type: "uuid_parsing",
                  loc: ["path", "operation_id"],
                  msg: "Input should be a valid UUID",
                  input: "not-a-uuid",
                },
              ],
            },
            { status: 422 },
          );
        }
        throw new Error(`unexpected ${url}`);
      }),
    );
    renderPage("/operations/not-a-uuid");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the operation: the request is not valid: operation_id: Input should be a valid UUID",
    );
    expect(screen.getByRole("heading", { name: "Operations" })).toBeInTheDocument();
  });

  it("says why the list could not be loaded", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).endsWith("/api/v1/bootstrap")
          ? Response.json(bootstrap)
          : Response.json({ detail: "database is locked" }, { status: 500 }),
      ),
    );
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the operations: database is locked",
    );
  });
});
