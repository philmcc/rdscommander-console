import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { EnvironmentOverview } from "@/api/types";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { readThemePreference } from "@/theme";
import SettingsPage from "./SettingsPage";

function target(slug: string, simulated: boolean) {
  return {
    target_key: `${slug}/000000000000/eu-west-2`,
    aws_account_id: "000000000000",
    aws_region: "eu-west-2",
    aws_profile_name: simulated ? null : "orders-prod",
    simulated,
  };
}

const bootstrap = {
  operator_name: "Phil McClarence",
  console_version: "0.3.0",
  theme_default: "system",
  csrf_token: "t",
  environments: [
    {
      slug: "sandbox",
      display_name: "Sandbox",
      requires_approval: true,
      is_production: false,
      change_management: "terraform",
      allow_direct_changes_to_foreign_owned: false,
      targets: [target("sandbox", true)],
    },
    {
      slug: "production",
      display_name: "Production",
      requires_approval: true,
      is_production: true,
      change_management: "direct",
      allow_direct_changes_to_foreign_owned: true,
      targets: [target("production", false)],
    },
  ],
};

function overview(slug: string, overrides: Partial<EnvironmentOverview>): EnvironmentOverview {
  return {
    slug,
    display_name: slug,
    is_production: false,
    change_management: "terraform",
    allow_direct_changes_to_foreign_owned: false,
    inventory_refresh_minutes: 15,
    drift_check_hours: 24,
    counts: {},
    ownership_counts: {},
    engine_versions: {},
    last_refresh_at_utc: null,
    running_operation_count: 0,
    attention_count: 0,
    ...overrides,
  };
}

function serve(
  environments: Response = Response.json({
    environments: [
      overview("sandbox", {}),
      overview("production", { inventory_refresh_minutes: 60, drift_check_hours: 6 }),
    ],
  }),
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.endsWith("/api/v1/environments")) return environments;
      throw new Error(`unexpected ${url}`);
    }),
  );
}

function renderPage() {
  render(
    <MemoryRouter>
      <EnvironmentProvider>
        <SettingsPage />
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
});

describe("SettingsPage", () => {
  it("shows the operator and the console version, and where the operator is changed", async () => {
    serve();
    renderPage();
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    const consoleSection = await screen.findByRole("region", { name: "Console" });
    expect(within(consoleSection).getByText("Phil McClarence")).toBeInTheDocument();
    expect(within(consoleSection).getByText("0.3.0")).toBeInTheDocument();
    expect(within(consoleSection).getByText("RDSC_OPERATOR_NAME")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Company" })).not.toBeInTheDocument();
  });

  it("applies and remembers the theme chosen with the radios", async () => {
    serve();
    renderPage();
    const group = await screen.findByRole("radiogroup", { name: "Theme" });
    expect(within(group).getByRole("radio", { name: "System" })).toBeChecked();
    await userEvent.click(within(group).getByRole("radio", { name: "Dark" }));
    expect(within(group).getByRole("radio", { name: "Dark" })).toBeChecked();
    expect(readThemePreference()).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    await userEvent.click(within(group).getByRole("radio", { name: "Light" }));
    expect(readThemePreference()).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("starts with the stored theme preference", async () => {
    window.localStorage.setItem("rdscommander.theme", "dark");
    serve();
    renderPage();
    expect(await screen.findByRole("radio", { name: "Dark" })).toBeChecked();
  });

  it("shows each environment's mode, flags, intervals and targets, and says where to change them", async () => {
    serve();
    renderPage();
    const sandbox = await screen.findByRole("article", { name: "Sandbox" });
    const production = screen.getByRole("article", { name: "Production" });
    expect(within(production).getByText("production", { selector: "span" })).toBeInTheDocument();
    // Once on the chip and once on the target.
    expect(within(sandbox).getAllByText("simulated")).toHaveLength(2);
    expect(within(sandbox).getByText("terraform")).toBeInTheDocument();
    expect(within(production).getByText("direct")).toBeInTheDocument();
    const facts = (article: HTMLElement) =>
      Object.fromEntries(
        within(article)
          .getAllByRole("term")
          .map((term) => [term.textContent, term.nextElementSibling?.textContent]),
      );
    expect(facts(sandbox)).toMatchObject({
      "Foreign-owned changes allowed": "no",
      "Approval required": "yes",
      "Inventory refresh every": "15 minutes",
      "Drift check every": "24 hours",
    });
    expect(facts(production)).toMatchObject({
      "Foreign-owned changes allowed": "yes",
      "Inventory refresh every": "60 minutes",
      "Drift check every": "6 hours",
    });
    expect(within(production).getByText("000000000000")).toBeInTheDocument();
    expect(within(production).getByText("eu-west-2")).toBeInTheDocument();
    expect(within(production).getByText("orders-prod")).toBeInTheDocument();
    const environments = screen.getByRole("region", { name: "Environments" });
    expect(within(environments).getByText("config/environments.yaml")).toBeInTheDocument();
    expect(
      within(environments).getByText(
        (_, element) =>
          element?.tagName === "P" &&
          element.textContent ===
            "Change these in config/environments.yaml and restart, or import a new environment.",
      ),
    ).toBeInTheDocument();
    expect(within(environments).getByRole("link", { name: "import a new environment" })).toHaveAttribute(
      "href",
      "/environments",
    );
  });

  it("still lists the environments when their intervals cannot be read", async () => {
    serve(Response.json({ detail: "database is locked" }, { status: 500 }));
    renderPage();
    expect(await screen.findByRole("article", { name: "Production" })).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("database is locked");
  });

  it("says where the Terraform settings live, since the console does not read them out", async () => {
    serve();
    renderPage();
    const terraform = await screen.findByRole("region", { name: "Terraform" });
    expect(within(terraform).getByText("config/terraform.yaml")).toBeInTheDocument();
  });
});
