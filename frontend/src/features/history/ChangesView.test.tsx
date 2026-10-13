import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { ChangesView } from "./ChangesView";

const events = {
  events: [
    {
      id: 1,
      resource_arn: "arn:i",
      resource_id: "YXJuOmk",
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
    {
      id: 2,
      resource_arn: "arn:i",
      resource_id: "YXJuOmk",
      environment_slug: "sandbox",
      target_key: "t",
      attribute_path: "parameter.work_mem",
      value_before: "16384",
      value_after: "65536",
      explanation: "operation",
      operation_id: "op-1",
      detected_at_utc: "2026-10-10T09:00:00Z",
      reviewed_by_operator_name: null,
      reviewed_at_utc: null,
    },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
});

/** The bootstrap answer `apiPost` needs for its CSRF token. */
const bootstrapAnswer = {
  csrf_token: "t",
  environments: [],
  operator_name: "",
  engine_version: "",
};

function renderChanges(environmentSlug = "sandbox") {
  return render(
    <MemoryRouter>
      <ChangesView environmentSlug={environmentSlug} />
    </MemoryRouter>,
  );
}

describe("ChangesView", () => {
  it("filters by explanation and marks a change reviewed", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push(`${init?.method ?? "GET"} ${url}`);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrapAnswer);
        if (url.includes("/api/v1/history/changes/1/review"))
          return Response.json({
            ...events.events[0],
            reviewed_by_operator_name: "phil",
            reviewed_at_utc: "2026-10-10T11:00:00Z",
          });
        if (url.includes("/api/v1/history/changes"))
          return Response.json(
            url.includes("explanation=outside_console") ? { events: [events.events[0]] } : events,
          );
        throw new Error(`unexpected ${url}`);
      }),
    );
    renderChanges();
    expect(await screen.findByText("parameter.work_mem")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Explanation"), "outside_console");
    expect(await screen.findByText("deletion_protection")).toBeInTheDocument();
    expect(screen.queryByText("parameter.work_mem")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
    expect(calls.some((c) => c.startsWith("POST") && c.endsWith("/history/changes/1/review"))).toBe(true);
  });

  it("asks for the environment's changes, only the unreviewed ones when the box is ticked", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => Response.json(events));
    vi.stubGlobal("fetch", fetchMock);
    renderChanges("production");
    await screen.findByText("parameter.work_mem");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/history/changes?environment_slug=production"),
    );
    await userEvent.click(screen.getByLabelText("Unreviewed only"));
    await waitFor(() => expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain("unreviewed_only=true"));
  });

  it("narrows the rows by resource and by attribute as they are typed", async () => {
    const other = {
      ...events.events[1],
      id: 3,
      resource_arn: "arn:aws:rds:eu-west-2:1:db:demo-orders",
      attribute_path: "db_instance_class",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ events: [...events.events, other] })),
    );
    renderChanges();
    await screen.findByText("db_instance_class");
    await userEvent.type(screen.getByLabelText("Resource"), "demo-orders");
    expect(screen.getByText("db_instance_class")).toBeInTheDocument();
    expect(screen.queryByText("deletion_protection")).not.toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Resource"));
    await userEvent.type(screen.getByLabelText("Attribute"), "work_mem");
    expect(screen.getByText("parameter.work_mem")).toBeInTheDocument();
    expect(screen.queryByText("db_instance_class")).not.toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Attribute"));
    await userEvent.type(screen.getByLabelText("Attribute"), "nothing-like-this");
    expect(screen.getByText("No changes match.")).toBeInTheDocument();
  });

  it("links a console change to its operation and the resource to its page, and marks the other one amber", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json(events)),
    );
    renderChanges();
    const byConsole = await screen.findByRole("link", { name: "by the console" });
    expect(byConsole).toHaveAttribute("href", "/operations/op-1");
    expect(screen.getAllByRole("link", { name: "i" })[0]).toHaveAttribute("href", "/resources/YXJuOmk");
    expect(screen.getByText("outside the console")).toHaveClass("text-status-amber");
  });

  it("shows who reviewed a change instead of the button once it is reviewed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          events: [
            {
              ...events.events[0],
              reviewed_by_operator_name: "phil",
              reviewed_at_utc: "2026-10-10T11:00:00Z",
            },
          ],
        }),
      ),
    );
    renderChanges();
    expect(await screen.findByText("reviewed by phil")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark reviewed" })).not.toBeInTheDocument();
  });

  it("says why the changes could not be loaded or a review could not be saved", async () => {
    let failReview = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrapAnswer);
        if (url.includes("/review") && failReview)
          return Response.json({ detail: "not allowed" }, { status: 403 });
        if (url.includes("explanation=operation"))
          return Response.json({ detail: "database is locked" }, { status: 500 });
        return Response.json({ events: [events.events[0]] });
      }),
    );
    renderChanges();
    failReview = true;
    await userEvent.click(await screen.findByRole("button", { name: "Mark reviewed" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not mark the change reviewed: not allowed",
    );
    await userEvent.selectOptions(screen.getByLabelText("Explanation"), "operation");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the changes: database is locked",
    );
  });

  it("says when the answer is capped, so the filters are known to search only the latest changes", async () => {
    const capped = Array.from({ length: 500 }, (_, index) => ({
      ...events.events[1],
      id: 1000 - index,
      attribute_path: `attribute_${index}`,
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ events: capped })),
    );
    renderChanges();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Showing the latest 500 changes; the resource and attribute filters search only these. Narrow by explanation or unreviewed to see older ones.",
    );
    await userEvent.type(screen.getByLabelText("Attribute"), "no-such-attribute");
    expect(screen.getByText("None of the latest 500 changes match.")).toBeInTheDocument();
    expect(screen.queryByText("No changes match.")).not.toBeInTheDocument();
  });

  it("shows no cap notice and says plainly that nothing matches when the answer is complete", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json(events)),
    );
    renderChanges();
    await screen.findByText("parameter.work_mem");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Attribute"), "no-such-attribute");
    expect(screen.getByText("No changes match.")).toBeInTheDocument();
  });

  it("tells an environment with no recorded changes from a server filter that leaves none", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ events: [] })),
    );
    renderChanges();
    expect(
      await screen.findByText("No changes have been recorded in this environment yet."),
    ).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Explanation"), "outside_console");
    expect(
      await screen.findByText("No changes match the explanation and review filters."),
    ).toBeInTheDocument();
  });

  it("reads loading, not no matches, until the first answer arrives", async () => {
    let answer: (response: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => {
      answer = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => pending),
    );
    renderChanges();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/No changes/)).not.toBeInTheDocument();
    await act(async () => {
      answer(Response.json(events));
    });
    expect(await screen.findByText("parameter.work_mem")).toBeInTheDocument();
  });

  it("disables Mark reviewed while its request is in flight, so a double click sends one request", async () => {
    let finishReview: (response: Response) => void = () => {};
    const reviewAnswer = new Promise<Response>((resolve) => {
      finishReview = resolve;
    });
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push(`${init?.method ?? "GET"} ${url}`);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrapAnswer);
        if (url.endsWith("/review")) return reviewAnswer;
        return Response.json({ events: [events.events[0]] });
      }),
    );
    renderChanges();
    const button = await screen.findByRole("button", { name: "Mark reviewed" });
    await userEvent.dblClick(button);
    expect(button).toBeDisabled();
    expect(calls.filter((call) => call.startsWith("POST"))).toHaveLength(1);
    await act(async () => {
      finishReview(Response.json({ ...events.events[0], reviewed_by_operator_name: "phil" }));
    });
  });

  it("enables Mark reviewed again when the review could not be saved", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrapAnswer);
        if (url.endsWith("/review")) return Response.json({ detail: "not allowed" }, { status: 403 });
        return Response.json({ events: [events.events[0]] });
      }),
    );
    renderChanges();
    await userEvent.click(await screen.findByRole("button", { name: "Mark reviewed" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeEnabled();
  });

  it("ignores the answer for an environment that is no longer shown", async () => {
    let answerSandbox: (response: Response) => void = () => {};
    const sandboxAnswer = new Promise<Response>((resolve) => {
      answerSandbox = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("environment_slug=sandbox")
          ? sandboxAnswer
          : Response.json({ events: [{ ...events.events[1], attribute_path: "production.only" }] }),
      ),
    );
    const { rerender } = renderChanges("sandbox");
    rerender(
      <MemoryRouter>
        <ChangesView environmentSlug="production" />
      </MemoryRouter>,
    );
    expect(await screen.findByText("production.only")).toBeInTheDocument();
    await act(async () => {
      answerSandbox(Response.json(events));
    });
    expect(screen.getByText("production.only")).toBeInTheDocument();
    expect(screen.queryByText("deletion_protection")).not.toBeInTheDocument();
  });
});
