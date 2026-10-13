import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it.each([
    ["available", "text-status-green"],
    ["in-sync", "text-status-green"],
    ["SUCCEEDED", "text-status-green"],
    ["adopted", "text-status-green"],
    ["completed", "text-status-green"],
    ["pending-reboot", "text-status-amber"],
    ["AWAITING_APPROVAL", "text-status-amber"],
    ["modifying", "text-status-amber"],
    ["applying", "text-status-amber"],
    ["backing-up", "text-status-amber"],
    ["pending", "text-status-amber"],
    ["FAILED", "text-status-red"],
    ["CANCELLED", "text-status-red"],
    ["deleting", "text-status-red"],
    ["failed", "text-status-red"],
    ["refused", "text-status-red"],
    ["blocked", "text-status-red"],
    ["RUNNING", "text-status-blue"],
    ["QUEUED", "text-status-blue"],
    ["creating", "text-status-blue"],
    ["rebooting", "text-status-blue"],
    // How the upgrade builder classified an override.
    ["carried", "text-status-green"],
    ["default_changed", "text-status-blue"],
    ["renamed", "text-status-blue"],
    ["needs_review", "text-status-amber"],
    ["removed", "text-status-red"],
    // RDS statuses the lifecycle operations pass through or leave behind.
    ["stopped", "text-status-amber"],
    ["stopping", "text-status-amber"],
    ["starting", "text-status-amber"],
    ["restoring", "text-status-amber"],
    ["failing-over", "text-status-amber"],
    ["renaming", "text-status-amber"],
    ["storage-optimization", "text-status-amber"],
    ["backing-up", "text-status-amber"],
    ["incompatible-parameters", "text-status-red"],
    ["incompatible-network", "text-status-red"],
    ["incompatible-restore", "text-status-red"],
    ["inaccessible-encryption-credentials", "text-status-red"],
    ["storage-full", "text-status-red"],
    ["something-new", "text-muted"],
  ])("renders %s in the %s colour", (status, colourClass) => {
    render(<StatusBadge status={status} />);
    const badge = screen.getByText(status);
    expect(badge.className).toContain(colourClass);
    expect(badge.className).toContain("font-mono");
  });

  it("renders a neutral unknown badge when the status is missing", () => {
    render(<StatusBadge status={null} />);
    expect(screen.getByText("unknown").className).toContain("text-muted");
  });
});
