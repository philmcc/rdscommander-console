import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OperationDetail } from "@/api/types";
import { useOperationPolling } from "./useOperationPolling";

function operation(id: string, state: string): OperationDetail {
  return {
    id,
    operation_kind: "inventory_refresh",
    title: `Refresh inventory: ${id}`,
    state,
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
    plan: {},
    step_attempts: [],
    diagnostic_reports: [],
    execution_records: [],
    terraform_runs: [],
    requires_reboot: false,
    downtime: "none",
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useOperationPolling", () => {
  it("reads an active operation again at the interval and stops once it has finished", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const states = ["RUNNING", "RUNNING", "SUCCEEDED"];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      Response.json(operation("op-1", states.shift() ?? "SUCCEEDED")),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useOperationPolling("op-1", 0, 1000));
    await waitFor(() => expect(result.current.operation?.state).toBe("RUNNING"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.operation?.state).toBe("SUCCEEDED");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("reads null as soon as the id changes and ignores the previous operation's late answer", async () => {
    let answerSecond: (response: Response) => void = () => {};
    const secondAnswer = new Promise<Response>((resolve) => {
      answerSecond = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).endsWith("/op-2") ? secondAnswer : Response.json(operation("op-1", "SUCCEEDED")),
      ),
    );
    const { result, rerender } = renderHook(({ id }) => useOperationPolling(id), {
      initialProps: { id: "op-1" },
    });
    await waitFor(() => expect(result.current.operation?.id).toBe("op-1"));
    rerender({ id: "op-2" });
    expect(result.current.operation).toBeNull();
    await act(async () => {
      answerSecond(Response.json(operation("op-2", "SUCCEEDED")));
    });
    expect(result.current.operation?.id).toBe("op-2");
  });

  it("ignores an answer for the operation that is no longer asked for", async () => {
    let answerFirst: (response: Response) => void = () => {};
    const firstAnswer = new Promise<Response>((resolve) => {
      answerFirst = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).endsWith("/op-1") ? firstAnswer : Response.json(operation("op-2", "SUCCEEDED")),
      ),
    );
    const { result, rerender } = renderHook(({ id }) => useOperationPolling(id), {
      initialProps: { id: "op-1" },
    });
    rerender({ id: "op-2" });
    await waitFor(() => expect(result.current.operation?.id).toBe("op-2"));
    await act(async () => {
      answerFirst(Response.json(operation("op-1", "SUCCEEDED")));
    });
    expect(result.current.operation?.id).toBe("op-2");
  });

  it("reports why a first read failed for that operation only, and does not retry it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith("/op-1")
        ? Response.json({ detail: "operation not found" }, { status: 404 })
        : Response.json(operation("op-2", "SUCCEEDED")),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { result, rerender } = renderHook(({ id }) => useOperationPolling(id, 0, 1000), {
      initialProps: { id: "op-1" },
    });
    await waitFor(() => expect(result.current.error).toBe("operation not found"));
    expect(result.current.operation).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    rerender({ id: "op-2" });
    expect(result.current.error).toBeNull();
    await waitFor(() => expect(result.current.operation?.id).toBe("op-2"));
  });

  it("keeps the last operation when a later read fails and reads again while it is active", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const answers = [
      () => Response.json(operation("op-1", "RUNNING")),
      () => Response.json({ detail: "database is locked" }, { status: 500 }),
      () => Response.json(operation("op-1", "SUCCEEDED")),
    ];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      (answers.shift() ?? answers[0] ?? (() => Response.error()))(),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useOperationPolling("op-1", 0, 1000));
    await waitFor(() => expect(result.current.operation?.state).toBe("RUNNING"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.error).toBe("database is locked");
    expect(result.current.operation?.state).toBe("RUNNING");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.error).toBeNull();
    expect(result.current.operation?.state).toBe("SUCCEEDED");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not read again after a failed read once the operation is known to have finished", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let failing = false;
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      failing
        ? Response.json({ detail: "database is locked" }, { status: 500 })
        : Response.json(operation("op-1", "SUCCEEDED")),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { result, rerender } = renderHook(({ version }) => useOperationPolling("op-1", version, 1000), {
      initialProps: { version: 0 },
    });
    await waitFor(() => expect(result.current.operation?.state).toBe("SUCCEEDED"));
    failing = true;
    rerender({ version: 1 });
    await waitFor(() => expect(result.current.error).toBe("database is locked"));
    expect(result.current.operation?.state).toBe("SUCCEEDED");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("reads nothing without an id", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useOperationPolling(null));
    expect(result.current).toEqual({ operation: null, error: null });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
