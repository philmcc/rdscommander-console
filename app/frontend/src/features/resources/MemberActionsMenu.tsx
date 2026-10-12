import { useState } from "react";
import { apiGet } from "@/api/client";
import type { ActionAvailability, ResourcePage, ResourceRecord } from "@/api/types";
import { describeError } from "@/lib/describeError";
import { ActionsMenu } from "./ActionsMenu";

interface MemberActionsMenuProps {
  member: ResourceRecord;
  onPick: (member: ResourceRecord, action: ActionAvailability) => void;
}

/** What the menu knows of the member's actions: not asked yet, being read, read, or why they could not be. */
type MemberActions =
  | { state: "unread" }
  | { state: "reading" }
  | { state: "read"; actions: ActionAvailability[] }
  | { state: "failed"; error: string };

/**
 * The Actions menu of a cluster member, for its row on the cluster page: the member's own actions (Remove
 * reader, Reboot, …), each disabled with the backend's reason, read from the member's page the first time the
 * menu opens.
 *
 * @example
 * <MemberActionsMenu member={readerRecord} onPick={(member, action) => start(member, action.kind)} />
 */
export function MemberActionsMenu({ member, onPick }: MemberActionsMenuProps) {
  const [memberActions, setMemberActions] = useState<MemberActions>({ state: "unread" });
  const read = () => {
    setMemberActions({ state: "reading" });
    apiGet<ResourcePage>(`/api/v1/resources/${member.resource_id}`).then(
      (page) => setMemberActions({ state: "read", actions: page.actions }),
      (caught: unknown) => setMemberActions({ state: "failed", error: describeError(caught) }),
    );
  };
  return (
    <ActionsMenu
      actions={memberActions.state === "read" ? memberActions.actions : []}
      onPick={(action) => onPick(member, action)}
      triggerLabel={`Actions for ${member.identifier}`}
      compact
      onOpenChange={(open) => {
        if (open && (memberActions.state === "unread" || memberActions.state === "failed")) read();
      }}
      status={
        memberActions.state === "read"
          ? null
          : memberActions.state === "failed"
            ? `Could not read the actions of ${member.identifier}: ${memberActions.error}`
            : "Loading…"
      }
    />
  );
}
