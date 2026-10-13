import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router";
import { apiPost } from "@/api/client";
import type { ActionAvailability, ResourcePage as ResourcePageData, ResourceRecord } from "@/api/types";
import { useEnvironment } from "@/app/EnvironmentContext";
import { StatusBadge } from "@/components/StatusBadge";
import { initialOptionsFor, optionsFromResource } from "@/features/launcher/launchOptions";
import { loadCatalogue } from "@/features/tasks/catalogue";
import { useTask } from "@/features/tasks/TaskContext";
import { describeError } from "@/lib/describeError";
import { ChangeTimeline } from "./ChangeTimeline";
import { ClusterDetails } from "./ClusterDetails";
import { DriftPanel } from "./DriftPanel";
import { InstanceDetails } from "./InstanceDetails";
import { ParameterGroupDetails } from "./ParameterGroupDetails";
import { ResourceHeader } from "./ResourceHeader";
import { ResourceSection } from "./ResourceSection";
import { SnapshotDetails } from "./SnapshotDetails";
import { useResourceDirectory } from "./useResourceDirectory";
import { useResourcePage } from "./useResourcePage";

interface ResourceViewProps {
  page: ResourcePageData;
  reload: () => Promise<void>;
}

/** A loaded resource: header, the body of its kind, drift, recent operations and change history. */
function ResourceView({ page, reload }: ResourceViewProps) {
  const { record } = page;
  const { environments, select } = useEnvironment();
  const { open } = useTask();
  const { hash } = useLocation();
  const directory = useResourceDirectory(record);
  const pendingMaintenance = page.pending_maintenance ?? [];
  const [reviewError, setReviewError] = useState<string | null>(null);

  // The shell shows the environment of the resource being looked at.
  const environmentSlug = record.environment_slug;
  useEffect(() => {
    if (environments.some((environment) => environment.slug === environmentSlug)) select(environmentSlug);
  }, [environmentSlug, environments, select]);

  // A link such as `#history` points into the page, which only exists once it has loaded.
  useEffect(() => {
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView?.();
  }, [hash]);

  const taskOn = (subject: ResourceRecord, kind: string, initialOptions?: Record<string, unknown>) =>
    open({
      kind,
      environmentSlug: subject.environment_slug,
      resourceId: subject.resource_id,
      resourceIdentifier: subject.identifier,
      resourceKind: subject.kind,
      targetKey: subject.target_key,
      initialOptions,
    });
  const taskFor = (kind: string, initialOptions?: Record<string, unknown>) =>
    taskOn(record, kind, initialOptions);
  // An action starts with the options the resource determines, as from the launcher: a hidden
  // `resource_arn` its definition needs (drift resolution) and what its kind takes from a resource
  // (adoption). Without the catalogue only the latter is known; the panel then says why it cannot go on.
  // A cluster member's action (from its row on the cluster page) starts on the member the same way.
  const startActionOn = async (subject: ResourceRecord, kind: string) => {
    let initialOptions = optionsFromResource(kind, subject);
    try {
      const definition = (await loadCatalogue()).find((candidate) => candidate.kind === kind);
      if (definition) initialOptions = initialOptionsFor(definition, subject);
    } catch {
      // The panel reads the catalogue again and shows why it could not.
    }
    taskOn(subject, kind, initialOptions);
  };
  const startAction = (kind: string) => startActionOn(record, kind);
  const review = async (changeEventId: number) => {
    try {
      await apiPost(`/api/v1/history/changes/${changeEventId}/review`);
      setReviewError(null);
      await reload();
    } catch (caught) {
      setReviewError(describeError(caught));
    }
  };

  return (
    <>
      <ResourceHeader
        record={record}
        actions={page.actions}
        onPickAction={(action: ActionAvailability) => void startAction(action.kind)}
      />
      {record.kind === "instance" && (
        <InstanceDetails record={record} directory={directory} pendingMaintenance={pendingMaintenance} />
      )}
      {record.kind === "cluster" && (
        <ClusterDetails
          record={record}
          directory={directory}
          pendingMaintenance={pendingMaintenance}
          onPickMemberAction={(member, action) => void startActionOn(member, action.kind)}
        />
      )}
      {record.kind === "parameter_group" && <ParameterGroupDetails record={record} directory={directory} />}
      {record.kind === "snapshot" && (
        <SnapshotDetails
          record={record}
          directory={directory}
          actions={page.actions}
          onStartAction={(kind) => void startAction(kind)}
        />
      )}
      <div className="mt-4 grid grid-cols-1 items-start gap-4 md:grid-cols-2">
        <ResourceSection title="Drift">
          <DriftPanel
            drift={page.drift ?? []}
            onResolve={(choice) => taskFor("drift_resolve", { resource_arn: record.arn, choice })}
          />
        </ResourceSection>
        <ResourceSection title="Recent operations">
          <ul className="space-y-1 text-sm">
            {page.recent_operations.map((operation) => (
              <li key={operation.id} className="flex items-center gap-2">
                <StatusBadge status={operation.state} />
                <Link to={`/operations/${operation.id}`} className="truncate hover:text-accent">
                  {operation.title}
                </Link>
              </li>
            ))}
            {page.recent_operations.length === 0 && <li className="text-muted">None yet.</li>}
          </ul>
        </ResourceSection>
      </div>
      <ResourceSection title="Change history" id="history" className="mt-4">
        {reviewError && (
          <p role="alert" className="mb-2 text-sm text-status-red">
            Could not mark the change reviewed: {reviewError}
          </p>
        )}
        <ChangeTimeline events={page.change_events ?? []} onReview={review} />
      </ResourceSection>
    </>
  );
}

/**
 * Header, kind-specific body, drift, recent operations and change history of one resource. Actions opens
 * the task panel on this resource, and the page is read again when the panel closes, because the task may
 * have changed the resource.
 */
export default function ResourcePage() {
  const { resourceId } = useParams();
  const { page, reload, error, gone } = useResourcePage(resourceId);
  const { current } = useTask();

  const taskWasOpen = useRef(false);
  useEffect(() => {
    if (current) {
      taskWasOpen.current = true;
    } else if (taskWasOpen.current) {
      taskWasOpen.current = false;
      void reload();
    }
  }, [current, reload]);

  return (
    <>
      {error && gone && (
        <p role="alert" className="mb-4 text-sm">
          This resource is no longer in the inventory: it was deleted, or AWS no longer lists it ({error}).{" "}
          <Link to="/" className="underline">
            Back to the overview
          </Link>
        </p>
      )}
      {error && !gone && (
        <p role="alert" className="mb-4 text-sm text-status-red">
          Could not load this resource: {error}.{" "}
          <Link to="/" className="underline">
            Back to the overview
          </Link>
        </p>
      )}
      {!page && !error && <p className="text-muted">Loading…</p>}
      {page && <ResourceView page={page} reload={reload} />}
    </>
  );
}
