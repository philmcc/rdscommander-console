import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import SettingsPage from "@/features/settings/SettingsPage";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider } from "@/features/tasks/TaskContext";
import { EnvironmentProvider } from "./EnvironmentContext";
import { Shell } from "./Shell";

const bootstrap = {
  company_display_name: "Test Co",
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
      targets: [
        {
          target_key: "sandbox/0/eu-west-2",
          aws_account_id: "0",
          aws_region: "eu-west-2",
          aws_profile_name: null,
          simulated: true,
        },
      ],
    },
    {
      slug: "production",
      display_name: "Production",
      requires_approval: true,
      is_production: true,
      change_management: "direct",
      allow_direct_changes_to_foreign_owned: false,
      targets: [
        {
          target_key: "production/1/eu-west-2",
          aws_account_id: "1",
          aws_region: "eu-west-2",
          aws_profile_name: "p",
          simulated: false,
        },
      ],
    },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
});

type ResponseSource = Response | Promise<Response>;

interface ShellOptions {
  /** Answer for `/api/v1/attention` given the environment slug asked for. */
  attention?: (environmentSlug: string) => ResponseSource;
  /** Answer for `/api/v1/operations`. */
  operations?: () => ResponseSource;
  /** Where the shell starts; the index route and the settings page are routed. */
  path?: string;
}

function attentionInbox(awaitingApprovalCount: number) {
  return {
    awaiting_approval: Array.from({ length: awaitingApprovalCount }, () => ({
      kind: "awaiting_approval",
      title: "x",
      detail: "",
      environment_slug: "sandbox",
    })),
    pending_reboot: [],
    pending_maintenance: [],
    drift: [],
    outside_changes: [],
    failed_recent: [],
    foreign_owned: [],
    unmanaged: [],
    blue_side: [],
  };
}

const catalogue = {
  definitions: [
    {
      kind: "inventory_refresh",
      title: "Refresh inventory",
      group: "Discovery and adoption",
      summary: "Read everything.",
      applies_to: ["environment"],
      modes: ["terraform", "direct"],
      destructive: false,
      disruptive: false,
      needs_target: false,
      options_schema: { type: "object", properties: {} },
    },
  ],
};

function runningOperations(count: number) {
  return Response.json({ operations: Array.from({ length: count }, () => ({})) });
}

/** A page that fails while rendering, as a page given an answer it cannot show would. */
function BrokenPage(): never {
  throw new Error("the operation's steps could not be read");
}

/** Renders the shell over a stubbed `fetch`; returns the URLs requested, in order. */
function renderShell(options: ShellOptions = {}) {
  const requestedUrls: string[] = [];
  const attention = options.attention ?? (() => Response.json(attentionInbox(1)));
  const operations = options.operations ?? (() => runningOperations(0));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requestedUrls.push(url);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.includes("/api/v1/attention")) {
        return attention(new URL(url, "http://console.test").searchParams.get("environment_slug") ?? "");
      }
      if (url.endsWith("/api/v1/catalogue")) return Response.json(catalogue);
      if (url.endsWith("/api/v1/environments")) return Response.json({ environments: [] });
      if (url.includes("/api/v1/operations")) return operations();
      throw new Error(`unexpected ${url}`);
    }),
  );
  render(
    <MemoryRouter initialEntries={[options.path ?? "/"]}>
      <EnvironmentProvider>
        <TaskProvider>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<p>overview body</p>} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="broken" element={<BrokenPage />} />
            </Route>
          </Routes>
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
  return requestedUrls;
}

describe("Shell", () => {
  it("shows navigation, environment chips with the production marker, the attention count and the body", async () => {
    renderShell();
    expect(await screen.findByText("overview body")).toBeInTheDocument();
    for (const label of [
      "Overview",
      "Instances",
      "Clusters",
      "Parameter groups",
      "Snapshots",
      "Operations",
      "History",
      "Environments",
      "Settings",
    ]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
    const production = screen.getByRole("button", { name: /Production/ });
    expect(production).toHaveTextContent("production");
    expect(production.className).toContain("border-l-status-red");
    expect(await screen.findByLabelText("Needs attention: 1")).toBeInTheDocument();
  });

  it("keeps the dashed simulated badge on a simulated environment's chip in the top bar", async () => {
    renderShell();
    await screen.findByText("overview body");
    const sandbox = screen.getByRole("button", { name: /Sandbox/ });
    expect(within(sandbox).getByText("simulated")).toBeInTheDocument();
    expect(within(screen.getByRole("button", { name: /Production/ })).queryByText("simulated")).toBeNull();
  });

  it("switches environment and remembers it, and toggles the theme", async () => {
    renderShell();
    await screen.findByText("overview body");
    await userEvent.click(screen.getByRole("button", { name: /Production/ }));
    expect(window.localStorage.getItem("rdscommander.selectedEnvironment")).toBe("production");
    await userEvent.click(screen.getByRole("button", { name: "Switch to dark theme" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("keeps the top-bar toggle and the Settings radios in step with each other", async () => {
    renderShell({ path: "/settings" });
    const theme = await screen.findByRole("radiogroup", { name: "Theme" });
    // Choosing Dark in Settings turns the top-bar button round at once: it now offers the light theme.
    await userEvent.click(within(theme).getByRole("radio", { name: "Dark" }));
    expect(screen.getByRole("button", { name: "Switch to light theme" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch to dark theme" })).not.toBeInTheDocument();
    // Clicking the top-bar button moves the radios.
    await userEvent.click(screen.getByRole("button", { name: "Switch to light theme" }));
    expect(within(theme).getByRole("radio", { name: "Light" })).toBeChecked();
    expect(within(theme).getByRole("radio", { name: "Dark" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeInTheDocument();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("collapses the sidebar to icons and keeps every page reachable", async () => {
    renderShell();
    await screen.findByText("overview body");
    await userEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(screen.getByRole("navigation", { name: "Primary" })).toHaveAttribute("data-collapsed", "true");
    for (const [label, path] of [
      ["Overview", "/"],
      ["Instances", "/estate/instances"],
      ["Clusters", "/estate/clusters"],
      ["Parameter groups", "/estate/parameter-groups"],
      ["Snapshots", "/estate/snapshots"],
      ["Operations", "/operations"],
      ["History", "/history"],
      ["Environments", "/environments"],
      ["Settings", "/settings"],
    ] as const) {
      const link = screen.getByRole("link", { name: label });
      expect(link).toHaveAttribute("href", path);
      expect(link).toHaveAttribute("title", label);
      expect(link.textContent).toBe("");
    }
  });

  it("shows the progress strip while any operation runs, whichever environment is selected", async () => {
    const requestedUrls = renderShell({ operations: () => runningOperations(1) });
    expect(await screen.findByRole("progressbar", { name: "Operations running" })).toBeInTheDocument();
    const operationUrls = requestedUrls.filter((url) => url.includes("/api/v1/operations"));
    expect(operationUrls.length).toBeGreaterThan(0);
    for (const url of operationUrls) {
      expect(url).toContain("state=RUNNING");
      expect(url).not.toContain("environment_slug");
    }
  });

  it("shows no progress strip when nothing is running", async () => {
    renderShell();
    await screen.findByLabelText("Needs attention: 1");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("shows an unknown bell, not zero, when the attention request fails, and still shows the strip", async () => {
    renderShell({
      attention: () => new Response("boom", { status: 500 }),
      operations: () => runningOperations(1),
    });
    const bell = await screen.findByLabelText("Needs attention: unknown");
    expect(bell).toHaveTextContent("?");
    expect(screen.queryByLabelText("Needs attention: 0")).toBeNull();
    expect(await screen.findByRole("progressbar", { name: "Operations running" })).toBeInTheDocument();
  });

  it("keeps the strip off but shows the attention count when the operations request fails", async () => {
    renderShell({ operations: () => new Response("boom", { status: 500 }) });
    expect(await screen.findByLabelText("Needs attention: 1")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("does not show the previous environment's count while the new environment's count loads", async () => {
    let answerProduction: (response: Response) => void = () => {};
    const productionAnswer = new Promise<Response>((resolve) => {
      answerProduction = resolve;
    });
    renderShell({
      attention: (slug) => (slug === "production" ? productionAnswer : Response.json(attentionInbox(1))),
    });
    expect(await screen.findByLabelText("Needs attention: 1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Production/ }));
    expect(await screen.findByLabelText("Needs attention: unknown")).toBeInTheDocument();
    expect(screen.queryByLabelText("Needs attention: 1")).toBeNull();
    answerProduction(Response.json(attentionInbox(3)));
    expect(await screen.findByLabelText("Needs attention: 3")).toBeInTheDocument();
  });

  it("opens the launcher from the Start a task button and from the keyboard", async () => {
    renderShell();
    await screen.findByText("overview body");
    expect(screen.queryByRole("dialog", { name: "Start a task" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start a task" }));
    expect(await screen.findByRole("dialog", { name: "Start a task" })).toBeInTheDocument();
    expect(await screen.findByText("Refresh inventory")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Start a task" })).not.toBeInTheDocument(),
    );
    await userEvent.keyboard("{Control>}k{/Control}");
    expect(await screen.findByPlaceholderText("Start a task…")).toBeInTheDocument();
  });
});

describe("Shell when a page fails", () => {
  it("shows the page's error inside the shell with a way back to the overview, which works", async () => {
    const reactError = vi.spyOn(console, "error").mockImplementation(() => {});
    renderShell({ path: "/broken" });
    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("This page could not be shown");
    expect(failure).toHaveTextContent("the operation's steps could not be read");
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Production/ })).toBeInTheDocument();
    await userEvent.click(within(failure).getByRole("link", { name: "Back to the overview" }));
    expect(await screen.findByText("overview body")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    reactError.mockRestore();
  });
});
