import { useId } from "react";
import type { ActionAvailability, ResourceRecord } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { scalar, textMap } from "@/lib/recordValues";
import { cn } from "@/lib/utils";
import { FactsGrid } from "./FactsGrid";
import { ResourceLink } from "./ResourceLink";
import { ResourceSection } from "./ResourceSection";
import type { ResourceDirectory } from "./useResourceDirectory";

interface SnapshotDetailsProps {
  record: ResourceRecord;
  directory: ResourceDirectory;
  /** The page's actions, from the backend; the restore and delete among them are offered in the body too. */
  actions: ActionAvailability[];
  /** Starts the task of `kind` on this snapshot, as the Actions menu does. */
  onStartAction: (kind: string) => void;
}

interface SnapshotActionProps {
  action: ActionAvailability | undefined;
  label: string;
  /** What the action does, under the button. */
  explanation: string;
  destructive?: boolean;
  onStart: (kind: string) => void;
}

/** One action as a button with what it does beneath, or why it cannot run now; nothing when not offered. */
function SnapshotAction({ action, label, explanation, destructive = false, onStart }: SnapshotActionProps) {
  const explanationId = useId();
  if (!action) return null;
  return (
    <div className="flex flex-col items-start gap-1">
      <Button
        variant="outline"
        size="sm"
        disabled={!action.applicable}
        aria-describedby={explanationId}
        onClick={() => onStart(action.kind)}
        className={cn(destructive && "border-status-red/40 text-status-red hover:bg-status-red/10")}
      >
        {label}
      </Button>
      <p id={explanationId} className="text-xs text-muted">
        {action.applicable ? explanation : action.reason}
      </p>
    </div>
  );
}

/**
 * The body of a snapshot page: where it came from, its type, engine version, size, creation time and tags,
 * and what can be done with it: restore it to a new instance (or a new cluster, for a cluster snapshot) and
 * delete it, each disabled with the reason when it cannot run. The same actions are in the Actions menu.
 *
 * @example
 * <SnapshotDetails record={page.record} directory={directory} actions={page.actions} onStartAction={start} />
 */
export function SnapshotDetails({ record, directory, actions, onStartAction }: SnapshotDetailsProps) {
  const facts = record.facts;
  const scope = scalar(facts, "snapshot_scope") === "cluster" ? "cluster" : "instance";
  const sourceName = scalar(record.relationships, "source_identifier");
  const size = scalar(facts, "allocated_storage_gb");
  const tags = Object.entries(textMap(facts, "tags"));
  const restore = actions.find((action) => action.kind === "snapshot_restore");
  const remove = actions.find((action) => action.kind === "snapshot_delete");
  return (
    <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2">
      <ResourceSection title="Snapshot">
        <FactsGrid
          facts={[
            {
              label: "Source",
              value: sourceName ? (
                <ResourceLink
                  target={directory.find(scope, String(sourceName))}
                  identifier={String(sourceName)}
                />
              ) : null,
            },
            { label: "Scope", value: scope },
            { label: "Type", value: scalar(facts, "snapshot_type") },
            { label: "Status", value: <StatusBadge status={record.status} /> },
            { label: "Engine version", value: scalar(facts, "engine_version") },
            { label: "Size", value: size === null ? null : `${size} GB` },
            { label: "Created (UTC)", value: scalar(facts, "created_at_source_utc") },
          ]}
        />
      </ResourceSection>
      <div className="space-y-4">
        {(restore || remove) && (
          <ResourceSection title="Use this snapshot">
            <div className="space-y-3">
              <SnapshotAction
                action={restore}
                label={`Restore to a new ${scope}`}
                explanation={`Creates a new ${scope} from this snapshot; the snapshot and its source are not changed.`}
                onStart={onStartAction}
              />
              <SnapshotAction
                action={remove}
                label="Delete snapshot"
                explanation="Deletes this snapshot for good; you type its name to approve."
                destructive
                onStart={onStartAction}
              />
            </div>
          </ResourceSection>
        )}
        <ResourceSection title="Tags">
          {tags.length === 0 ? (
            <p className="text-sm text-muted">No tags.</p>
          ) : (
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 font-mono text-sm">
              {tags.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="text-muted">{key}</dt>
                  <dd className="min-w-0 break-words">{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </ResourceSection>
      </div>
    </div>
  );
}
