import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import HistoryPage from "./HistoryPage";

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
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  window.localStorage.clear();
});

function serve(environments = bootstrap.environments) {
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json({ ...bootstrap, environments });
      if (url.startsWith("/api/v1/history/changes"))
        return Response.json({
          events: [
            {
              id: 1,
              resource_arn: "arn:aws:rds:eu-west-2:1:db:demo-orders",
              resource_id: "abc",
              environment_slug: "sandbox",
              target_key: "t",
              attribute_path: "deletion_protection",
              value_before: false,
              value_after: true,
              explanation: "outside_console",
              operation_id: null,
              detected_at_utc: "2026-10-10T10:00:00Z",
              reviewed_by_operator_name: null,
              reviewed_at_utc: null,
            },
          ],
        });
      if (url.startsWith("/api/v1/history")) return Response.json({ records: [] });
      throw new Error(`unexpected ${url}`);
    }),
  );
  return requested;
}

function Hash() {
  return <output aria-label="Location hash">{useLocation().hash}</output>;
}

function renderPage(entry = "/history") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <EnvironmentProvider>
        <HistoryPage />
        <Hash />
      </EnvironmentProvider>
    </MemoryRouter>,
  );
}

describe("HistoryPage", () => {
  it("opens on the execution records and keeps the tab in the URL hash", async () => {
    const requested = serve();
    renderPage();
    expect(await screen.findByRole("heading", { name: "History" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Execution records" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByLabelText("Text")).toBeInTheDocument();
    expect(requested.some((url) => url.startsWith("/api/v1/history?environment_slug=sandbox"))).toBe(true);

    await userEvent.click(screen.getByRole("tab", { name: "Changes" }));
    expect(screen.getByLabelText("Location hash")).toHaveTextContent("#changes");
    expect(await screen.findByText("deletion_protection")).toBeInTheDocument();
    expect(screen.queryByLabelText("Text")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Execution records" }));
    expect(screen.getByLabelText("Location hash")).toHaveTextContent("#records");
    expect(await screen.findByLabelText("Text")).toBeInTheDocument();
  });

  it("opens on the changes when the address says so", async () => {
    serve();
    renderPage("/history#changes");
    expect(await screen.findByText("deletion_protection")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");
  });

  it("asks for an environment before it can show a history", async () => {
    serve([]);
    renderPage();
    expect(await screen.findByText("Add an environment to begin.")).toBeInTheDocument();
  });
});
