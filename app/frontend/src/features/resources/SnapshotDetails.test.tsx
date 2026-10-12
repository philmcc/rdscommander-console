import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { ActionAvailability, ResourceRecord } from "@/api/types";
import { SnapshotDetails } from "./SnapshotDetails";
import type { ResourceDirectory } from "./useResourceDirectory";

const emptyDirectory: ResourceDirectory = {
  loaded: true,
  error: null,
  find: () => undefined,
  snapshotsOf: () => [],
};

function snapshot(scope: "instance" | "cluster", snapshotType: string): ResourceRecord {
  return {
    kind: "snapshot",
    arn: "arn:snap",
    identifier: "orders-before-change",
    environment_slug: "sandbox",
    target_key: "t",
    ownership: "unmanaged",
    status: "available",
    facts: { snapshot_scope: scope, snapshot_type: snapshotType, engine_version: "16.4" },
    relationships: { source_identifier: "demo-orders" },
    raw: {},
    snapshot_id: 1,
    terraform_address: null,
    parameter_apply_status: null,
    resource_id: "YXJuOnNuYXA",
  };
}

function action(kind: string, title: string, applicable: boolean, reason = ""): ActionAvailability {
  return {
    kind,
    title,
    group: "Snapshots",
    summary: "",
    destructive: kind === "snapshot_delete",
    disruptive: false,
    applicable,
    reason,
  };
}

function renderDetails(record: ResourceRecord, actions: ActionAvailability[]) {
  const onStartAction = vi.fn();
  render(
    <MemoryRouter>
      <SnapshotDetails
        record={record}
        directory={emptyDirectory}
        actions={actions}
        onStartAction={onStartAction}
      />
    </MemoryRouter>,
  );
  return onStartAction;
}

describe("SnapshotDetails actions", () => {
  it("restores a manual instance snapshot to a new instance and deletes it", async () => {
    const onStartAction = renderDetails(snapshot("instance", "manual"), [
      action("snapshot_restore", "Restore to new instance or cluster", true),
      action("snapshot_delete", "Delete snapshot", true),
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Restore to a new instance" }));
    expect(onStartAction).toHaveBeenLastCalledWith("snapshot_restore");
    await userEvent.click(screen.getByRole("button", { name: "Delete snapshot" }));
    expect(onStartAction).toHaveBeenLastCalledWith("snapshot_delete");
  });

  it("restores a cluster snapshot to a new cluster", () => {
    renderDetails(snapshot("cluster", "manual"), [
      action("snapshot_restore", "Restore to new instance or cluster", true),
    ]);
    expect(screen.getByRole("button", { name: "Restore to a new cluster" })).toBeEnabled();
  });

  it("shows Delete disabled with the reason for an automated snapshot", () => {
    const reason = "AWS deletes automated snapshots itself when they expire";
    renderDetails(snapshot("instance", "automated"), [
      action("snapshot_restore", "Restore to new instance or cluster", true),
      action("snapshot_delete", "Delete snapshot", false, reason),
    ]);
    const deleteButton = screen.getByRole("button", { name: "Delete snapshot" });
    expect(deleteButton).toBeDisabled();
    expect(deleteButton).toHaveAccessibleDescription(reason);
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it("leaves out an action the backend does not offer", () => {
    renderDetails(snapshot("instance", "manual"), []);
    expect(screen.queryByRole("button", { name: /Restore/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete snapshot" })).toBeNull();
  });
});
