import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider, type TaskRequest } from "@/features/tasks/TaskContext";
import ClustersPage from "./ClustersPage";
import InstancesPage from "./InstancesPage";

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

const bootstrap = {
  company_display_name: "T",
  operator_name: "phil",
  engine_version: "0.2.0",
  csrf_token: "t",
  environments: [
    {
      slug: "sandbox",
      display_name: "Sandbox",
      requires_approval: true,
      is_production: false,
      change_management: "direct",
      allow_direct_changes_to_foreign_owned: false,
      targets: [],
    },
  ],
};

function createDefinition(kind: string, title: string) {
  return {
    kind,
    title,
    group: "Create and remove",
    summary: `${title} from the template.`,
    applies_to: ["environment"],
    modes: ["terraform", "direct"],
    destructive: false,
    disruptive: false,
    needs_target: true,
    pauses_for_approval: true,
    pauses_for_approval_when: null,
    options_schema: { type: "object", properties: {} },
  };
}

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.endsWith("/api/v1/catalogue"))
        return Response.json({
          definitions: [
            createDefinition("instance_create", "Create instance"),
            createDefinition("cluster_create", "Create cluster"),
          ],
        });
      if (url.includes("/api/v1/environments/sandbox/resources")) return Response.json({ resources: [] });
      throw new Error(`unexpected ${url}`);
    }),
  );
}

function renderPage(page: ReactNode) {
  render(
    <MemoryRouter>
      <EnvironmentProvider>
        <TaskProvider>{page}</TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  openTask.mockReset();
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
});

describe("Create buttons on the estate lists", () => {
  it("opens the instance create for the selected environment from the Instances page", async () => {
    serve();
    renderPage(<InstancesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Create instance" }));
    expect(openTask).toHaveBeenCalledWith({ kind: "instance_create", environmentSlug: "sandbox" });
    expect(await screen.findByRole("dialog", { name: "Create instance" })).toBeInTheDocument();
  });

  it("opens the cluster create for the selected environment from the Clusters page", async () => {
    serve();
    renderPage(<ClustersPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Create cluster" }));
    expect(openTask).toHaveBeenCalledWith({ kind: "cluster_create", environmentSlug: "sandbox" });
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Create cluster" })).toBeInTheDocument());
  });
});
