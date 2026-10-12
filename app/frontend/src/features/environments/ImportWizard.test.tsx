import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider, type TaskRequest } from "@/features/tasks/TaskContext";
import { ImportWizard } from "./ImportWizard";

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

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
  openTask.mockClear();
});

const bootstrapResponse = () =>
  Response.json({
    company_display_name: "T",
    operator_name: "phil",
    engine_version: "0.2.0",
    csrf_token: "t",
    environments: [],
  });
const catalogueResponse = () =>
  Response.json({
    definitions: [
      {
        kind: "environment_import",
        title: "Import environment",
        group: "Environments",
        summary: "",
        applies_to: ["environment"],
        modes: ["terraform", "direct"],
        destructive: false,
        disruptive: false,
        needs_target: false,
        options_schema: {
          type: "object",
          properties: {
            slug: { type: "string", ui: { widget: "text", help: "", order: 1 } },
            aws_profile_name: { type: "string", ui: { widget: "profile", help: "", order: 2 } },
          },
        },
      },
    ],
  });

function renderWizard() {
  render(
    <MemoryRouter>
      <EnvironmentProvider>
        <TaskProvider>
          <ImportWizard />
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

describe("ImportWizard", () => {
  it("lists profiles and opens the import task with the chosen profile", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return bootstrapResponse();
        if (url.endsWith("/api/v1/environments/import/profiles"))
          return Response.json({ profiles: ["acme-dev", "acme-prod"] });
        if (url.endsWith("/api/v1/catalogue")) return catalogueResponse();
        throw new Error(`unexpected ${url}`);
      }),
    );
    renderWizard();
    await userEvent.click(await screen.findByRole("button", { name: "acme-prod" }));
    expect(await screen.findByRole("dialog", { name: "Import environment" })).toBeInTheDocument();
    expect(screen.getByLabelText("aws_profile_name")).toHaveValue("acme-prod");
    expect(openTask).toHaveBeenCalledWith({
      kind: "environment_import",
      initialOptions: {
        aws_profile_name: "acme-prod",
        simulated: false,
        change_management: "terraform",
        regions: [],
      },
    });
  });

  it("prefills the simulated account without a profile", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return bootstrapResponse();
        if (url.endsWith("/api/v1/environments/import/profiles"))
          return Response.json({ profiles: ["acme-dev"] });
        if (url.endsWith("/api/v1/catalogue")) return catalogueResponse();
        throw new Error(`unexpected ${url}`);
      }),
    );
    renderWizard();
    await screen.findByRole("button", { name: "acme-dev" });
    await userEvent.click(screen.getByRole("button", { name: "Use the simulated account" }));
    expect(openTask).toHaveBeenCalledWith({
      kind: "environment_import",
      initialOptions: {
        simulated: true,
        aws_profile_name: null,
        regions: ["eu-west-2"],
        change_management: "terraform",
      },
    });
    expect(await screen.findByRole("dialog", { name: "Import environment" })).toBeInTheDocument();
  });

  it("says when this machine has no profiles and still offers the simulated account", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return bootstrapResponse();
        if (url.endsWith("/api/v1/environments/import/profiles")) return Response.json({ profiles: [] });
        throw new Error(`unexpected ${url}`);
      }),
    );
    renderWizard();
    expect(await screen.findByText(/No profiles found/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use the simulated account" })).toBeEnabled();
  });

  it("says why the profiles could not be read and still offers the simulated account", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return bootstrapResponse();
        if (url.endsWith("/api/v1/environments/import/profiles"))
          return Response.json({ detail: "the AWS config file is unreadable" }, { status: 500 });
        throw new Error(`unexpected ${url}`);
      }),
    );
    renderWizard();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not read the AWS profiles: the AWS config file is unreadable",
    );
    expect(screen.getByRole("button", { name: "Use the simulated account" })).toBeEnabled();
  });
});
