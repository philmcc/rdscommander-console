import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AttentionInbox } from "@/api/types";
import { useAttention } from "./useAttention";

function inboxWith(title: string): AttentionInbox {
  return {
    awaiting_approval: [],
    pending_reboot: [],
    pending_maintenance: [
      {
        kind: "pending_maintenance",
        title,
        detail: "",
        environment_slug: "x",
        operation_id: null,
        resource_id: null,
        resource_identifier: null,
        action_kind: null,
        action_options: {},
      },
    ],
    drift: [],
    outside_changes: [],
    failed_recent: [],
    foreign_owned: [],
    unmanaged: [],
    blue_side: [],
  };
}

/** The title of the inbox's first pending-maintenance row, which is how these tests tell inboxes apart. */
function firstTitle(inbox: AttentionInbox | null): string | undefined {
  return inbox?.pending_maintenance[0]?.title;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useAttention", () => {
  it("asks for the environment's inbox and reloads every fifteen seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn(async () => Response.json(inboxWith("first")));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useAttention("sandbox"));
    await waitFor(() => expect(firstTitle(result.current.inbox)).toBe("first"));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/attention?environment_slug=sandbox");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
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
          : Response.json(inboxWith("production row")),
      ),
    );
    const { result, rerender } = renderHook(({ slug }) => useAttention(slug), {
      initialProps: { slug: "sandbox" },
    });
    rerender({ slug: "production" });
    await waitFor(() => expect(firstTitle(result.current.inbox)).toBe("production row"));
    await act(async () => {
      answerSandbox(Response.json(inboxWith("sandbox row")));
    });
    expect(firstTitle(result.current.inbox)).toBe("production row");
  });

  it("reports a failed load and clears the report when the next load works", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("boom", { status: 500 }))
      .mockResolvedValue(Response.json(inboxWith("ok")));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useAttention("sandbox"));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.inbox).toBeNull();
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.error).toBeNull();
    expect(firstTitle(result.current.inbox)).toBe("ok");
  });

  it("asks for nothing without an environment", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useAttention(undefined));
    expect(result.current.inbox).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
