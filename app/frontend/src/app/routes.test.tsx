import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider } from "@/features/tasks/TaskContext";
import { EnvironmentProvider } from "./EnvironmentContext";
import { AppRoutes } from "./routes";

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
      targets: [],
    },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
});

/** Answers what the shell itself reads; a page that reads anything else is not under test here. */
function stubShellReads() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.includes("/api/v1/attention"))
        return Response.json({ detail: "not under test" }, { status: 500 });
      if (url.includes("/api/v1/operations")) return Response.json({ operations: [] });
      if (url.endsWith("/api/v1/catalogue")) return Response.json({ definitions: [] });
      return Response.json({ detail: "not under test" }, { status: 500 });
    }),
  );
}

describe("AppRoutes", () => {
  it("shows a not-found page inside the shell for an address no page has, with the way back", async () => {
    stubShellReads();
    render(
      <MemoryRouter initialEntries={["/operation/3fa85f64"]}>
        <EnvironmentProvider>
          <TaskProvider>
            <AppRoutes />
          </TaskProvider>
        </EnvironmentProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByText(/\/operation\/3fa85f64/)).toHaveClass("font-mono");
    expect(screen.getByRole("link", { name: "Operations" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sandbox/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Back to the overview" }));
    expect(screen.queryByRole("heading", { level: 1, name: "Page not found" })).not.toBeInTheDocument();
  });
});
