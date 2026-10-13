import { describe, expect, it, vi } from "vitest";
import type { AttentionItem } from "@/api/types";
import { openForAttentionItem } from "./TaskContext";

function item(overrides: Partial<AttentionItem>): AttentionItem {
  return {
    kind: "drift",
    title: "t",
    detail: "d",
    environment_slug: "sandbox",
    operation_id: null,
    resource_id: null,
    resource_identifier: null,
    action_kind: null,
    action_options: {},
    ...overrides,
  };
}

function resolve(row: AttentionItem) {
  const open = vi.fn();
  const navigate = vi.fn();
  openForAttentionItem(row, open, navigate);
  return { open, navigate };
}

describe("openForAttentionItem", () => {
  it("opens an operation awaiting approval on its review", () => {
    const { open, navigate } = resolve(
      item({
        kind: "awaiting_approval",
        operation_id: "op-1",
        resource_id: "r1",
        resource_identifier: "demo",
      }),
    );
    expect(open).toHaveBeenCalledWith({ kind: "", operationId: "op-1", environmentSlug: "sandbox" });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("opens the resolving task with the resource and the ready-made options", () => {
    const { open, navigate } = resolve(
      item({
        kind: "unmanaged",
        resource_id: "r2",
        resource_identifier: "demo-aurora",
        action_kind: "terraform_adoption",
        action_options: { target_key: "t", resource_arns: ["arn:c"] },
      }),
    );
    expect(open).toHaveBeenCalledWith({
      kind: "terraform_adoption",
      environmentSlug: "sandbox",
      resourceId: "r2",
      resourceIdentifier: "demo-aurora",
      initialOptions: { target_key: "t", resource_arns: ["arn:c"] },
    });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("opens the resolving task without a resource when the row has no page", () => {
    const { open } = resolve(
      item({
        resource_identifier: "demo-subnets",
        action_kind: "drift_resolve",
        action_options: { resource_arn: "arn:s", choice: "accept" },
      }),
    );
    expect(open).toHaveBeenCalledWith({
      kind: "drift_resolve",
      environmentSlug: "sandbox",
      resourceId: undefined,
      resourceIdentifier: "demo-subnets",
      initialOptions: { resource_arn: "arn:s", choice: "accept" },
    });
  });

  it("sends an outside change to the resource's history", () => {
    const { open, navigate } = resolve(item({ kind: "outside_change", resource_id: "r3" }));
    expect(navigate).toHaveBeenCalledWith("/resources/r3#history");
    expect(open).not.toHaveBeenCalled();
  });

  it("sends pending maintenance and foreign-owned rows to the resource page", () => {
    expect(resolve(item({ kind: "pending_maintenance", resource_id: "r4" })).navigate).toHaveBeenCalledWith(
      "/resources/r4",
    );
    expect(resolve(item({ kind: "foreign_owned", resource_id: "r5" })).navigate).toHaveBeenCalledWith(
      "/resources/r5",
    );
  });

  it("sends a failed operation to the operation, even when it is about a resource", () => {
    const { navigate } = resolve(item({ kind: "failed_recent", operation_id: "op-9", resource_id: "r6" }));
    expect(navigate).toHaveBeenCalledWith("/operations/op-9");
  });

  it("does nothing for a row that points nowhere", () => {
    const { open, navigate } = resolve(item({ kind: "pending_maintenance" }));
    expect(open).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });
});
