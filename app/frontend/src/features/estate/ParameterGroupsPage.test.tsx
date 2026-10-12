import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { TaskProvider } from "@/features/tasks/TaskContext";
import ParameterGroupsPage from "./ParameterGroupsPage";

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
      change_management: "terraform",
      allow_direct_changes_to_foreign_owned: false,
      targets: [],
    },
  ],
};

function group(identifier: string, scope: "instance" | "cluster", family = "postgres16") {
  return {
    kind: "parameter_group",
    arn: `arn:${identifier}`,
    identifier,
    environment_slug: "sandbox",
    target_key: "t",
    ownership: "unmanaged",
    status: "available",
    facts: { group_scope: scope, parameter_group_family: family },
    relationships: {},
    raw: {},
    snapshot_id: 1,
    terraform_address: null,
    parameter_apply_status: null,
    resource_id: `id-${identifier}`,
  };
}

const groups = [
  group("demo-postgres16-tuned", "instance"),
  group("demo-postgres16-other", "instance"),
  group("demo-postgres17-new", "instance", "postgres17"),
  group("demo-aurora16-cluster", "cluster", "aurora-postgresql16"),
];

const comparison = {
  left_name: "demo-postgres16-tuned",
  right_name: "demo-postgres16-other",
  family_left: "postgres16",
  family_right: "postgres16",
  rows: [
    {
      name: "work_mem",
      left: "16384",
      right: "8192",
      default: "4096",
      differs: true,
      apply_type: "dynamic",
      data_type: "integer",
      description: "",
    },
  ],
};

function serve() {
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.endsWith("/api/v1/catalogue")) return Response.json({ definitions: [] });
      if (url.includes("/api/v1/environments/sandbox/resources")) return Response.json({ resources: groups });
      if (url.startsWith("/api/v1/parameters/compare")) return Response.json(comparison);
      throw new Error(`unexpected ${url}`);
    }),
  );
  return requested;
}

function Address() {
  const location = useLocation();
  return <output aria-label="Address">{location.search}</output>;
}

function renderAt(search: string) {
  render(
    <MemoryRouter initialEntries={[`/estate/parameter-groups${search}`]}>
      <EnvironmentProvider>
        <TaskProvider>
          <Address />
          <ParameterGroupsPage />
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  window.localStorage.clear();
});

describe("ParameterGroupsPage comparison", () => {
  it("shows no comparison unless the address asks for one", async () => {
    const requested = serve();
    renderAt("");
    expect(await screen.findByText("demo-postgres16-other")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Compare parameter groups" })).not.toBeInTheDocument();
    expect(requested.some((url) => url.startsWith("/api/v1/parameters/compare"))).toBe(false);
  });

  it("compares a group with its family defaults", async () => {
    const requested = serve();
    renderAt("?compare=id-demo-postgres16-tuned&defaults=1");
    const section = await screen.findByRole("region", { name: "Compare parameter groups" });
    expect(await within(section).findByText("work_mem")).toBeInTheDocument();
    expect(requested).toContain(
      "/api/v1/parameters/compare?left=id-demo-postgres16-tuned&against_defaults=true",
    );
    expect(within(section).getByRole("combobox", { name: "Compare with" })).toHaveValue("defaults");
    // The list of groups is still there below the comparison.
    expect(screen.getByRole("heading", { level: 1, name: "Parameter groups" })).toBeInTheDocument();
  });

  it("asks for a group to compare with when the address names only the left one", async () => {
    const requested = serve();
    renderAt("?compare=id-demo-postgres16-tuned");
    const section = await screen.findByRole("region", { name: "Compare parameter groups" });
    expect(
      within(section).getByText("Choose a group, or the family defaults, to compare with."),
    ).toBeInTheDocument();
    expect(within(section).getByRole("combobox", { name: "Compare with" })).toHaveValue("");
    expect(requested.some((url) => url.startsWith("/api/v1/parameters/compare"))).toBe(false);
  });

  it("offers the other groups of the same scope and compares with the one chosen", async () => {
    const requested = serve();
    renderAt("?compare=id-demo-postgres16-tuned");
    const picker = await screen.findByRole("combobox", { name: "Compare with" });
    await waitFor(() => expect(within(picker).getAllByRole("option").length).toBeGreaterThan(2));
    expect(
      within(picker)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "Choose a group…",
      "Family defaults",
      "demo-postgres16-other (postgres16)",
      "demo-postgres17-new (postgres17)",
    ]);
    await userEvent.selectOptions(picker, "id-demo-postgres16-other");
    expect(await screen.findByRole("columnheader", { name: /demo-postgres16-other/ })).toBeInTheDocument();
    expect(requested).toContain(
      "/api/v1/parameters/compare?left=id-demo-postgres16-tuned&right=id-demo-postgres16-other",
    );
    expect(screen.getByLabelText("Address")).toHaveTextContent(
      "?compare=id-demo-postgres16-tuned&with=id-demo-postgres16-other",
    );
    await userEvent.selectOptions(picker, "defaults");
    await waitFor(() =>
      expect(requested).toContain(
        "/api/v1/parameters/compare?left=id-demo-postgres16-tuned&against_defaults=true",
      ),
    );
    expect(screen.getByLabelText("Address")).toHaveTextContent(
      "?compare=id-demo-postgres16-tuned&defaults=1",
    );
  });

  it("compares with the group the address names, even before the list of groups has loaded", async () => {
    const requested = serve();
    renderAt("?compare=id-demo-postgres16-tuned&with=id-demo-postgres16-other");
    expect(await screen.findByRole("columnheader", { name: /demo-postgres16-other/ })).toBeInTheDocument();
    expect(requested).toContain(
      "/api/v1/parameters/compare?left=id-demo-postgres16-tuned&right=id-demo-postgres16-other",
    );
    expect(screen.getByRole("combobox", { name: "Compare with" })).toHaveValue("id-demo-postgres16-other");
  });

  it("closes the comparison and keeps the rest of the address", async () => {
    serve();
    renderAt("?compare=id-demo-postgres16-tuned&defaults=1&other=kept");
    await userEvent.click(await screen.findByRole("button", { name: "Close comparison" }));
    expect(screen.queryByRole("region", { name: "Compare parameter groups" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Address")).toHaveTextContent("?other=kept");
  });
});
