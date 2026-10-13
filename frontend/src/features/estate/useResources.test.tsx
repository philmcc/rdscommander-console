import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ResourceRecord } from "@/api/types";
import { useResources } from "./useResources";

function record(identifier: string, environmentSlug: string): ResourceRecord {
  return {
    kind: "instance",
    arn: `arn:${environmentSlug}:${identifier}`,
    identifier,
    environment_slug: environmentSlug,
    target_key: "t",
    ownership: "unmanaged",
    status: "available",
    facts: {},
    relationships: {},
    raw: {},
    snapshot_id: 1,
    terraform_address: null,
    parameter_apply_status: null,
    resource_id: `id-${identifier}`,
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

/** Answers each environment's resource list; an environment with a gate answers only once it opens. */
function serve(
  lists: Record<string, ResourceRecord[]>,
  gates: Record<string, Promise<void>> = {},
  failing: Set<string> = new Set(),
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const slug = String(input).match(/\/environments\/([^/]+)\/resources/)?.[1] ?? "";
      await gates[slug];
      if (failing.has(slug)) return Response.json({ detail: "database is locked" }, { status: 500 });
      return Response.json({ resources: lists[slug] });
    }),
  );
}

function renderResources(initialSlug: string | undefined) {
  return renderHook(({ slug }) => useResources(slug, "instance"), { initialProps: { slug: initialSlug } });
}

/** Lets answers that are already on their way reach the hook. */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
}

const identifiers = (resources: ResourceRecord[]) => resources.map((resource) => resource.identifier);

afterEach(() => vi.unstubAllGlobals());

describe("useResources", () => {
  it("loads the list of one environment", async () => {
    serve({ sandbox: [record("sandbox-orders", "sandbox")] });
    const { result } = renderResources("sandbox");
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(identifiers(result.current.resources)).toEqual(["sandbox-orders"]));
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("keeps the new environment's list when the previous environment's answer arrives late", async () => {
    const sandboxGate = gate();
    serve(
      { sandbox: [record("sandbox-orders", "sandbox")], production: [record("prod-orders", "production")] },
      { sandbox: sandboxGate.opened },
    );
    const { result, rerender } = renderResources("sandbox");
    rerender({ slug: "production" });
    await waitFor(() => expect(identifiers(result.current.resources)).toEqual(["prod-orders"]));
    expect(result.current.loading).toBe(false);

    sandboxGate.open();
    await settle();

    expect(identifiers(result.current.resources)).toEqual(["prod-orders"]);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("does not let a late failure of the previous environment replace the new environment's list with an error", async () => {
    const sandboxGate = gate();
    serve(
      { sandbox: [], production: [record("prod-orders", "production")] },
      { sandbox: sandboxGate.opened },
      new Set(["sandbox"]),
    );
    const { result, rerender } = renderResources("sandbox");
    rerender({ slug: "production" });
    await waitFor(() => expect(identifiers(result.current.resources)).toEqual(["prod-orders"]));

    sandboxGate.open();
    await settle();

    expect(identifiers(result.current.resources)).toEqual(["prod-orders"]);
    expect(result.current.error).toBeNull();
  });

  it("drops the previous environment's list at once and reads loading until the new one answers", async () => {
    const productionGate = gate();
    serve(
      { sandbox: [record("sandbox-orders", "sandbox")], production: [record("prod-orders", "production")] },
      { production: productionGate.opened },
    );
    const { result, rerender } = renderResources("sandbox");
    await waitFor(() => expect(identifiers(result.current.resources)).toEqual(["sandbox-orders"]));

    rerender({ slug: "production" });
    expect(result.current.resources).toEqual([]);
    expect(result.current.loading).toBe(true);

    productionGate.open();
    await waitFor(() => expect(identifiers(result.current.resources)).toEqual(["prod-orders"]));
    expect(result.current.loading).toBe(false);
  });

  it("reports a failed load and keeps the rows it already has when a reload fails", async () => {
    const failing = new Set<string>();
    serve({ sandbox: [record("sandbox-orders", "sandbox")] }, {}, failing);
    const { result } = renderResources("sandbox");
    await waitFor(() => expect(identifiers(result.current.resources)).toEqual(["sandbox-orders"]));

    failing.add("sandbox");
    await act(async () => {
      await result.current.reload();
    });

    expect(result.current.error).toBe("database is locked");
    expect(identifiers(result.current.resources)).toEqual(["sandbox-orders"]);
    expect(result.current.loading).toBe(false);
  });
});
