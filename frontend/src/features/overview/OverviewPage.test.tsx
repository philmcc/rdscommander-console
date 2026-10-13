import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { EnvironmentPageData, OperationSummary } from "@/api/types";
import { EnvironmentProvider, useEnvironment } from "@/app/EnvironmentContext";
import { TaskProvider } from "@/features/tasks/TaskContext";
import OverviewPage from "./OverviewPage";

const openTask = vi.hoisted(() => vi.fn());
vi.mock("@/features/tasks/TaskContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/tasks/TaskContext")>();
  return { ...actual, useTask: () => ({ ...actual.useTask(), open: openTask }) };
});

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
  operator_name: "phil",
  console_version: "0.2.0",
  csrf_token: "t",
  environments: [environment("sandbox", "Sandbox", false), environment("production", "Production", true)],
};
const overview = {
  slug: "sandbox",
  display_name: "Sandbox",
  is_production: false,
  change_management: "terraform",
  allow_direct_changes_to_foreign_owned: false,
  inventory_refresh_minutes: 15,
  drift_check_hours: 24,
  counts: { instance: 4, cluster: 1, parameter_group: 2, snapshot: 0 },
  ownership_counts: { unmanaged: 7 },
  engine_versions: { "16.4": 5 },
  last_refresh_at_utc: "2026-10-10T10:00:00Z",
  running_operation_count: 1,
  attention_count: 3,
};
const refreshOperation: OperationSummary = {
  id: "op-1",
  operation_kind: "inventory_refresh",
  state: "RUNNING",
  environment_slug: "sandbox",
  target_key: null,
  created_by_operator_name: "phil",
  approved_by_operator_name: null,
  failure_summary: null,
  created_at_utc: "2026-10-10T10:00:00Z",
  updated_at_utc: "2026-10-10T10:00:00Z",
  finished_at_utc: null,
  title: "Refresh inventory: sandbox",
  resource_arn: null,
  resource_id: null,
  approval_required_text: null,
  progress: { completed_steps: 2, total_steps: 12, current_step: "write_import_blocks" },
};
const sandboxPage: EnvironmentPageData = {
  overview,
  targets: [],
  actions: [],
  recent_operations: [refreshOperation],
  change_events: [],
  drift: [],
  first_steps: { imported: true, refreshed: true, adopted_or_direct: false, mode_set: true },
};
const productionPage: EnvironmentPageData = {
  ...sandboxPage,
  overview: {
    ...overview,
    slug: "production",
    display_name: "Production",
    is_production: true,
    counts: { instance: 9, cluster: 2, parameter_group: 5, snapshot: 1 },
    engine_versions: { "15.8": 11 },
    attention_count: 0,
  },
  recent_operations: [],
};

interface Served {
  /** The page and the in-progress operations each environment answers with. */
  pages?: Record<string, EnvironmentPageData>;
  active?: Record<string, OperationSummary[]>;
  /** A page request for an environment waits until its gate resolves. */
  gates?: Record<string, Promise<void>>;
}

/** Answers bootstrap, the environment pages, the in-progress operations and the catalogue; returns the URLs asked for. */
function serve({
  pages = { sandbox: sandboxPage, production: productionPage },
  active = {},
  gates = {},
}: Served = {}) {
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.endsWith("/api/v1/catalogue")) return Response.json({ definitions: [] });
      const pageSlug = url.match(/\/api\/v1\/environments\/([^/?]+)$/)?.[1];
      if (pageSlug) {
        await gates[pageSlug];
        return Response.json(pages[pageSlug]);
      }
      if (url.startsWith("/api/v1/operations?")) {
        const slug = new URL(url, "http://console").searchParams.get("environment_slug") ?? "";
        return Response.json({ operations: active[slug] ?? [] });
      }
      throw new Error(`unexpected ${url}`);
    }),
  );
  return requested;
}

/** A gate that a test opens when the held answer should arrive. */
function gate() {
  let open = () => {};
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { opened, open };
}

function SwitchToProduction() {
  const { select } = useEnvironment();
  return (
    <button type="button" onClick={() => select("production")}>
      Switch to Production
    </button>
  );
}

function renderOverview() {
  render(
    <MemoryRouter>
      <EnvironmentProvider>
        <TaskProvider>
          <SwitchToProduction />
          <OverviewPage />
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

/** Lets answers that are already on their way reach the page. */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
}

beforeEach(() => openTask.mockClear());
afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  window.localStorage.clear();
});

describe("OverviewPage", () => {
  it("shows counts, versions, running operations, attention and the first steps", async () => {
    serve({ active: { sandbox: [refreshOperation] } });
    renderOverview();
    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.getByText("16.4")).toBeInTheDocument();
    expect(screen.getByText("Refresh inventory: sandbox")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /3 items need attention/ })).toBeInTheDocument();
    expect(screen.getByText("Adopt resources or switch the environment to direct mode")).toBeInTheDocument();
  });

  it("links each count to its estate list", async () => {
    serve();
    renderOverview();
    expect(await screen.findByRole("link", { name: /Instances/ })).toHaveAttribute(
      "href",
      "/estate/instances",
    );
    expect(screen.getByRole("link", { name: /Parameter groups/ })).toHaveAttribute(
      "href",
      "/estate/parameter-groups",
    );
  });

  it("hides the first steps once every step is done", async () => {
    serve({
      pages: {
        sandbox: {
          ...sandboxPage,
          first_steps: { imported: true, refreshed: true, adopted_or_direct: true, mode_set: true },
        },
      },
    });
    renderOverview();
    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.queryByText("First steps")).not.toBeInTheDocument();
  });

  it("asks the operations list for what is in progress in the selected environment", async () => {
    const requested = serve({ active: { sandbox: [refreshOperation] } });
    renderOverview();
    expect(await screen.findByText("Refresh inventory: sandbox")).toBeInTheDocument();
    expect(requested).toContain(
      "/api/v1/operations?environment_slug=sandbox&state=QUEUED%2CRUNNING%2CAWAITING_APPROVAL",
    );
  });

  it("lists an in-progress operation that the environment's recent operations no longer hold", async () => {
    serve({
      pages: { sandbox: { ...sandboxPage, recent_operations: [] } },
      active: { sandbox: [refreshOperation] },
    });
    renderOverview();
    expect(await screen.findByText("Refresh inventory: sandbox")).toBeInTheDocument();
    expect(screen.getByText("Running now (1)")).toBeInTheDocument();
  });

  it("says nothing is running when no operation is in progress, whatever the recent operations say", async () => {
    serve({ active: { sandbox: [] } });
    renderOverview();
    expect(await screen.findByText("Nothing running.")).toBeInTheDocument();
    expect(screen.getByText("Running now (0)")).toBeInTheDocument();
    expect(screen.queryByText("Refresh inventory: sandbox")).not.toBeInTheDocument();
  });

  it("shows the step a running operation is on, with its step name and a determinate bar", async () => {
    serve({ active: { sandbox: [refreshOperation] } });
    renderOverview();
    expect(await screen.findByText(/step 3 of 12/)).toBeInTheDocument();
    expect(screen.getByText("write_import_blocks").className).toContain("font-mono");
    const bar = screen.getByRole("progressbar", { name: "Progress of Refresh inventory: sandbox" });
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "12");
    expect(bar).toHaveAttribute("aria-valuenow", "2");
  });

  it("shows a queued operation's first step without a step name, and none for an operation without progress", async () => {
    const queued = {
      ...refreshOperation,
      id: "op-2",
      state: "QUEUED",
      title: "Reboot instance: demo-orders",
      progress: { completed_steps: 0, total_steps: 5, current_step: null },
    };
    const unknown = { ...refreshOperation, id: "op-3", title: "Custom task", progress: null };
    serve({ active: { sandbox: [queued, unknown] } });
    renderOverview();
    expect(await screen.findByText("Reboot instance: demo-orders")).toBeInTheDocument();
    expect(screen.getByText("step 1 of 5")).toBeInTheDocument();
    expect(screen.getAllByRole("progressbar")).toHaveLength(1);
    expect(screen.getByText("Custom task")).toBeInTheDocument();
  });

  it("opens the inventory refresh and adoption tasks for the environment", async () => {
    serve();
    renderOverview();
    await userEvent.click(await screen.findByRole("button", { name: "Adopt" }));
    expect(openTask).toHaveBeenLastCalledWith({ kind: "terraform_adoption", environmentSlug: "sandbox" });
    await userEvent.click(screen.getByRole("button", { name: "Refresh inventory" }));
    expect(openTask).toHaveBeenLastCalledWith({ kind: "inventory_refresh", environmentSlug: "sandbox" });
  });

  it("reports a failed load instead of staying on the loading message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        return Response.json({ detail: "database is locked" }, { status: 500 });
      }),
    );
    renderOverview();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("database is locked"));
  });

  it("keeps the new environment's overview when the previous environment's answer arrives late", async () => {
    const sandboxGate = gate();
    serve({ gates: { sandbox: sandboxGate.opened }, active: { sandbox: [refreshOperation] } });
    renderOverview();
    await userEvent.click(await screen.findByRole("button", { name: "Switch to Production" }));
    expect(await screen.findByText("9")).toBeInTheDocument();
    expect(screen.getByText("15.8")).toBeInTheDocument();

    sandboxGate.open();
    await settle();

    expect(screen.getByText("9")).toBeInTheDocument();
    expect(screen.getByText("15.8")).toBeInTheDocument();
    expect(screen.queryByText("16.4")).not.toBeInTheDocument();
    expect(screen.queryByText("Refresh inventory: sandbox")).not.toBeInTheDocument();
    expect(screen.getByText("Production")).toBeInTheDocument();
    expect(screen.queryByText("Sandbox")).not.toBeInTheDocument();
  });

  it("drops the previous environment's overview at once when the environment changes", async () => {
    const productionGate = gate();
    serve({ gates: { production: productionGate.opened }, active: { sandbox: [refreshOperation] } });
    renderOverview();
    expect(await screen.findByText("16.4")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Switch to Production" }));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("16.4")).not.toBeInTheDocument();
    expect(screen.queryByText("Refresh inventory: sandbox")).not.toBeInTheDocument();

    productionGate.open();
    expect(await screen.findByText("15.8")).toBeInTheDocument();
  });
});
