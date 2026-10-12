import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import type { EnvironmentSummary, OperationSummary } from "@/api/types";
import { environmentHue, useEnvironment } from "@/app/EnvironmentContext";
import { type Column, DataTable } from "@/components/DataTable";
import { EnvironmentChip } from "@/components/EnvironmentChip";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { useTask } from "@/features/tasks/TaskContext";
import { cn } from "@/lib/utils";
import { OperationDetail } from "./OperationDetail";
import { OperationProgressText } from "./OperationProgressText";
import { OperationTitle } from "./OperationTitle";
import { ALL_ENVIRONMENTS, useOperations } from "./useOperations";

/** The state filter's choices; `states` is what the operations API takes, empty for every state. */
const STATE_FILTERS = [
  { key: "all", label: "All", states: "", empty: "No operations yet." },
  { key: "active", label: "Active", states: "QUEUED,RUNNING,AWAITING_APPROVAL", empty: "Nothing is active." },
  { key: "failed", label: "Failed", states: "FAILED", empty: "Nothing has failed." },
] as const;

/** The columns that stay while an operation is open beside the list; the detail shows the rest. */
const COMPACT_COLUMN_KEYS = new Set(["title", "state"]);

/** What the filter box matches: the title, state, kind, author and environment, whichever columns are showing. */
function searchText(operation: OperationSummary): string {
  return [
    operation.title,
    operation.state,
    operation.operation_kind,
    operation.created_by_operator_name,
    operation.environment_slug ?? "",
  ].join(" ");
}

/**
 * The environment an operation of the all-environments list belongs to: its chip, or "No environment" for
 * one that acts on none (an environment import).
 */
function OperationEnvironment({
  slug,
  environments,
}: {
  slug: string | null;
  environments: EnvironmentSummary[];
}) {
  const environment = slug ? environments.find((candidate) => candidate.slug === slug) : undefined;
  if (environment)
    return <EnvironmentChip environment={environment} hue={environmentHue(environments, environment.slug)} />;
  return slug ? (
    <span className="font-mono text-xs">{slug}</span>
  ) : (
    <span className="text-muted">No environment</span>
  );
}

/**
 * The list's columns; the title of the open operation is marked as the current one. While one is open the
 * list narrows to its title and state, and their cells wrap instead of cutting anything off. Listing every
 * environment (`environments` given) adds the environment each operation belongs to.
 */
function columnsFor(
  openOperationId: string | undefined,
  environments: EnvironmentSummary[] | null,
): Column<OperationSummary>[] {
  const environmentColumn: Column<OperationSummary>[] = environments
    ? [
        {
          key: "environment",
          header: "Environment",
          sortValue: (operation) => operation.environment_slug ?? "",
          render: (operation) => (
            <OperationEnvironment slug={operation.environment_slug} environments={environments} />
          ),
        },
      ]
    : [];
  const all: Column<OperationSummary>[] = [
    {
      key: "title",
      header: "Operation",
      sortValue: (operation) => operation.title,
      className: "whitespace-normal break-words",
      render: (operation) => (
        <Link
          to={`/operations/${operation.id}`}
          aria-current={operation.id === openOperationId ? "true" : undefined}
          className={cn("hover:text-accent", operation.id === openOperationId && "font-medium text-accent")}
        >
          <OperationTitle title={operation.title} />
        </Link>
      ),
    },
    {
      key: "state",
      header: "State",
      sortValue: (operation) => operation.state,
      className: "whitespace-normal",
      render: (operation) => (
        <span className="flex flex-wrap items-center gap-x-2">
          <StatusBadge status={operation.state} />
          {operation.progress && (
            <span className="text-xs text-muted">
              <OperationProgressText progress={operation.progress} />
            </span>
          )}
        </span>
      ),
    },
    ...environmentColumn,
    {
      key: "kind",
      header: "Kind",
      sortValue: (operation) => operation.operation_kind,
      render: (operation) => <span className="font-mono text-xs">{operation.operation_kind}</span>,
    },
    {
      key: "created_by",
      header: "Created by",
      sortValue: (operation) => operation.created_by_operator_name,
      render: (operation) => operation.created_by_operator_name,
    },
    {
      key: "created_at",
      header: "Created",
      sortValue: (operation) => operation.created_at_utc,
      render: (operation) => (
        <span className="text-xs text-muted">{new Date(operation.created_at_utc).toLocaleString()}</span>
      ),
    },
  ];
  return openOperationId ? all.filter((column) => COMPACT_COLUMN_KEYS.has(column.key)) : all;
}

/**
 * The activity list of the selected environment, or of every environment and the operations that belong to
 * none (environment imports) under "All environments"; when the address names an operation
 * (`/operations/{id}`) its detail takes the right column and the list narrows to make room. The list is read
 * again every five seconds and when the task panel closes, and can be narrowed to active or failed
 * operations; opening a row opens that operation's address.
 *
 * @example
 * <Route path="operations/:operationId" element={<OperationsPage />} />
 */
export default function OperationsPage() {
  const { selected, environments } = useEnvironment();
  const { operationId } = useParams();
  const navigate = useNavigate();
  const { current } = useTask();
  const [filterKey, setFilterKey] = useState<(typeof STATE_FILTERS)[number]["key"]>("all");
  const [everyEnvironment, setEveryEnvironment] = useState(false);
  const stateFilter = STATE_FILTERS.find((entry) => entry.key === filterKey) ?? STATE_FILTERS[0];
  const { operations, loading, error, reload } = useOperations(
    everyEnvironment ? ALL_ENVIRONMENTS : selected?.slug,
    stateFilter.states,
  );
  const columns = useMemo(
    () => columnsFor(operationId, everyEnvironment ? environments : null),
    [operationId, everyEnvironment, environments],
  );

  // A task may have changed an operation (an approval), so read the list when its panel closes.
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
      breadcrumb={
        everyEnvironment
          ? [{ label: "All environments" }, { label: "Activity" }]
          : selected
            ? [{ label: selected.display_name }, { label: "Activity" }]
            : [{ label: "Activity" }]
      }
      title="Operations"
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
      <div
        className={cn(
          "grid grid-cols-1 items-start gap-4",
          operationId && "lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]",
        )}
      >
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <fieldset className="flex gap-1">
              <legend className="sr-only">Show operations of</legend>
              <Button
                size="sm"
                variant={everyEnvironment ? "ghost" : "secondary"}
                aria-pressed={!everyEnvironment}
                onClick={() => setEveryEnvironment(false)}
              >
                {selected.display_name}
              </Button>
              <Button
                size="sm"
                variant={everyEnvironment ? "secondary" : "ghost"}
                aria-pressed={everyEnvironment}
                onClick={() => setEveryEnvironment(true)}
              >
                All environments
              </Button>
            </fieldset>
            <fieldset className="flex gap-1">
              <legend className="sr-only">Show operations in state</legend>
              {STATE_FILTERS.map((entry) => (
                <Button
                  key={entry.key}
                  size="sm"
                  variant={entry.key === filterKey ? "secondary" : "ghost"}
                  aria-pressed={entry.key === filterKey}
                  onClick={() => setFilterKey(entry.key)}
                >
                  {entry.label}
                </Button>
              ))}
            </fieldset>
          </div>
          {error && (
            <p role="alert" className="text-sm text-status-red">
              Could not load the operations: {error}
            </p>
          )}
          <DataTable
            columns={columns}
            rows={operations}
            rowKey={(operation) => operation.id}
            onOpen={(operation) => navigate(`/operations/${operation.id}`)}
            filterPlaceholder="Filter operations"
            searchText={searchText}
            empty={loading ? "Loading…" : stateFilter.empty}
          />
        </div>
        {operationId && <OperationDetail operationId={operationId} />}
      </div>
    </>
  );
}
