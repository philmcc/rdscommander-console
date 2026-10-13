import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";
import { apiGet, apiPost } from "@/api/client";
import type { ChangeEventPage, ChangeEventSummary } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { describeError } from "@/lib/describeError";
import { displayValue } from "@/lib/recordValues";

/** The most change events one answer carries; the resource and attribute filters narrow these further. */
const CHANGE_LIMIT = 500;

/** The change events last loaded, and the environment they belong to. */
interface LoadedChanges {
  environmentSlug: string;
  events: ChangeEventSummary[];
}

/** Why the table has no rows: still loading, nothing recorded, a server filter that leaves none, or no match in the answer. */
function emptyMessage(state: {
  answered: boolean;
  problem: string | null;
  loadedCount: number;
  serverFiltered: boolean;
  capped: boolean;
}): string {
  if (!state.answered) return state.problem ? "No changes to show." : "Loading…";
  if (state.loadedCount === 0) {
    return state.serverFiltered
      ? "No changes match the explanation and review filters."
      : "No changes have been recorded in this environment yet.";
  }
  return state.capped ? `None of the latest ${CHANGE_LIMIT} changes match.` : "No changes match.";
}

/** One change: when, which resource and attribute, before and after, who or what changed it, and the review. */
function ChangeRow({
  event,
  reviewing,
  onReview,
}: {
  event: ChangeEventSummary;
  /** The review request for this change is in flight. */
  reviewing: boolean;
  onReview: (id: number) => void;
}) {
  const baseId = useId();
  const resourceId = `${baseId}-resource`;
  const attributeId = `${baseId}-attribute`;
  const resourceIdentifier = event.resource_arn.split(":").at(-1);
  return (
    <TableRow className="h-9">
      <TableCell className="text-xs text-muted">{new Date(event.detected_at_utc).toLocaleString()}</TableCell>
      <TableCell id={resourceId} className="font-mono">
        <Link to={`/resources/${event.resource_id}`} className="hover:text-accent">
          {resourceIdentifier}
        </Link>
      </TableCell>
      <TableCell id={attributeId} className="font-mono">
        {event.attribute_path}
      </TableCell>
      <TableCell className="max-w-64 truncate font-mono text-muted" title={displayValue(event.value_before)}>
        {displayValue(event.value_before)}
      </TableCell>
      <TableCell className="max-w-64 truncate font-mono" title={displayValue(event.value_after)}>
        {displayValue(event.value_after)}
      </TableCell>
      <TableCell>
        {event.explanation === "operation" && event.operation_id ? (
          <Link to={`/operations/${event.operation_id}`} className="text-accent hover:underline">
            by the console
          </Link>
        ) : event.explanation === "operation" ? (
          <span className="text-muted">by the console</span>
        ) : (
          <span className="text-status-amber">outside the console</span>
        )}
      </TableCell>
      <TableCell>
        {event.explanation === "outside_console" && !event.reviewed_at_utc ? (
          <Button
            size="sm"
            variant="ghost"
            aria-describedby={`${resourceId} ${attributeId}`}
            disabled={reviewing}
            onClick={() => onReview(event.id)}
          >
            Mark reviewed
          </Button>
        ) : (
          event.reviewed_by_operator_name && (
            <span className="text-xs text-muted">reviewed by {event.reviewed_by_operator_name}</span>
          )
        )}
      </TableCell>
    </TableRow>
  );
}

/**
 * Change events of an environment with filters by resource, attribute and explanation. A change made outside
 * the console can be marked reviewed; one the console made links to its operation.
 *
 * @example
 * <ChangesView environmentSlug="sandbox" />
 */
export function ChangesView({ environmentSlug }: { environmentSlug: string }) {
  const [explanation, setExplanation] = useState("");
  const [attribute, setAttribute] = useState("");
  const [resource, setResource] = useState("");
  const [unreviewed, setUnreviewed] = useState(false);
  const [loaded, setLoaded] = useState<LoadedChanges | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  // The changes whose review request is in flight; a second click on one would send the request again.
  const [reviewing, setReviewing] = useState<ReadonlySet<number>>(new Set());
  // Counts the loads started; only the latest one may show its answer.
  const latestLoad = useRef(0);

  const load = useCallback(async () => {
    latestLoad.current += 1;
    const thisLoad = latestLoad.current;
    try {
      const page = await apiGet<ChangeEventPage>("/api/v1/history/changes", {
        environment_slug: environmentSlug,
        explanation,
        unreviewed_only: unreviewed ? "true" : "",
        limit: CHANGE_LIMIT,
      });
      if (thisLoad !== latestLoad.current) return;
      setLoaded({ environmentSlug, events: page.events });
      setProblem(null);
    } catch (caught) {
      if (thisLoad !== latestLoad.current) return;
      setProblem(`Could not load the changes: ${describeError(caught)}`);
    }
  }, [environmentSlug, explanation, unreviewed]);

  useEffect(() => {
    void load();
  }, [load]);

  const review = async (id: number) => {
    setReviewing((current) => new Set(current).add(id));
    try {
      await apiPost(`/api/v1/history/changes/${id}/review`);
      await load();
    } catch (caught) {
      setProblem(`Could not mark the change reviewed: ${describeError(caught)}`);
    } finally {
      setReviewing((current) => {
        const remaining = new Set(current);
        remaining.delete(id);
        return remaining;
      });
    }
  };

  // Rows loaded for another environment are never shown under this one.
  const answered = loaded?.environmentSlug === environmentSlug;
  const events = answered ? loaded.events : [];
  // An answer of exactly the limit may have more behind it, and the resource and attribute filters see only it.
  const capped = events.length === CHANGE_LIMIT;
  const resourceText = resource.toLowerCase();
  const attributeText = attribute.toLowerCase();
  const shown = events.filter(
    (event) =>
      event.resource_arn.toLowerCase().includes(resourceText) &&
      event.attribute_path.toLowerCase().includes(attributeText),
  );
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="space-y-1" htmlFor="changes-resource">
          <span>Resource</span>
          <Input
            id="changes-resource"
            value={resource}
            onChange={(e) => setResource(e.target.value)}
            className="h-8 font-mono"
            placeholder="identifier"
          />
        </label>
        <label className="space-y-1" htmlFor="changes-attribute">
          <span>Attribute</span>
          <Input
            id="changes-attribute"
            value={attribute}
            onChange={(e) => setAttribute(e.target.value)}
            className="h-8 font-mono"
            placeholder="db_instance_class"
          />
        </label>
        <label className="space-y-1">
          <span>Explanation</span>
          <select
            aria-label="Explanation"
            value={explanation}
            onChange={(e) => setExplanation(e.target.value)}
            className="block h-8 rounded-md border border-hairline bg-surface px-2"
          >
            <option value="">All</option>
            <option value="operation">By the console</option>
            <option value="outside_console">Outside the console</option>
          </select>
        </label>
        <label className="flex h-8 items-center gap-2">
          <input type="checkbox" checked={unreviewed} onChange={(e) => setUnreviewed(e.target.checked)} />
          Unreviewed only
        </label>
      </div>
      {problem && (
        <p role="alert" className="text-sm text-status-red">
          {problem}
        </p>
      )}
      {capped && (
        <p role="status" className="text-sm text-muted">
          Showing the latest {CHANGE_LIMIT} changes; the resource and attribute filters search only these.
          Narrow by explanation or unreviewed to see older ones.
        </p>
      )}
      <div className="max-h-[70vh] overflow-auto rounded-lg border border-hairline bg-surface [&_[data-slot=table-container]]:overflow-visible">
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-surface">
            <TableRow>
              <TableHead>When</TableHead>
              <TableHead>Resource</TableHead>
              <TableHead>Attribute</TableHead>
              <TableHead>Before</TableHead>
              <TableHead>After</TableHead>
              <TableHead>Explanation</TableHead>
              <TableHead>
                <span className="sr-only">Review</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((event) => (
              <ChangeRow key={event.id} event={event} reviewing={reviewing.has(event.id)} onReview={review} />
            ))}
            {shown.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} className="h-9 text-muted">
                  {emptyMessage({
                    answered,
                    problem,
                    loadedCount: events.length,
                    serverFiltered: explanation !== "" || unreviewed,
                    capped,
                  })}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
