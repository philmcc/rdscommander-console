import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExecutionRecordsView } from "./ExecutionRecordsView";

afterEach(() => vi.unstubAllGlobals());

function record(id: number, commandLine: string) {
  return {
    id,
    step_name: "plan",
    record_kind: "terraform",
    target_key: "sandbox/0/eu-west-2",
    command_line: commandLine,
    working_directory: "/w",
    api_name: null,
    request_parameters: null,
    response_payload: null,
    standard_output: "ok",
    standard_error: "",
    exit_code: 0,
    succeeded: true,
    error_message: null,
    started_at_utc: "2026-10-10T10:00:00Z",
    duration_milliseconds: 5,
  };
}

describe("ExecutionRecordsView", () => {
  it("searches with filters and renders records", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      Response.json({ records: [record(1, "tofu plan")] }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ExecutionRecordsView environmentSlug="sandbox" />);
    expect(await screen.findByText(/tofu plan/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Text"), "Describe");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    const lastUrl = String(fetchMock.mock.calls.at(-1)?.[0]);
    expect(lastUrl).toContain("environment_slug=sandbox");
    expect(lastUrl).toContain("text=Describe");
  });

  it("opens the export with the same filters in a new tab", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ records: [] })),
    );
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<ExecutionRecordsView environmentSlug="sandbox" />);
    await screen.findByText("No records match.");
    await userEvent.type(screen.getByLabelText("Text"), "Describe");
    await userEvent.click(screen.getByRole("button", { name: "Export JSON" }));
    expect(open).toHaveBeenCalledWith(
      "/api/v1/history/export?environment_slug=sandbox&text=Describe",
      "_blank",
    );
    open.mockRestore();
  });

  it("says why the search failed instead of leaving the page empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ detail: "database is locked" }, { status: 500 })),
    );
    render(<ExecutionRecordsView environmentSlug="sandbox" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not search the records: database is locked",
    );
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
          : Response.json({ records: [record(2, "tofu plan production")] }),
      ),
    );
    const { rerender } = render(<ExecutionRecordsView environmentSlug="sandbox" />);
    rerender(<ExecutionRecordsView environmentSlug="production" />);
    expect(await screen.findByText(/tofu plan production/)).toBeInTheDocument();
    await act(async () => {
      answerSandbox(Response.json({ records: [record(1, "tofu plan sandbox")] }));
    });
    expect(screen.getByText(/tofu plan production/)).toBeInTheDocument();
    expect(screen.queryByText(/tofu plan sandbox/)).not.toBeInTheDocument();
  });
});
