import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEnvironmentPage } from "./useEnvironmentPage";

function page(slug: string) {
  return {
    overview: { slug },
    targets: [],
    actions: [],
    recent_operations: [],
    change_events: [],
    drift: [],
  };
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

describe("useEnvironmentPage", () => {
  it("reads the page of the environment and reads it again on reload", async () => {
    let refreshMinutes = 15;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          ...page("sandbox"),
          overview: { slug: "sandbox", inventory_refresh_minutes: refreshMinutes },
        }),
      ),
    );
    const { result } = renderHook(() => useEnvironmentPage("sandbox"));
    expect(result.current.page).toBeNull();
    await waitFor(() => expect(result.current.page?.overview.inventory_refresh_minutes).toBe(15));
    refreshMinutes = 30;
    await act(() => result.current.reload());
    expect(result.current.page?.overview.inventory_refresh_minutes).toBe(30);
    expect(result.current.error).toBeNull();
  });

  it("drops the page of the previous environment at once and ignores its late answer", async () => {
    const slowFirst = gate();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const slug = String(input).split("/").at(-1) ?? "";
        if (slug === "sandbox") await slowFirst.opened;
        return Response.json(page(slug));
      }),
    );
    const { result, rerender } = renderHook(({ slug }) => useEnvironmentPage(slug), {
      initialProps: { slug: "sandbox" },
    });
    rerender({ slug: "production" });
    expect(result.current.page).toBeNull();
    await waitFor(() => expect(result.current.page?.overview.slug).toBe("production"));
    slowFirst.open();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    expect(result.current.page?.overview.slug).toBe("production");
  });

  it("keeps the page it has when a reload fails and says why", async () => {
    let failing = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        failing
          ? Response.json({ detail: "the control database is down" }, { status: 500 })
          : Response.json(page("sandbox")),
      ),
    );
    const { result } = renderHook(() => useEnvironmentPage("sandbox"));
    await waitFor(() => expect(result.current.page).not.toBeNull());
    failing = true;
    await act(() => result.current.reload());
    expect(result.current.page?.overview.slug).toBe("sandbox");
    expect(result.current.error).toBe("the control database is down");
  });

  it("does not read anything without an environment", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useEnvironmentPage(undefined));
    expect(result.current.page).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
