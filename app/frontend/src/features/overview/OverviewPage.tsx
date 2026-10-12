import { type ReactNode, useEffect, useState } from "react";
import { Link } from "react-router";
import { apiGet } from "@/api/client";
import type { EnvironmentPageData, OperationList, OperationSummary } from "@/api/types";
import { useEnvironment } from "@/app/EnvironmentContext";
import { Checklist } from "@/components/Checklist";
import { OwnershipBadge } from "@/components/OwnershipBadge";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { OperationProgressText } from "@/features/operations/OperationProgressText";
import { useTask } from "@/features/tasks/TaskContext";
import { describeError } from "@/lib/describeError";
import { cn } from "@/lib/utils";

/** Resource kinds in the order the count cards show them, each with its label and list page. */
const KINDS: { kind: string; label: string; to: string }[] = [
  { kind: "instance", label: "Instances", to: "/estate/instances" },
  { kind: "cluster", label: "Clusters", to: "/estate/clusters" },
  { kind: "parameter_group", label: "Parameter groups", to: "/estate/parameter-groups" },
  { kind: "snapshot", label: "Snapshots", to: "/estate/snapshots" },
];
/** The operation states that count as running now, as the operations list's `state` filter takes them. */
const RUNNING_STATES = "QUEUED,RUNNING,AWAITING_APPROVAL";
/** How often the page is read again, so running operations and counts stay current. */
const RELOAD_INTERVAL_MILLISECONDS = 5000;

/** An environment's page together with the operations in progress in it. */
interface OverviewData {
  page: EnvironmentPageData;
  running: OperationSummary[];
}

/**
 * An environment's overview data, read again every five seconds.
 *
 * The operations in progress come from the operations list, which answers for every one of them; the
 * page's `recent_operations` is only the latest few. `data` is `null` until the first answer for this
 * environment (switching environment drops the previous one's data at once, and an answer that arrives
 * after the switch is ignored); `error` says why the latest load failed and is `null` when it worked.
 */
function useOverviewData(environmentSlug: string | undefined) {
  const [data, setData] = useState<OverviewData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null);
    setError(null);
    if (!environmentSlug) return;
    let cancelled = false;
    const load = async () => {
      try {
        const [page, active] = await Promise.all([
          apiGet<EnvironmentPageData>(`/api/v1/environments/${environmentSlug}`),
          apiGet<OperationList>("/api/v1/operations", {
            environment_slug: environmentSlug,
            state: RUNNING_STATES,
          }),
        ]);
        if (cancelled) return;
        setData({ page, running: active.operations });
        setError(null);
      } catch (caught) {
        if (!cancelled) setError(describeError(caught));
      }
    };
    void load();
    const timer = setInterval(() => void load(), RELOAD_INTERVAL_MILLISECONDS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [environmentSlug]);
  return { data, error };
}

/** The link text for the number of inbox rows. */
function attentionSummary(count: number): string {
  if (count === 0) return "Nothing needs attention";
  return count === 1 ? "1 item needs attention" : `${count} items need attention`;
}

/** A titled box on the overview. */
function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-hairline bg-surface p-4">
      <h2 className="mb-2 text-sm font-medium">{title}</h2>
      {children}
    </section>
  );
}

/** One operation in progress: its state, title, the step it is on and a determinate bar. */
function RunningOperationRow({ operation }: { operation: OperationSummary }) {
  const { progress } = operation;
  return (
    <li className="flex h-9 items-center gap-3 border-b border-hairline px-4 text-sm last:border-0">
      <StatusBadge status={operation.state} />
      <Link to={`/operations/${operation.id}`} className="truncate hover:text-accent">
        {operation.title}
      </Link>
      {progress && (
        <>
          <span className="ml-auto shrink-0 text-xs text-muted">
            <OperationProgressText progress={progress} />
          </span>
          <div
            role="progressbar"
            aria-label={`Progress of ${operation.title}`}
            aria-valuemin={0}
            aria-valuemax={progress.total_steps}
            aria-valuenow={progress.completed_steps}
            className="h-1 w-24 shrink-0 overflow-hidden rounded-full bg-hairline"
          >
            <div
              className={cn(
                "h-full",
                operation.state === "AWAITING_APPROVAL" ? "bg-status-amber" : "bg-status-blue",
              )}
              style={{ width: `${(progress.completed_steps / Math.max(progress.total_steps, 1)) * 100}%` }}
            />
          </div>
        </>
      )}
    </li>
  );
}

/**
 * Counts by kind and ownership, engine versions in use, what is running, what needs attention, and the
 * first steps until they are all done.
 *
 * @example
 * <Route index element={<OverviewPage />} />
 */
export default function OverviewPage() {
  const { selected } = useEnvironment();
  const { open } = useTask();
  const { data, error } = useOverviewData(selected?.slug);
  if (!selected) return <p className="text-muted">Add an environment to begin.</p>;
  if (error && !data) {
    return (
      <p role="alert" className="text-sm text-status-red">
        Could not load the overview: {error}
      </p>
    );
  }
  if (!data) return <p className="text-muted">Loading…</p>;
  const { page, running } = data;
  const { overview } = page;
  const refreshInventory = () => open({ kind: "inventory_refresh", environmentSlug: selected.slug });
  const steps = [
    { label: "Import the environment", done: page.first_steps.imported === true },
    {
      label: "Refresh inventory",
      done: page.first_steps.refreshed === true,
      action: (
        <Button size="sm" variant="outline" onClick={refreshInventory}>
          Refresh now
        </Button>
      ),
    },
    {
      label: "Adopt resources or switch the environment to direct mode",
      done: page.first_steps.adopted_or_direct === true,
      action: (
        <Button
          size="sm"
          variant="outline"
          onClick={() => open({ kind: "terraform_adoption", environmentSlug: selected.slug })}
        >
          Adopt
        </Button>
      ),
    },
    { label: "Set the change-management mode", done: page.first_steps.mode_set === true },
  ];
  const ownership = Object.entries(overview.ownership_counts);
  const versions = Object.entries(overview.engine_versions);
  return (
    <>
      <PageHeader
        breadcrumb={[{ label: selected.display_name }]}
        title="Overview"
        chips={
          <>
            <StatusBadge status={overview.change_management} />
            <span className="text-xs text-muted">
              last refresh{" "}
              {overview.last_refresh_at_utc
                ? new Date(overview.last_refresh_at_utc).toLocaleString()
                : "never"}
            </span>
          </>
        }
        actions={<Button onClick={refreshInventory}>Refresh inventory</Button>}
      />
      {error && (
        <p role="alert" className="mb-4 text-sm text-status-red">
          Could not refresh the overview: {error}
        </p>
      )}
      {!steps.every((step) => step.done) && (
        <section className="mb-6 rounded-lg border border-hairline bg-surface p-4">
          <h2 className="mb-2 text-sm font-medium">First steps</h2>
          <Checklist items={steps} />
        </section>
      )}
      <div className="grid gap-4 md:grid-cols-4">
        {KINDS.map(({ kind, label, to }) => (
          <Link
            key={kind}
            to={to}
            className="rounded-lg border border-hairline bg-surface p-4 hover:border-accent"
          >
            <div className="text-3xl font-semibold">{overview.counts[kind] ?? 0}</div>
            <div className="text-sm text-muted">{label}</div>
          </Link>
        ))}
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-3">
        <Panel title="Ownership">
          <ul className="space-y-1 text-sm">
            {ownership.map(([name, count]) => (
              <li key={name} className="flex items-center justify-between">
                <OwnershipBadge ownership={name} />
                <span className="font-mono">{count}</span>
              </li>
            ))}
            {ownership.length === 0 && <li className="text-muted">No resources yet.</li>}
          </ul>
        </Panel>
        <Panel title="Engine versions">
          <ul className="space-y-1 text-sm">
            {versions.map(([version, count]) => (
              <li key={version} className="flex justify-between">
                <span className="font-mono">{version}</span>
                <span className="font-mono">{count}</span>
              </li>
            ))}
            {versions.length === 0 && <li className="text-muted">No resources yet.</li>}
          </ul>
        </Panel>
        <Panel title="Needs attention">
          <Link to="/attention" className="text-sm text-accent">
            {attentionSummary(overview.attention_count)}
          </Link>
        </Panel>
      </div>
      <section className="mt-4 rounded-lg border border-hairline bg-surface">
        <h2 className="border-b border-hairline px-4 py-2 text-sm font-medium">
          Running now ({running.length})
        </h2>
        <ul>
          {running.map((operation) => (
            <RunningOperationRow key={operation.id} operation={operation} />
          ))}
          {running.length === 0 && <li className="px-4 py-2 text-sm text-muted">Nothing running.</li>}
        </ul>
      </section>
    </>
  );
}
