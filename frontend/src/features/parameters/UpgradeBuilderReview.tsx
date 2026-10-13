import { useEffect, useId, useState } from "react";
import { apiPost } from "@/api/client";
import type { UpgradeGroupPlan, UpgradePreview } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { describeError } from "@/lib/describeError";

/** Edits per scope (`instance`, `cluster`): parameter name in the target family to value. */
export type UpgradeOverrides = Record<string, Record<string, string>>;

/** Props of the upgrade review; the form field passes the resource, the chosen version and the edits. */
export interface UpgradeBuilderReviewProps {
  resourceId: string;
  targetEngineVersion: string;
  overrides: UpgradeOverrides;
  onChange: (overrides: UpgradeOverrides) => void;
}

type Classification = UpgradeGroupPlan["classifications"][number];

interface PlanReviewProps {
  plan: UpgradeGroupPlan;
  headingId: string;
  edits: Record<string, string>;
  /** Called with the parameter's name in the target family and the value typed for it. */
  onEdit: (key: string, classification: Classification, value: string) => void;
}

/** One group the build will create: where it comes from, and every override with its fate. */
function PlanReview({ plan, headingId, edits, onEdit }: PlanReviewProps) {
  return (
    <section aria-labelledby={headingId} className="rounded-lg border border-hairline bg-surface">
      <h3 id={headingId} className="border-b border-hairline px-3 py-2 text-sm">
        <span className="text-muted">{plan.scope} group</span>{" "}
        <span className="font-mono">{plan.new_group_name}</span>{" "}
        <span className="text-muted">
          from <span className="font-mono">{plan.source_group_name}</span> ({plan.source_family} →{" "}
          {plan.target_family})
        </span>
        {plan.already_built && (
          <span className="ml-2 rounded border border-hairline px-1.5 text-xs text-muted">
            already built — kept
          </span>
        )}
      </h3>
      {plan.already_built && (
        <p className="border-b border-hairline px-3 py-2 text-xs text-muted">
          This group exists from an earlier build. The build leaves it as it is, so the values below are not
          applied.
        </p>
      )}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Parameter</TableHead>
            <TableHead>Outcome</TableHead>
            <TableHead>Value in the new group</TableHead>
            <TableHead>Note</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {plan.classifications.map((classification) => {
            const key = classification.new_name ?? classification.name;
            // A value that needs review is left out of the new group until the operator gives it one.
            const value =
              edits[key] ?? (classification.outcome === "needs_review" ? "" : classification.value);
            return (
              <TableRow key={classification.name} className="h-9">
                <TableCell className="font-mono">
                  {classification.name}
                  {classification.new_name && (
                    <>
                      {" → "}
                      <span>{classification.new_name}</span>
                    </>
                  )}
                </TableCell>
                <TableCell>
                  <StatusBadge status={classification.outcome} />
                </TableCell>
                <TableCell>
                  {classification.outcome === "removed" ? (
                    <>
                      <span className="text-muted">dropped</span>{" "}
                      <span className="font-mono text-xs text-muted">(was {classification.value})</span>
                    </>
                  ) : (
                    <Input
                      aria-label={`Value of ${key}`}
                      value={value}
                      disabled={plan.already_built}
                      onChange={(event) => onEdit(key, classification, event.target.value)}
                      className="h-7 min-w-48 font-mono"
                    />
                  )}
                </TableCell>
                <TableCell className="whitespace-normal text-xs text-muted">
                  {classification.note}
                  {classification.outcome === "default_changed" && (
                    <>
                      {" "}
                      <span className="font-mono">
                        (default {classification.old_default} → {classification.new_default})
                      </span>
                    </>
                  )}
                </TableCell>
              </TableRow>
            );
          })}
          {plan.classifications.length === 0 && (
            <TableRow>
              <TableCell colSpan={4} className="whitespace-normal text-muted">
                No overrides on the current group; the new group starts from {plan.target_family} defaults.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </section>
  );
}

/**
 * The parameter groups an upgrade will build, one table per group (instance, and cluster for Aurora): each
 * override of the current group with how it fares in the target family (carried, default changed, renamed,
 * needs review or removed), a note, and an editable value for everything that is not dropped. Edits land in
 * `overrides[scope][new_name ?? name]`; clearing a value that needs review leaves it out again. A group that
 * was built earlier is marked as kept and its values cannot be edited.
 *
 * @example
 * <UpgradeBuilderReview resourceId={resourceId} targetEngineVersion="17.2" overrides={overrides} onChange={setOverrides} />
 */
export function UpgradeBuilderReview({
  resourceId,
  targetEngineVersion,
  overrides,
  onChange,
}: UpgradeBuilderReviewProps) {
  const [preview, setPreview] = useState<UpgradePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const headingIdBase = useId();
  useEffect(() => {
    if (!resourceId || !targetEngineVersion) return;
    let outdated = false;
    setPreview(null);
    setError(null);
    apiPost<UpgradePreview>("/api/v1/parameters/upgrade-preview", {
      resource_id: resourceId,
      target_engine_version: targetEngineVersion,
    }).then(
      (answer) => {
        if (!outdated) setPreview(answer);
      },
      (caught: unknown) => {
        if (!outdated) setError(describeError(caught));
      },
    );
    return () => {
      outdated = true;
    };
  }, [resourceId, targetEngineVersion]);

  if (!targetEngineVersion) {
    return <p className="text-sm text-muted">Choose a target version to preview the groups.</p>;
  }
  if (error) {
    return (
      <p role="alert" className="text-sm text-status-red">
        Could not preview the groups: {error}
      </p>
    );
  }
  if (!preview) {
    return (
      <p role="status" className="text-sm text-muted">
        Classifying overrides…
      </p>
    );
  }

  const edit = (scope: string, key: string, classification: Classification, value: string) => {
    const scopeEdits = { ...overrides[scope] };
    if (value === "" && classification.outcome === "needs_review") delete scopeEdits[key];
    else scopeEdits[key] = value;
    const { [scope]: _replaced, ...otherScopes } = overrides;
    onChange(Object.keys(scopeEdits).length > 0 ? { ...otherScopes, [scope]: scopeEdits } : otherScopes);
  };
  return (
    <div className="space-y-4">
      {preview.plans.map((plan) => (
        <PlanReview
          key={plan.scope}
          plan={plan}
          headingId={`${headingIdBase}-${plan.scope}`}
          edits={overrides[plan.scope] ?? {}}
          onEdit={(key, classification, value) => edit(plan.scope, key, classification, value)}
        />
      ))}
      {preview.plans.length === 0 && (
        <p className="text-sm text-muted">No parameter groups need building for this version.</p>
      )}
    </div>
  );
}
