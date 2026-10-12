import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ActionAvailability } from "@/api/types";
import { ActionsMenu } from "./ActionsMenu";

function action(overrides: Partial<ActionAvailability>): ActionAvailability {
  return {
    kind: "reboot_instance",
    title: "Reboot instance",
    group: "Instance lifecycle",
    summary: "",
    destructive: false,
    disruptive: false,
    applicable: true,
    reason: "",
    ...overrides,
  };
}

const actions = [
  action({ disruptive: true }),
  action({ kind: "instance_delete", title: "Delete instance", destructive: true, disruptive: true }),
  action({
    kind: "snapshot_create",
    title: "Create snapshot",
    group: "Backups",
    applicable: false,
    reason: "the instance is not available",
  }),
];

describe("ActionsMenu", () => {
  it("lists every entry under its group, with the reason of an entry that cannot run", async () => {
    render(<ActionsMenu actions={actions} onPick={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    expect(await screen.findByText("Instance lifecycle")).toBeInTheDocument();
    expect(screen.getByText("Backups")).toBeInTheDocument();
    const blocked = screen.getByRole("menuitem", { name: /Create snapshot/ });
    expect(blocked).toHaveAttribute("aria-disabled", "true");
    expect(blocked).toHaveAttribute("title", "the instance is not available");
    expect(within(blocked).getByText("the instance is not available")).toBeInTheDocument();
  });

  it("writes destructive entries in red and marks disruptive entries", async () => {
    render(<ActionsMenu actions={actions} onPick={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    const reboot = await screen.findByRole("menuitem", { name: /Reboot instance/ });
    expect(within(reboot).getByText("disruptive")).toBeInTheDocument();
    expect(reboot).toHaveAccessibleName("Reboot instance disruptive");
    expect(within(reboot).getByText("Reboot instance")).not.toHaveClass("text-status-red");
    const deletion = screen.getByRole("menuitem", { name: /Delete instance/ });
    expect(within(deletion).getByText("Delete instance")).toHaveClass("text-status-red");
    expect(within(deletion).getByText("disruptive")).toBeInTheDocument();
    expect(deletion).toHaveAccessibleName(/destructive/);
    expect(screen.getByRole("menuitem", { name: /Create snapshot/ })).not.toHaveTextContent("disruptive");
  });

  it("hands the picked entry to the page and ignores an entry that cannot run", async () => {
    const onPick = vi.fn();
    render(<ActionsMenu actions={actions} onPick={onPick} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Create snapshot/ }));
    expect(onPick).not.toHaveBeenCalled();
    // A disabled entry leaves the menu open.
    await userEvent.click(screen.getByRole("menuitem", { name: /Delete instance/ }));
    expect(onPick).toHaveBeenCalledExactlyOnceWith(actions[1]);
  });

  it("says so when no entry applies to the kind of resource", async () => {
    render(<ActionsMenu actions={[]} onPick={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    expect(await screen.findByText("No actions are defined for this resource.")).toBeInTheDocument();
  });
});
