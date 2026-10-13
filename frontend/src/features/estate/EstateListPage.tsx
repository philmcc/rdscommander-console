import { type ReactNode, useEffect, useRef } from "react";
import type { ResourceRecord } from "@/api/types";
import { useEnvironment } from "@/app/EnvironmentContext";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { useTask } from "@/features/tasks/TaskContext";
import { ResourceTable } from "./ResourceTable";
import { useResources } from "./useResources";

interface EstateListPageProps {
  /** Resource kind to list: `instance`, `cluster`, `parameter_group` or `snapshot`. */
  kind: string;
  /** Page title, also used in the empty-state sentence. */
  title: string;
  /** Catalogue kind of the task the Create button opens; no button when omitted. */
  createKind?: string;
  /** Words of the Create button, such as "Create instance"; "Create" when omitted. */
  createLabel?: string;
  /** Content between the header and the list, given the resources loaded so far (none until they arrive). */
  aboveList?: (resources: ResourceRecord[]) => ReactNode;
}

/**
 * One estate list: a resource kind in the selected environment, with an optional Create task and an empty
 * state that offers an inventory refresh. The list is read again when the task panel closes.
 *
 * @example
 * <EstateListPage kind="instance" title="Instances" createKind="instance_create" createLabel="Create instance" />
 */
export function EstateListPage({ kind, title, createKind, createLabel, aboveList }: EstateListPageProps) {
  const { selected } = useEnvironment();
  const { open, current } = useTask();
  const { resources, loading, error, reload } = useResources(selected?.slug, kind);

  // A task may have changed the estate (a refresh, a create), so read the list when its panel closes.
  const taskWasOpen = useRef(false);
  useEffect(() => {
    if (current) {
      taskWasOpen.current = true;
    } else if (taskWasOpen.current) {
      taskWasOpen.current = false;
      void reload();
    }
  }, [current, reload]);

  const header = (
    <PageHeader
      breadcrumb={selected ? [{ label: selected.display_name }, { label: "Estate" }] : [{ label: "Estate" }]}
      title={title}
      actions={
        createKind && selected ? (
          <Button onClick={() => open({ kind: createKind, environmentSlug: selected.slug })}>
            {createLabel ?? "Create"}
          </Button>
        ) : undefined
      }
    />
  );
  if (!selected) {
    return (
      <>
        {header}
        <p className="text-muted">Add an environment to begin.</p>
      </>
    );
  }
  return (
    <>
      {header}
      {aboveList?.(resources)}
      {error && (
        <p role="alert" className="mb-4 text-sm text-status-red">
          Could not load {title.toLowerCase()}: {error}
        </p>
      )}
      {loading && resources.length === 0 && !error && <p className="text-sm text-muted">Loading…</p>}
      {!loading && !error && resources.length === 0 && (
        <EmptyState
          icon="○"
          title={`No ${title.toLowerCase()} in the latest inventory.`}
          action={
            <Button
              variant="outline"
              onClick={() => open({ kind: "inventory_refresh", environmentSlug: selected.slug })}
            >
              Refresh inventory
            </Button>
          }
        />
      )}
      {resources.length > 0 && <ResourceTable kind={kind} resources={resources} />}
    </>
  );
}
