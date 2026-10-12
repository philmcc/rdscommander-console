import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider, useEnvironment } from "@/app/EnvironmentContext";
import { TaskProvider, type TaskRequest, useTask } from "@/features/tasks/TaskContext";
import ClustersPage from "./ClustersPage";
import InstancesPage from "./InstancesPage";
import ParameterGroupsPage from "./ParameterGroupsPage";
import SnapshotsPage from "./SnapshotsPage";

const openTask = vi.hoisted(() => vi.fn());
vi.mock("@/features/tasks/TaskContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/tasks/TaskContext")>();
  return {
    ...actual,
    useTask: () => {
      const task = actual.useTask();
      return {
        ...task,
        open: (request: TaskRequest) => {
          openTask(request);
          task.open(request);
        },
      };
    },
  };
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
  company_display_name: "T",
  operator_name: "phil",
  engine_version: "0.2.0",
  csrf_token: "t",
  environments: [environment("sandbox", "Sandbox", false), environment("production", "Production", true)],
};

function record(identifier: string, kind: string, environmentSlug = "sandbox") {
  return {
    kind,
    arn: `arn:${identifier}`,
    identifier,
    environment_slug: environmentSlug,
    target_key: "t",
    ownership: "unmanaged",
    status: "available",
    facts: { engine_version: "16.4", group_scope: "instance", parameter_group_family: "postgres16" },
    relationships: {},
    raw: {},
    snapshot_id: 1,
    terraform_address: null,
    parameter_apply_status: null,
    resource_id: `id-${identifier}`,
  };
}

/** Answers bootstrap and the resource list; `listings` is consumed one answer per request. */
function serve(listings: unknown[][]) {
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.includes("/api/v1/environments/sandbox/resources")) {
        return Response.json({ resources: listings.length > 1 ? listings.shift() : listings[0] });
      }
      if (url.endsWith("/api/v1/catalogue")) return Response.json({ definitions: [] });
      throw new Error(`unexpected ${url}`);
    }),
  );
  return requested;
}

function SwitchToProduction() {
  const { select } = useEnvironment();
  return (
    <button type="button" onClick={() => select("production")}>
      Switch to Production
    </button>
  );
}

function renderPage(page: ReactNode) {
  render(
    <MemoryRouter>
      <EnvironmentProvider>
        <TaskProvider>
          <SwitchToProduction />
          {page}
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

/** A gate that a test opens when the held answer should arrive. */
function gate() {
  let open = () => {};
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { opened, open };
}

/** Serves each environment's instance list; a list with a gate answers only once the gate opens. */
function serveEnvironments(gates: Record<string, Promise<void>>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.endsWith("/api/v1/catalogue")) return Response.json({ definitions: [] });
      const slug = url.match(/\/environments\/([^/]+)\/resources/)?.[1] ?? "";
      await gates[slug];
      const identifier = slug === "production" ? "prod-orders" : "sandbox-orders";
      return Response.json({ resources: [record(identifier, "instance", slug)] });
    }),
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

describe("estate list pages", () => {
  it.each([
    ["Instances", InstancesPage],
    ["Clusters", ClustersPage],
    ["Parameter groups", ParameterGroupsPage],
    ["Snapshots", SnapshotsPage],
  ])("%s page has its title", async (title, Page) => {
    serve([[]]);
    renderPage(<Page />);
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
  });

  it("requests the resources of its kind in the selected environment and lists them", async () => {
    const requested = serve([[record("demo-orders", "instance")]]);
    renderPage(<InstancesPage />);
    expect(await screen.findByText("demo-orders")).toBeInTheDocument();
    expect(requested).toContain("/api/v1/environments/sandbox/resources?kind=instance");
    expect(screen.queryByRole("button", { name: "Create" })).not.toBeInTheDocument();
  });

  it("offers a refresh when the inventory has none of the kind", async () => {
    serve([[]]);
    renderPage(<SnapshotsPage />);
    expect(await screen.findByText("No snapshots in the latest inventory.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh inventory" }));
    expect(openTask).toHaveBeenCalledWith({ kind: "inventory_refresh", environmentSlug: "sandbox" });
  });

  it("creates a parameter group in the selected environment", async () => {
    serve([[record("demo-postgres16-tuned", "parameter_group")]]);
    renderPage(<ParameterGroupsPage />);
    expect(await screen.findByText("demo-postgres16-tuned")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(openTask).toHaveBeenCalledWith({ kind: "parameter_group_create", environmentSlug: "sandbox" });
  });

  it("reports a failed load", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        return Response.json({ detail: "database is locked" }, { status: 500 });
      }),
    );
    renderPage(<InstancesPage />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("database is locked"));
    expect(screen.queryByText(/in the latest inventory/)).not.toBeInTheDocument();
  });

  it("reloads the list when the task panel closes", async () => {
    const requested = serve([[], [record("demo-orders", "instance")]]);
    let task: ReturnType<typeof useTask> | undefined;
    function CaptureTask() {
      task = useTask();
      return null;
    }
    renderPage(
      <>
        <CaptureTask />
        <InstancesPage />
      </>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Refresh inventory" }));
    expect(await screen.findByText(/This task is not available/)).toBeInTheDocument();
    act(() => task?.close());
    expect(await screen.findByText("demo-orders")).toBeInTheDocument();
    expect(requested.filter((url) => url.includes("/resources?"))).toHaveLength(2);
  });

  it("keeps the new environment's list when the previous environment's answer arrives late", async () => {
    const sandboxGate = gate();
    serveEnvironments({ sandbox: sandboxGate.opened });
    renderPage(<InstancesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Switch to Production" }));
    expect(await screen.findByText("prod-orders")).toBeInTheDocument();

    sandboxGate.open();
    await settle();

    expect(screen.getByText("prod-orders")).toBeInTheDocument();
    expect(screen.queryByText("sandbox-orders")).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("Production / Estate");
  });

  it("drops the previous environment's list at once and shows loading until the new one answers", async () => {
    const productionGate = gate();
    serveEnvironments({ production: productionGate.opened });
    renderPage(<InstancesPage />);
    expect(await screen.findByText("sandbox-orders")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Switch to Production" }));
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("sandbox-orders")).not.toBeInTheDocument();

    productionGate.open();
    expect(await screen.findByText("prod-orders")).toBeInTheDocument();
  });
});
