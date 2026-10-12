import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OperationSummary } from "@/api/types";
import { useOperations } from "./useOperations";

function operation(id: string, title: string): OperationSummary {
  return {
    id,
    operation_kind: "inventory_refresh",
    title,
    state: "SUCCEEDED",
    environment_slug: "sandbox",
    target_key: null,
    resource_arn: null,
    resource_id: null,
    approval_required_text: null,
    created_by_operator_name: "phil",
    approved_by_operator_name: null,
    failure_summary: null,
    created_at_utc: "2026-10-10T10:00:00Z",
    updated_at_utc: "2026-10-10T10:00:00Z",
    finished_at_utc: null,
    progress: null,
  };
}

const titles = (operations: OperationSummary[]) => operations.map((entry) => entry.title);

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useOperations", () => {
  it("asks for the environment's operations in the given states and reloads every five seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () => Response.json({ operations: [operation("1", "first")] }));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useOperations("sandbox", "FAILED"));
    await waitFor(() => expect(titles(result.current.operations)).toEqual(["first"]));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/operations?environment_slug=sandbox&state=FAILED&limit=100",
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("leaves the state out of the request when no state is asked for", async () => {
    const fetchMock = vi.fn(async () => Response.json({ operations: [] }));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useOperations("sandbox", ""));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/operations?environment_slug=sandbox&limit=100");
  });

  it("drops the previous environment's rows at once and ignores its late answer", async () => {
    let answerSandbox: (response: Response) => void = () => {};
    const sandboxAnswer = new Promise<Response>((resolve) => {
      answerSandbox = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("environment_slug=sandbox")
          ? sandboxAnswer
          : Response.json({ operations: [operation("2", "production row")] }),
      ),
    );
    const { result, rerender } = renderHook(({ slug }) => useOperations(slug, ""), {
      initialProps: { slug: "sandbox" },
    });
    rerender({ slug: "production" });
    expect(result.current.operations).toEqual([]);
    await waitFor(() => expect(titles(result.current.operations)).toEqual(["production row"]));
    await act(async () => {
      answerSandbox(Response.json({ operations: [operation("1", "sandbox row")] }));
    });
    expect(titles(result.current.operations)).toEqual(["production row"]);
    expect(result.current.loading).toBe(false);
  });

  it("ignores the late answer of the previous state filter", async () => {
    let answerAll: (response: Response) => void = () => {};
    const allAnswer = new Promise<Response>((resolve) => {
      answerAll = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("state=FAILED")
          ? Response.json({ operations: [operation("2", "failed row")] })
          : allAnswer,
      ),
    );
    const { result, rerender } = renderHook(({ states }) => useOperations("sandbox", states), {
      initialProps: { states: "" },
    });
    rerender({ states: "FAILED" });
    await waitFor(() => expect(titles(result.current.operations)).toEqual(["failed row"]));
    await act(async () => {
      answerAll(Response.json({ operations: [operation("1", "any row")] }));
    });
    expect(titles(result.current.operations)).toEqual(["failed row"]);
  });

  it("reports a failed load, keeps the rows it has, and clears the report when a load works", async () => {
    let failing = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        failing
          ? Response.json({ detail: "database is locked" }, { status: 500 })
          : Response.json({ operations: [operation("1", "kept")] }),
      ),
    );
    const { result } = renderHook(() => useOperations("sandbox", ""));
    await waitFor(() => expect(titles(result.current.operations)).toEqual(["kept"]));
    failing = true;
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.error).toBe("database is locked");
    expect(titles(result.current.operations)).toEqual(["kept"]);
    failing = false;
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.error).toBeNull();
  });
});
