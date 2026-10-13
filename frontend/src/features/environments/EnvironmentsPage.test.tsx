import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useParams } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { EnvironmentOverview, OperationSummary } from "@/api/types";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { OperationDetail } from "@/features/operations/OperationDetail";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider } from "@/features/tasks/TaskContext";
import EnvironmentsPage from "./EnvironmentsPage";

function summary(slug: string, displayName: string, isProduction: boolean, simulated = false) {
  return {
    slug,
    display_name: displayName,
    requires_approval: true,
    is_production: isProduction,
    change_management: "terraform",
    allow_direct_changes_to_foreign_owned: false,
    targets: [
      {
        target_key: `${slug}/000000000000/eu-west-2`,
        aws_account_id: "000000000000",
        aws_region: "eu-west-2",
        aws_profile_name: null,
        simulated,
      },
    ],
  };
}
function bootstrapOf(...environments: ReturnType<typeof summary>[]) {
  return {
    operator_name: "phil",
    console_version: "0.2.0",
    csrf_token: "t",
    environments,
  };
}
function overview(
  slug: string,
  displayName: string,
  overrides: Partial<EnvironmentOverview> = {},
): EnvironmentOverview {
  return {
    slug,
    display_name: displayName,
    is_production: false,
    change_management: "terraform",
    allow_direct_changes_to_foreign_owned: false,
    inventory_refresh_minutes: 15,
    drift_check_hours: 24,
    counts: { instance: 4, cluster: 1, parameter_group: 2, snapshot: 3 },
    ownership_counts: { unmanaged: 7 },
    engine_versions: { "16.4": 5 },
    last_refresh_at_utc: "2026-10-10T10:00:00Z",
    running_operation_count: 0,
    attention_count: 0,
    ...overrides,
  };
}

const sandboxOverview = overview("sandbox", "Sandbox", { attention_count: 3, running_operation_count: 1 });
const productionOverview = overview("production", "Production", {
  is_production: true,
  change_management: "direct",
  last_refresh_at_utc: null,
  counts: { instance: 12, cluster: 2, parameter_group: 5, snapshot: 40 },
});

const catalogue = {
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
      options_schema: { type: "object", properties: {} },
    },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
});

interface Answers {
  bootstrap: () => unknown;
  environments: () => { environments: EnvironmentOverview[] } | Response;
  profiles?: string[];
  /** The import operations `GET /operations?kind=environment_import` answers; none by default. */
  imports?: OperationSummary[];
  /** Details of single operations by id. */
  operationDetails?: Record<string, object>;
}

/** Answers every request the page makes; each call to `bootstrap` or `environments` is counted. */
function stubApi(answers: Answers) {
  const calls = { bootstrap: 0, environments: 0, operationLists: [] as string[] };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://console.test");
      const path = url.pathname;
      if (path === "/api/v1/operations") {
        calls.operationLists.push(url.search);
        return Response.json({ operations: answers.imports ?? [] });
      }
      const detail = answers.operationDetails?.[path.replace("/api/v1/operations/", "")];
      if (detail) return Response.json(detail);
      if (path === "/api/v1/bootstrap") {
        calls.bootstrap += 1;
        return Response.json(answers.bootstrap());
      }
      if (path === "/api/v1/environments") {
        calls.environments += 1;
        const answer = answers.environments();
        return answer instanceof Response ? answer : Response.json(answer);
      }
      if (path === "/api/v1/environments/import/profiles")
        return Response.json({ profiles: answers.profiles ?? [] });
      if (path === "/api/v1/catalogue") return Response.json(catalogue);
      throw new Error(`unexpected ${path}`);
    }),
  );
  return calls;
}

function renderPage() {
  render(
    <MemoryRouter>
      <EnvironmentProvider>
        <TaskProvider>
          <EnvironmentsPage />
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

describe("EnvironmentsPage", () => {
  it("shows a card per environment with its mode, counts, last refresh and attention count", async () => {
    stubApi({
      bootstrap: () =>
        bootstrapOf(summary("sandbox", "Sandbox", false, true), summary("production", "Production", true)),
      environments: () => ({ environments: [sandboxOverview, productionOverview] }),
    });
    renderPage();
    const sandbox = await screen.findByRole("article", { name: "Sandbox" });
    expect(within(sandbox).getByText("terraform")).toBeInTheDocument();
    expect(within(sandbox).getByText("simulated")).toBeInTheDocument();
    expect(
      within(within(sandbox).getByRole("list", { name: "Resource counts" }))
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["4 Instances", "1 Clusters", "2 Parameter groups", "3 Snapshots"]);
    expect(within(sandbox).getByText(/last refresh/)).toHaveTextContent(
      new Date("2026-10-10T10:00:00Z").toLocaleString(),
    );
    expect(within(sandbox).getByText("3 items need attention")).toBeInTheDocument();
    expect(within(sandbox).getByText("1 operation running")).toBeInTheDocument();

    const production = screen.getByRole("article", { name: "Production" });
    expect(within(production).getByText("production", { selector: "span" })).toBeInTheDocument();
    expect(within(production).getByText("direct")).toBeInTheDocument();
    expect(within(production).getByText(/last refresh never/)).toBeInTheDocument();
    expect(within(production).getByText("Nothing needs attention")).toBeInTheDocument();
  });

  it("opens an environment or compares it from its card", async () => {
    stubApi({
      bootstrap: () =>
        bootstrapOf(summary("sandbox", "Sandbox", false), summary("production", "Production", true)),
      environments: () => ({ environments: [sandboxOverview, productionOverview] }),
    });
    renderPage();
    const production = await screen.findByRole("article", { name: "Production" });
    expect(within(production).getByRole("link", { name: "Open Production" })).toHaveAttribute(
      "href",
      "/environments/production",
    );
    expect(within(production).getByRole("link", { name: "Compare Production with another" })).toHaveAttribute(
      "href",
      "/environments/compare?left=production",
    );
  });

  it("offers the import wizard only when asked for, and shows it at once when there is nothing yet", async () => {
    stubApi({
      bootstrap: () => bootstrapOf(summary("sandbox", "Sandbox", false)),
      environments: () => ({ environments: [sandboxOverview] }),
      profiles: ["example-dev"],
    });
    renderPage();
    await screen.findByRole("article", { name: "Sandbox" });
    expect(screen.queryByRole("heading", { name: "Import an environment" })).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Import environment" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByRole("button", { name: "example-dev" })).toBeInTheDocument();
  });

  it("invites the first import when there are no environments", async () => {
    stubApi({
      bootstrap: () => bootstrapOf(),
      environments: () => ({ environments: [] }),
      profiles: ["example-dev"],
    });
    renderPage();
    expect(await screen.findByText("Import your first environment to begin.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "example-dev" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use the simulated account" })).toBeInTheDocument();
  });

  it("says why the list could not be read", async () => {
    stubApi({
      bootstrap: () => bootstrapOf(summary("sandbox", "Sandbox", false)),
      environments: () => Response.json({ detail: "the control database is down" }, { status: 500 }),
    });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the environments: the control database is down",
    );
  });

  it("shows an imported environment as soon as the import's task panel closes", async () => {
    let imported = false;
    const calls = stubApi({
      bootstrap: () =>
        imported
          ? bootstrapOf(summary("sandbox", "Sandbox", false), summary("staging", "Staging", false))
          : bootstrapOf(summary("sandbox", "Sandbox", false)),
      environments: () => ({
        environments: imported ? [sandboxOverview, overview("staging", "Staging")] : [sandboxOverview],
      }),
      profiles: [],
    });
    renderPage();
    await screen.findByRole("article", { name: "Sandbox" });
    expect(screen.queryByRole("article", { name: "Staging" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Import environment" }));
    await userEvent.click(await screen.findByRole("button", { name: "Use the simulated account" }));
    const dialog = await screen.findByRole("dialog", { name: "Import environment" });
    imported = true;
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("article", { name: "Staging" })).toBeInTheDocument();
    await waitFor(() => expect(calls.bootstrap).toBe(2));
    expect(calls.environments).toBe(2);
  });
});

function importOperation(overrides: Partial<OperationSummary>): OperationSummary {
  return {
    id: "op-import",
    operation_kind: "environment_import",
    title: "Import environment: staging",
    state: "SUCCEEDED",
    environment_slug: null,
    target_key: null,
    resource_arn: null,
    resource_id: null,
    approval_required_text: null,
    created_by_operator_name: "phil",
    approved_by_operator_name: null,
    failure_summary: null,
    created_at_utc: "2026-10-10T09:30:00Z",
    updated_at_utc: "2026-10-10T09:31:00Z",
    finished_at_utc: "2026-10-10T09:31:00Z",
    progress: null,
    ...overrides,
  };
}

function OperationAtAddress() {
  const { operationId } = useParams();
  return <OperationDetail operationId={operationId ?? ""} />;
}

describe("EnvironmentsPage recent imports", () => {
  it("lists recent imports with their state and time, and a failed one leads to its diagnostic", async () => {
    const failed = importOperation({
      id: "op-failed",
      title: "Import environment: example-prod",
      state: "FAILED",
      failure_summary: "The AWS SSO session of profile example-prod has expired",
      created_at_utc: "2026-10-11T08:15:00Z",
    });
    const calls = stubApi({
      bootstrap: () => bootstrapOf(summary("sandbox", "Sandbox", false)),
      environments: () => ({ environments: [sandboxOverview] }),
      imports: [failed, importOperation({})],
      operationDetails: {
        "op-failed": {
          ...failed,
          plan: {},
          step_attempts: [],
          diagnostic_reports: [
            {
              id: 1,
              step_name: "confirm_account",
              severity: "error",
              diagnostic_kind: "credentials",
              title: "The SSO session has expired",
              detail: "",
              fix_suggestion: "Run aws sso login --profile example-prod, then import again",
              created_at_utc: "2026-10-11T08:15:01Z",
            },
          ],
          execution_records: [],
          terraform_runs: [],
          change_set: null,
          requires_reboot: false,
          downtime: "none",
        },
      },
    });
    render(
      <MemoryRouter initialEntries={["/environments"]}>
        <EnvironmentProvider>
          <TaskProvider>
            <Routes>
              <Route path="/environments" element={<EnvironmentsPage />} />
              <Route path="/operations/:operationId" element={<OperationAtAddress />} />
            </Routes>
          </TaskProvider>
        </EnvironmentProvider>
      </MemoryRouter>,
    );
    const imports = await screen.findByRole("region", { name: "Recent imports" });
    const rows = await within(imports).findAllByRole("listitem");
    expect(rows).toHaveLength(2);
    const failedRow = rows[0] as HTMLElement;
    expect(within(failedRow).getByText("FAILED")).toBeInTheDocument();
    expect(within(failedRow).getByText("2026-10-11 08:15 UTC")).toBeInTheDocument();
    expect(
      within(failedRow).getByText("The AWS SSO session of profile example-prod has expired"),
    ).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("SUCCEEDED")).toBeInTheDocument();
    expect(calls.operationLists[0]).toContain("kind=environment_import");
    expect(calls.operationLists[0]).not.toContain("environment_slug");

    await userEvent.click(within(failedRow).getByRole("link", { name: "Import environment: example-prod" }));
    expect(
      await screen.findByText("Fix: Run aws sso login --profile example-prod, then import again"),
    ).toBeInTheDocument();
  });

  it("says when nothing has been imported from the console yet", async () => {
    stubApi({
      bootstrap: () => bootstrapOf(summary("sandbox", "Sandbox", false)),
      environments: () => ({ environments: [sandboxOverview] }),
    });
    renderPage();
    const imports = await screen.findByRole("region", { name: "Recent imports" });
    expect(await within(imports).findByText("No imports yet.")).toBeInTheDocument();
  });
});
