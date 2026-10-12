import { describe, expect, it } from "vitest";
import type { DefinitionSummary } from "@/api/types";
import { approvalPauseOf, REVIEW_SUBMIT_LABEL } from "./approvalPause";

function definition(kind: string, pausesForApproval: boolean, properties: Record<string, unknown> = {}) {
  return {
    kind,
    title: kind,
    group: "g",
    summary: "",
    applies_to: ["environment"],
    modes: ["terraform", "direct"],
    destructive: false,
    disruptive: false,
    needs_target: false,
    pauses_for_approval: pausesForApproval,
    pauses_for_approval_when: null,
    options_schema: { type: "object", properties },
  } as DefinitionSummary;
}

/** The lifecycle operations; every one pauses for review and approval. */
const LIFECYCLE_KINDS = [
  "instance_class_change",
  "storage_change",
  "multi_az_change",
  "backup_settings_change",
  "maintenance_window_change",
  "monitoring_change",
  "deletion_protection_change",
  "serverless_capacity_change",
  "tags_change",
  "reboot_instance",
  "cluster_failover",
  "apply_pending_maintenance",
  "snapshot_take",
  "snapshot_delete",
  "replica_promote",
  "replica_create",
  "cluster_reader_add",
  "cluster_reader_remove",
  "snapshot_restore",
  "pitr_restore",
  "instance_create",
  "cluster_create",
  "decommission",
];

describe("approvalPauseOf", () => {
  it.each([
    ["inventory_refresh", "Refresh"],
    ["drift_detect", "Detect drift"],
    ["environment_import", "Import environment"],
    ["some_future_kind", "Run"],
  ])("labels %s, which runs to the end, %s", (kind, label) => {
    expect(approvalPauseOf(definition(kind, false), {})).toEqual({ pauses: false, submitLabel: label });
  });

  it("follows drift resolution's chosen value", () => {
    const driftResolve = {
      ...definition("drift_resolve", true, {
        choice: {
          type: "string",
          enum: ["accept", "restore"],
          ui: { labels: { accept: "Accept into the record", restore: "Restore from the record" } },
        },
      }),
      pauses_for_approval_when: { accept: false, restore: true },
    } as DefinitionSummary;
    expect(approvalPauseOf(driftResolve, { choice: "accept" })).toEqual({
      pauses: false,
      submitLabel: "Accept into the record",
    });
    expect(approvalPauseOf(driftResolve, { choice: "restore" })).toEqual({
      pauses: true,
      submitLabel: REVIEW_SUBMIT_LABEL,
    });
  });

  it.each(LIFECYCLE_KINDS)("has %s continue to review, since it pauses for approval", (kind) => {
    expect(approvalPauseOf(definition(kind, true), {})).toEqual({
      pauses: true,
      submitLabel: "Continue to review",
    });
  });
});
