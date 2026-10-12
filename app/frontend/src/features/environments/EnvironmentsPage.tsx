import { useId, useState } from "react";
import { Link } from "react-router";
import type { EnvironmentOverview } from "@/api/types";
import { environmentHue, useEnvironment } from "@/app/EnvironmentContext";
import { EmptyState } from "@/components/EmptyState";
import { EnvironmentChip } from "@/components/EnvironmentChip";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { OperationTitle } from "@/features/operations/OperationTitle";
import { ALL_ENVIRONMENTS, useOperations } from "@/features/operations/useOperations";
import { useReloadWhenTaskCloses } from "@/features/tasks/useReloadWhenTaskCloses";
import { formatUtc } from "@/lib/formatUtc";
import { cn } from "@/lib/utils";
import { ImportWizard } from "./ImportWizard";
import { useEnvironmentOverviews } from "./useEnvironmentOverviews";

/** Resource kinds in the order a card counts them. */
const COUNTED_KINDS: { kind: string; label: string }[] = [
  { kind: "instance", label: "Instances" },
  { kind: "cluster", label: "Clusters" },
  { kind: "parameter_group", label: "Parameter groups" },
  { kind: "snapshot", label: "Snapshots" },
];

/** The kind of the task that adds an environment; its operations belong to no environment. */
const ENVIRONMENT_IMPORT_KIND = "environment_import";
/** How many of the latest imports the page lists. */
const RECENT_IMPORT_COUNT = 5;

function attentionSummary(count: number): string {
  if (count === 0) return "Nothing needs attention";
  return count === 1 ? "1 item needs attention" : `${count} items need attention`;
}

function runningSummary(count: number): string {
  return count === 1 ? "1 operation running" : `${count} operations running`;
}

/** One environment: its chip and mode, what it holds, when it was last read, and the way into its page. */
function EnvironmentCard({ overview }: { overview: EnvironmentOverview }) {
  const { environments } = useEnvironment();
  // A card can briefly know an environment the switcher has not yet been told about.
  const chipEnvironment = environments.find((candidate) => candidate.slug === overview.slug) ?? {
    display_name: overview.display_name,
    is_production: overview.is_production,
    targets: [],
  };
  return (
    <article
      aria-label={overview.display_name}
      className={cn(
        "flex flex-col rounded-lg border border-hairline bg-surface p-4",
        overview.is_production && "border-l-4 border-l-status-red",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <EnvironmentChip environment={chipEnvironment} hue={environmentHue(environments, overview.slug)} />
        <StatusBadge status={overview.change_management} />
      </div>
      <p className="mt-1 font-mono text-xs text-muted">{overview.slug}</p>
      <ul aria-label="Resource counts" className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        {COUNTED_KINDS.map(({ kind, label }) => (
          <li key={kind}>
            <span className="font-mono">{overview.counts[kind] ?? 0}</span>{" "}
            <span className="text-muted">{label}</span>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-muted">
        last refresh{" "}
        {overview.last_refresh_at_utc ? new Date(overview.last_refresh_at_utc).toLocaleString() : "never"}
      </p>
      <p className={cn("mt-1 text-sm", overview.attention_count > 0 ? "text-status-amber" : "text-muted")}>
        {attentionSummary(overview.attention_count)}
      </p>
      {overview.running_operation_count > 0 && (
        <p className="mt-1 text-sm text-status-blue">{runningSummary(overview.running_operation_count)}</p>
      )}
      <div className="mt-auto flex gap-2 pt-4">
        <Button asChild size="sm">
          <Link to={`/environments/${overview.slug}`} aria-label={`Open ${overview.display_name}`}>
            Open
          </Link>
        </Button>
        <Button asChild size="sm" variant="outline">
          <Link
            to={`/environments/compare?left=${encodeURIComponent(overview.slug)}`}
            aria-label={`Compare ${overview.display_name} with another`}
          >
            Compare
          </Link>
        </Button>
      </div>
    </article>
  );
}

/**
 * The latest environment imports, newest first: each one's state, when it started and a link to its
 * operation, with the reason a failed one stopped. An import belongs to no environment, so this is where it
 * can be found again once its task panel has closed. Read every five seconds and when the task panel closes.
 *
 * @example
 * <RecentImports />
 */
function RecentImports() {
  const headingId = useId();
  const { operations, loading, error, reload } = useOperations(ALL_ENVIRONMENTS, "", ENVIRONMENT_IMPORT_KIND);
  useReloadWhenTaskCloses(reload);
  const recent = operations.slice(0, RECENT_IMPORT_COUNT);
  return (
    <section aria-labelledby={headingId} className="mt-6 rounded-lg border border-hairline bg-surface">
      <h2 id={headingId} className="border-b border-hairline px-3 py-2 text-sm font-medium">
        Recent imports
      </h2>
      {error && (
        <p role="alert" className="px-3 py-2 text-sm text-status-red">
          Could not load the imports: {error}
        </p>
      )}
      {recent.length > 0 ? (
        <ul>
          {recent.map((operation) => (
            <li
              key={operation.id}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-hairline px-3 py-2 text-sm last:border-0"
            >
              <StatusBadge status={operation.state} />
              <Link to={`/operations/${operation.id}`} className="min-w-0 break-words hover:text-accent">
                <OperationTitle title={operation.title} />
              </Link>
              <span className="text-xs text-muted">{formatUtc(operation.created_at_utc)}</span>
              {operation.failure_summary && (
                <span className="basis-full text-status-red">{operation.failure_summary}</span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        !error && <p className="px-3 py-2 text-sm text-muted">{loading ? "Loading…" : "No imports yet."}</p>
      )}
    </section>
  );
}

/**
 * Every environment as a card, with the import wizard behind an "Import environment" button (shown at once
 * when there is no environment yet), and the recent imports with the way back to each one's operation. The
 * list is read again when the task panel closes, because the task may have imported an environment; the
 * task panel itself refreshes the switcher after an import.
 *
 * @example
 * <Route path="environments" element={<EnvironmentsPage />} />
 */
export default function EnvironmentsPage() {
  const { environments, error, reload } = useEnvironmentOverviews();
  const [wizardShown, setWizardShown] = useState(false);
  useReloadWhenTaskCloses(reload);

  const none = environments !== null && environments.length === 0;
  return (
    <>
      <PageHeader
        breadcrumb={[{ label: "Environments" }]}
        title="Environments"
        actions={
          none ? undefined : (
            <Button
              variant="outline"
              aria-expanded={wizardShown}
              onClick={() => setWizardShown((shown) => !shown)}
            >
              Import environment
            </Button>
          )
        }
      />
      {error && (
        <p role="alert" className="mb-4 text-sm text-status-red">
          Could not load the environments: {error}
        </p>
      )}
      {environments === null && !error && <p className="text-muted">Loading…</p>}
      {none && <EmptyState icon="○" title="Import your first environment to begin." />}
      {environments !== null && environments.length > 0 && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {environments.map((overview) => (
            <EnvironmentCard key={overview.slug} overview={overview} />
          ))}
        </div>
      )}
      {(wizardShown || none) && (
        <div className="mt-4">
          <ImportWizard />
        </div>
      )}
      <RecentImports />
    </>
  );
}
