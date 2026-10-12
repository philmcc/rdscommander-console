import { useEffect, useState } from "react";
import { apiGet } from "@/api/client";
import type { PlanView } from "@/api/types";
import { Badge } from "@/components/ui/badge";

interface ResourceChange {
  address: string;
  resource_type: string;
  actions: string[];
  is_import: boolean;
  import_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

interface Summary {
  create_count: number;
  update_count: number;
  delete_count: number;
  replace_count: number;
  import_count: number;
  no_op_count: number;
  resource_changes: ResourceChange[];
}

function changedKeys(change: ResourceChange): string[] {
  const keys = new Set([...Object.keys(change.before ?? {}), ...Object.keys(change.after ?? {})]);
  return [...keys]
    .filter((k) => JSON.stringify(change.before?.[k]) !== JSON.stringify(change.after?.[k]))
    .sort();
}

type NamedEntry = Record<string, unknown> & { name: string };

function isNamedEntry(value: unknown): value is NamedEntry {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    typeof (value as Record<string, unknown>).name === "string"
  );
}

/**
 * The entries of a set-of-object attribute keyed by their `name`, or null when the value is not such a set.
 * A missing value is an empty set; a list with an entry that has no string `name`, or two entries of one
 * name, is not a named set and keeps the plain before/after view.
 *
 * @example
 * namedEntries([{ name: "work_mem", value: "16384" }]); // Map { "work_mem" => { name, value } }
 * namedEntries(["a", "b"]); // null
 */
function namedEntries(value: unknown): Map<string, NamedEntry> | null {
  if (value === null || value === undefined) return new Map();
  if (!Array.isArray(value)) return null;
  const entries = new Map<string, NamedEntry>();
  for (const item of value) {
    if (!isNamedEntry(item) || entries.has(item.name)) return null;
    entries.set(item.name, item);
  }
  return entries;
}

type NamedSetChangeKind = "changed" | "added" | "removed";

interface NamedSetChange {
  name: string;
  kind: NamedSetChangeKind;
  before: NamedEntry | undefined;
  after: NamedEntry | undefined;
}

interface NamedSetDiff {
  attribute: string;
  changes: NamedSetChange[];
  unchangedCount: number;
}

/**
 * A set-of-object attribute keyed by `name` (such as a parameter group's `parameter` set) compared entry by
 * entry, or null when the attribute is not a named set on both sides. Changes are sorted by name.
 *
 * @example
 * namedSetDiff("parameter", [{ name: "work_mem", value: "16384" }], [{ name: "work_mem", value: "65536" }]);
 * // { attribute: "parameter", changes: [{ name: "work_mem", kind: "changed", … }], unchangedCount: 0 }
 */
function namedSetDiff(attribute: string, before: unknown, after: unknown): NamedSetDiff | null {
  if (!Array.isArray(before) && !Array.isArray(after)) return null;
  const beforeEntries = namedEntries(before);
  const afterEntries = namedEntries(after);
  if (beforeEntries === null || afterEntries === null) return null;
  const names = [...new Set([...beforeEntries.keys(), ...afterEntries.keys()])].sort();
  const changes: NamedSetChange[] = [];
  let unchangedCount = 0;
  for (const name of names) {
    const beforeEntry = beforeEntries.get(name);
    const afterEntry = afterEntries.get(name);
    if (beforeEntry && afterEntry && JSON.stringify(beforeEntry) === JSON.stringify(afterEntry)) {
      unchangedCount += 1;
      continue;
    }
    const kind: NamedSetChangeKind = !beforeEntry ? "added" : !afterEntry ? "removed" : "changed";
    changes.push({ name, kind, before: beforeEntry, after: afterEntry });
  }
  return { attribute, changes, unchangedCount };
}

function showValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value ?? null);
}

/**
 * One side of a named-set entry: its `value` in the cell's main line, and its other fields (such as
 * `apply_method`) underneath. A changed entry lists only the fields that differ from the other side; an
 * entry that is going away (`valueOnly`) shows its value alone, since how it applied no longer matters.
 */
function NamedEntryCell({
  entry,
  other,
  valueOnly = false,
}: {
  entry: NamedEntry | undefined;
  other: NamedEntry | undefined;
  valueOnly?: boolean;
}) {
  if (!entry) return <span className="text-muted">—</span>;
  const fields = valueOnly
    ? []
    : Object.keys(entry)
        .filter((key) => key !== "name" && key !== "value")
        .filter((key) => !other || JSON.stringify(entry[key]) !== JSON.stringify(other[key]))
        .sort();
  return (
    <>
      {"value" in entry ? <span className="block font-mono">{showValue(entry.value)}</span> : null}
      {fields.map((key) => (
        <span key={key} className="block text-muted">
          {key}: {showValue(entry[key])}
        </span>
      ))}
    </>
  );
}

/** The set of a parameter group (instance or cluster) that holds its user-set parameters. */
const PARAMETER_SET_ATTRIBUTE = "parameter";

/**
 * The words for a kind of change in one named set. A parameter left out of a parameter group's set goes back
 * to its default, which is what the person asked for when they reset it; any other set's entry is "removed".
 *
 * @example
 * changeKindLabel("parameter", "removed"); // "reset to default"
 */
function changeKindLabel(attribute: string, kind: NamedSetChangeKind): string {
  if (kind === "removed" && attribute === PARAMETER_SET_ATTRIBUTE) return "reset to default";
  return kind;
}

/** Counts of a named set's changes as one phrase, such as "1 changed, 2 added, 0 removed, 41 unchanged". */
function namedSetCounts(diff: NamedSetDiff): string {
  const count = (kind: NamedSetChangeKind) => diff.changes.filter((change) => change.kind === kind).length;
  const removed = changeKindLabel(diff.attribute, "removed");
  return `${count("changed")} changed, ${count("added")} added, ${count("removed")} ${removed}, ${diff.unchangedCount} unchanged`;
}

/**
 * A named set's changes as rows: each entry's name with whether it was changed, added or removed, and its
 * value before and after. Entries that did not change are counted, not listed.
 *
 * @example
 * <NamedSetTable diff={namedSetDiff("parameter", before.parameter, after.parameter)} />
 */
function NamedSetTable({ diff }: { diff: NamedSetDiff }) {
  return (
    <div className="mt-2 space-y-1">
      <p className="flex flex-wrap items-baseline gap-x-2 text-xs">
        <span className="font-mono">{diff.attribute}</span>
        <span className="text-muted">{namedSetCounts(diff)}</span>
      </p>
      <table aria-label={`${diff.attribute} changes`} className="w-full table-fixed text-xs">
        <colgroup>
          <col className="w-[44%]" />
          <col className="w-[28%]" />
          <col className="w-[28%]" />
        </colgroup>
        <thead>
          <tr className="text-left text-muted">
            <th className="py-1 pr-2 font-normal">Name</th>
            <th className="py-1 pr-2 font-normal">Before</th>
            <th className="py-1 font-normal">After</th>
          </tr>
        </thead>
        <tbody>
          {diff.changes.map((change) => (
            <tr key={change.name} className="border-t align-top">
              <td className="py-1 pr-2 [overflow-wrap:anywhere]">
                <span className="block font-mono">{change.name}</span>
                <Badge variant="outline" className="mt-0.5">
                  {changeKindLabel(diff.attribute, change.kind)}
                </Badge>
              </td>
              <td
                className={`py-1 pr-2 text-muted [overflow-wrap:anywhere] ${change.before ? "line-through" : ""}`}
              >
                <NamedEntryCell
                  entry={change.before}
                  other={change.after}
                  valueOnly={change.kind === "removed"}
                />
              </td>
              <td className="py-1 [overflow-wrap:anywhere]">
                <NamedEntryCell entry={change.after} other={change.before} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface PlanViewerProps {
  runId: number;
  /** Called with the run id once that run's plan summary has loaded and is on screen. */
  onLoaded?: (runId: number) => void;
}

/**
 * Resource-by-resource view of a stored plan. Each changed attribute shows its value before and after; a
 * set of objects keyed by `name`, such as a parameter group's `parameter` set, shows one row per added,
 * removed or changed entry instead.
 */
export function PlanViewer({ runId, onLoaded }: PlanViewerProps) {
  const [plan, setPlan] = useState<PlanView | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    apiGet<PlanView>(`/api/v1/terraform/runs/${runId}/plan`).then(
      (loaded) => {
        if (!cancelled) setPlan(loaded);
      },
      (caught: unknown) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [runId]);
  const summary = plan?.plan_summary as Summary | null | undefined;
  const loadedRunId = summary ? plan?.run_id : undefined;
  useEffect(() => {
    if (loadedRunId === runId) onLoaded?.(runId);
  }, [loadedRunId, runId, onLoaded]);
  if (error) {
    return (
      <p role="alert" className="text-sm text-status-red">
        Could not load the plan: {error}
      </p>
    );
  }
  if (!summary) return <p className="text-sm text-muted">Loading plan…</p>;
  const visible = summary.resource_changes.filter((c) => c.actions.join() !== "no-op" || c.is_import);
  return (
    <div className="space-y-3 text-sm">
      <p>
        {summary.import_count} to import, {summary.create_count} to create, {summary.update_count} to update,{" "}
        {summary.replace_count} to replace, {summary.delete_count} to delete, {summary.no_op_count} unchanged
      </p>
      {visible.map((change) => {
        const keys = changedKeys(change);
        const namedSets = keys
          .map((key) => namedSetDiff(key, change.before?.[key], change.after?.[key]))
          .filter((diff): diff is NamedSetDiff => diff !== null);
        const namedSetAttributes = new Set(namedSets.map((diff) => diff.attribute));
        const plainKeys = keys.filter((key) => !namedSetAttributes.has(key));
        return (
          <div key={change.address} className="rounded-lg border p-2">
            <p className="flex flex-wrap items-center gap-2 font-mono text-xs">
              {change.address}
              {change.is_import ? <Badge variant="outline">import {change.import_id}</Badge> : null}
              {change.actions.map((a) => (
                <Badge key={a} variant={a === "delete" ? "destructive" : "outline"}>
                  {a}
                </Badge>
              ))}
            </p>
            {plainKeys.length ? (
              <table className="mt-2 w-full table-fixed text-xs">
                {/* Fixed shares: a long value wraps inside its own column instead of squeezing a short one. */}
                <colgroup>
                  <col className="w-[38%]" />
                  <col className="w-[22%]" />
                  <col className="w-[40%]" />
                </colgroup>
                <tbody>
                  {plainKeys.map((key) => (
                    <tr key={key} className="border-t align-top">
                      <td className="py-1 pr-2 font-mono [overflow-wrap:anywhere]">{key}</td>
                      <td className="py-1 pr-2 text-muted line-through [overflow-wrap:anywhere]">
                        {JSON.stringify(change.before?.[key] ?? null)}
                      </td>
                      <td className="py-1 [overflow-wrap:anywhere]">
                        {JSON.stringify(change.after?.[key] ?? null)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
            {namedSets.map((diff) => (
              <NamedSetTable key={diff.attribute} diff={diff} />
            ))}
          </div>
        );
      })}
    </div>
  );
}
