import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useResourcePage } from "./useResourcePage";

function page(identifier: string) {
  return { record: { identifier }, actions: [], recent_operations: [] };
}

/** A gate that a test opens when the held answer should arrive. */
function gate() {
  let open = () => {};
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { opened, open };
}

afterEach(() => vi.unstubAllGlobals());

describe("useResourcePage", () => {
  it("reads the page of the resource and reads it again on reload", async () => {
    let identifier = "first";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json(page(identifier))),
    );
    const { result } = renderHook(() => useResourcePage("a"));
    expect(result.current.page).toBeNull();
    await waitFor(() => expect(result.current.page?.record.identifier).toBe("first"));
    identifier = "second";
    await act(() => result.current.reload());
    expect(result.current.page?.record.identifier).toBe("second");
    expect(result.current.error).toBeNull();
  });

  it("drops the page of the previous resource at once and ignores its late answer", async () => {
    const slowFirst = gate();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/a")) await slowFirst.opened;
        return Response.json(page(String(input).endsWith("/a") ? "a" : "b"));
      }),
    );
    const { result, rerender } = renderHook(({ id }) => useResourcePage(id), { initialProps: { id: "a" } });
    rerender({ id: "b" });
    expect(result.current.page).toBeNull();
    await waitFor(() => expect(result.current.page?.record.identifier).toBe("b"));
    slowFirst.open();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    expect(result.current.page?.record.identifier).toBe("b");
  });

  it("keeps the page it has when a reload fails and says why", async () => {
    let failing = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        failing
          ? Response.json({ detail: "the database is busy" }, { status: 503 })
          : Response.json(page("a")),
      ),
    );
    const { result } = renderHook(() => useResourcePage("a"));
    await waitFor(() => expect(result.current.page).not.toBeNull());
    failing = true;
    await act(() => result.current.reload());
    expect(result.current.error).toBe("the database is busy");
    expect(result.current.page?.record.identifier).toBe("a");
  });

  it("drops the page when a reload finds the resource gone, so a deleted resource is not shown as it was", async () => {
    // After Delete snapshot the page kept showing the snapshot as available, with Restore and Delete, under
    // the "not in the latest inventory" error.
    let gone = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        gone
          ? Response.json({ detail: "resource arn:x is not in the latest inventory" }, { status: 404 })
          : Response.json(page("a")),
      ),
    );
    const { result } = renderHook(() => useResourcePage("a"));
    await waitFor(() => expect(result.current.page).not.toBeNull());
    gone = true;
    await act(() => result.current.reload());
    expect(result.current.page).toBeNull();
    expect(result.current.gone).toBe(true);
  });

  it("reads nothing without a resource id", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useResourcePage(undefined));
    expect(result.current.page).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
