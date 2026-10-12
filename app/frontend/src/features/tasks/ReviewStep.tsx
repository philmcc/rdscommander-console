import { type ReactNode, useEffect, useId, useState } from "react";
import type { ChangeSetEntry, DiagnosticSummary, OperationDetail } from "@/api/types";
import { CopyableValue } from "@/components/CopyableValue";
import { DiffTable } from "@/components/DiffTable";
import { StatusBadge } from "@/components/StatusBadge";
import { PlanViewer } from "@/features/terraform/PlanViewer";

/** Preflight and diagnostic severities as the status the shared badge knows. */
function severityStatus(severity: string): string {
  if (severity === "warning") return "warn";
  if (severity === "error") return "fail";
  return "info";
}

/**
 * Diagnostics as checklist rows: a warning shows as warn, an error as fail, anything else as info, each with
 * its title, detail and the suggested fix. `heading` names the list; it defaults to "Preflight checks".
 *
 * @example
 * <PreflightChecklist reports={operation.diagnostic_reports} />
 */
export function PreflightChecklist({
  reports,
  heading = "Preflight checks",
}: {
  reports: DiagnosticSummary[];
  heading?: string;
}) {
  if (reports.length === 0) return null;
  return (
    <section className="space-y-1">
      <h3 className="text-sm font-medium">{heading}</h3>
      <ul className="space-y-2 text-sm">
        {reports.map((report) => (
          <li key={report.id} className="flex items-start gap-2">
            <StatusBadge status={severityStatus(report.severity)} className="mt-0.5 shrink-0" />
            {/* A detail can be a traceback with long unbroken runs; it wraps inside the panel. */}
            <span className="min-w-0 [overflow-wrap:anywhere]">
              <span className="font-medium">{report.title}</span>
              {report.detail && <span className="block text-muted">{report.detail}</span>}
              {report.fix_suggestion && <span className="block">Fix: {report.fix_suggestion}</span>}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * The change set grouped by resource address, one table per address; rows carry reboot and downtime flags.
 *
 * @example
 * <ChangeSetView changeSet={operation.change_set ?? []} />
 */
export function ChangeSetView({ changeSet }: { changeSet: ChangeSetEntry[] }) {
  const byAddress = new Map<string, ChangeSetEntry[]>();
  for (const entry of changeSet)
    byAddress.set(entry.address, [...(byAddress.get(entry.address) ?? []), entry]);
  return (
    <div className="space-y-3">
      {[...byAddress.entries()].map(([address, entries]) => (
        <div key={address} className="space-y-1">
          <p className="font-mono text-xs text-muted">{address}</p>
          <DiffTable
            rows={entries.map((entry) => ({
              attribute: entry.attribute,
              before: entry.current,
              after: entry.desired,
              flags: [
                ...(entry.apply_type === "pending-window" ? ["queued for the maintenance window"] : []),
                ...(entry.requires_reboot ? ["reboot"] : []),
                ...(entry.downtime && entry.downtime !== "none" ? [entry.downtime] : []),
              ],
            }))}
            beforeLabel="Current"
            afterLabel="New"
          />
        </div>
      ))}
    </div>
  );
}

/** What an action (reboot, failover, snapshot, promote…) will do, as its `await_approval` step stored it. */
export interface ActionReview {
  /** Sentences saying exactly what will happen. */
  what: string[];
  /** The resources acted on. */
  identifiers: string[];
  requiresReboot: boolean;
  /** What the approver should expect, in words; `none` when nothing is interrupted. */
  downtime: string;
  /** Whether the Terraform files are re-rendered and committed afterwards. */
  reconcile: boolean;
}

/** The change that follows the approved one in a two-phase operation (a decommission's delete). */
export interface SecondPhase {
  summary: string;
  /** What the second change touches. */
  what: string[];
  /** The second change's rows of `operation.change_set`; empty when they cannot be known before approval. */
  changeSet: ChangeSetEntry[];
}

function stringsOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function objectOf(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** The pause of a major upgrade at which production switches to the green side, typed by identifier. */
export const SWITCHOVER_APPROVAL_STAGE = "switchover";

/**
 * Which of an operation's pauses it is at (`plan.approval_stage`): a major upgrade pauses at `create_green`
 * and then at `switchover`; an operation that pauses once has no stage and reads as an empty string.
 *
 * @example
 * approvalStageOf(majorUpgrade); // "switchover"
 * approvalStageOf(reboot); // ""
 */
export function approvalStageOf(operation: OperationDetail): string {
  const stage = operation.plan.approval_stage;
  return typeof stage === "string" ? stage : "";
}

/** A major upgrade's blue/green deployment, as its plan and step outputs report it. */
export interface BlueGreenDeployment {
  deploymentId: string;
  deploymentName: string | null;
  /** The deployment status AWS last reported to a step (`AVAILABLE`, `SWITCHOVER_COMPLETED`…); null before any. */
  status: string | null;
  /** Where the green side answers once AWS reports it available; null until then. */
  greenEndpoint: string | null;
  /** The renamed old instances or cluster kept after the switchover. */
  blueSideIdentifiers: string[];
  switchoverApprovedBy: string | null;
  switchoverApprovedAtUtc: string | null;
}

function textOf(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** The operation kind that creates a blue/green deployment and owns its lifecycle. */
export const MAJOR_UPGRADE_KIND = "major_upgrade";

/** The operation kind that deletes a deployment a major upgrade left in place. */
export const BLUE_GREEN_CLEANUP_KIND = "blue_green_cleanup";

/**
 * The blue/green deployment of a major upgrade, or `null` before one is created. Any other operation reads
 * `null` even when its plan names a deployment (a cleanup names the one it deletes, which it never creates):
 * its id and name and green endpoint from the plan, and the status from the latest step output that has one.
 *
 * @example
 * blueGreenDeploymentOf(majorUpgrade)?.greenEndpoint; // "demo-orders-green-abc.eu-west-2.rds.amazonaws.com"
 * blueGreenDeploymentOf(cleanup); // null
 */
export function blueGreenDeploymentOf(operation: OperationDetail): BlueGreenDeployment | null {
  if (operation.operation_kind !== MAJOR_UPGRADE_KIND) return null;
  const plan = operation.plan;
  const deploymentId = textOf(plan.deployment_id);
  if (deploymentId === null) return null;
  const reportedStatuses = operation.step_attempts
    .map((attempt) => textOf(attempt.output.deployment_status))
    .filter((status): status is string => status !== null);
  return {
    deploymentId,
    deploymentName: textOf(plan.deployment_name),
    status: reportedStatuses.at(-1) ?? null,
    greenEndpoint: textOf(plan.green_endpoint),
    blueSideIdentifiers: stringsOf(plan.blue_side_identifiers),
    switchoverApprovedBy: textOf(plan.switchover_approved_by),
    switchoverApprovedAtUtc: textOf(plan.switchover_approved_at_utc),
  };
}

/**
 * The deployment a blue/green cleanup deletes (`plan.deployment_id`), or `null` for any other operation.
 *
 * @example
 * cleanupDeploymentIdOf(cleanup); // "bgd-abc123"
 */
export function cleanupDeploymentIdOf(operation: OperationDetail): string | null {
  if (operation.operation_kind !== BLUE_GREEN_CLEANUP_KIND) return null;
  return textOf(operation.plan.deployment_id);
}

/**
 * The action review of an operation (`plan.action_review`), or `null` when it has none.
 *
 * @example
 * actionReviewOf(failover)?.what; // ["fail over demo-aurora from demo-aurora-writer to demo-aurora-reader"]
 */
export function actionReviewOf(operation: OperationDetail): ActionReview | null {
  const review = objectOf(operation.plan.action_review);
  if (review === null) return null;
  return {
    what: stringsOf(review.what),
    identifiers: stringsOf(review.identifiers),
    requiresReboot: review.requires_reboot === true,
    downtime: typeof review.downtime === "string" ? review.downtime : "none",
    reconcile: review.reconcile === true,
  };
}

/**
 * An operation's change set split by phase. A two-phase operation (`plan.second_phase`) lists the second
 * phase's rows last in `change_set`: those computed after approval (`plan.change_set_phase_2`) once they exist,
 * else those shown at review (`plan.second_phase.change_set`). Any other operation is all first phase.
 *
 * @example
 * phasesOf(decommission); // { firstChangeSet: [protection off], secondPhase: { summary: "delete demo-temp", ... } }
 */
export function phasesOf(operation: OperationDetail): {
  firstChangeSet: ChangeSetEntry[];
  secondPhase: SecondPhase | null;
} {
  const changeSet = operation.change_set ?? [];
  const second = objectOf(operation.plan.second_phase);
  if (second === null) return { firstChangeSet: changeSet, secondPhase: null };
  const phaseTwoRows = operation.plan.change_set_phase_2 ?? second.change_set;
  const secondCount = Math.min(Array.isArray(phaseTwoRows) ? phaseTwoRows.length : 0, changeSet.length);
  return {
    firstChangeSet: changeSet.slice(0, changeSet.length - secondCount),
    secondPhase: {
      summary: typeof second.summary === "string" ? second.summary : "the second change",
      what: stringsOf(second.what),
      changeSet: changeSet.slice(changeSet.length - secondCount),
    },
  };
}

/** A titled part of a review; it is a named region, so a screen reader can jump between the parts. */
export function ReviewSection({
  title,
  children,
  className,
}: {
  title: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className={className ?? "space-y-2"}>
      <h3 id={headingId} className="text-sm font-medium">
        {title}
      </h3>
      {children}
    </section>
  );
}

/** A backend sentence (`fail over demo-aurora …`) as it starts a line: with a capital. */
function asSentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * What an action will do: its sentences in order, the resources it acts on, the reboot and downtime to expect,
 * and whether the files are re-rendered afterwards.
 *
 * @example
 * <ActionReviewView review={actionReviewOf(operation)} />
 */
export function ActionReviewView({ review }: { review: ActionReview }) {
  return (
    <ReviewSection title="What will happen">
      <ol className="space-y-1.5">
        {review.what.map((sentence, index) => (
          <li key={sentence} className="flex items-start gap-2 text-sm">
            <span
              aria-hidden="true"
              className="mt-0.5 inline-flex size-4 shrink-0 items-center justify-center rounded-full border border-accent text-[10px] text-accent"
            >
              {index + 1}
            </span>
            <span className="min-w-0 [overflow-wrap:anywhere]">{asSentence(sentence)}</span>
          </li>
        ))}
      </ol>
      {review.identifiers.length > 0 && (
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
          Acts on
          {review.identifiers.map((identifier) => (
            <span key={identifier} className="rounded-md border border-hairline px-1.5 font-mono text-ink">
              {identifier}
            </span>
          ))}
        </p>
      )}
      <p className="text-sm">
        {review.requiresReboot && <>A reboot is required. </>}
        Expected downtime: <strong>{review.downtime}</strong>.
      </p>
      {review.reconcile && (
        <p className="text-xs text-muted">
          Afterwards the resource's files are re-rendered and committed, so the record matches what AWS has.
        </p>
      )}
    </ReviewSection>
  );
}

/**
 * The change a two-phase operation makes after the approved one, under the same approval: "Then: <summary>",
 * what it touches, and its change set rows when they are known.
 *
 * @example
 * <SecondPhaseView phase={phasesOf(operation).secondPhase} />
 */
export function SecondPhaseView({ phase }: { phase: SecondPhase }) {
  return (
    <ReviewSection title={`Then: ${phase.summary}`} className="space-y-2 border-l-2 border-accent/40 pl-3">
      <p className="text-xs text-muted">Once the change above is applied, under this same approval:</p>
      {phase.what.length > 0 && (
        <ul className="space-y-1 font-mono text-sm">
          {phase.what.map((item) => (
            <li key={item} className="[overflow-wrap:anywhere]">
              {item}
            </li>
          ))}
        </ul>
      )}
      {phase.changeSet.length > 0 && <ChangeSetView changeSet={phase.changeSet} />}
    </ReviewSection>
  );
}

/**
 * Where the green side of a major upgrade answers, with a Copy button, so the operator can test it before
 * approving the switchover.
 *
 * @example
 * <GreenEndpointView endpoint={deployment.greenEndpoint} />
 */
export function GreenEndpointView({ endpoint }: { endpoint: string }) {
  return (
    <ReviewSection title="Green endpoint">
      <p className="text-xs text-muted">
        The green side runs the new version here. Connect to it and test it before you switch production over.
      </p>
      <CopyableValue value={endpoint} />
    </ReviewSection>
  );
}

interface ReviewStepProps {
  operation: OperationDetail;
  mode: string;
  /**
   * Called with whether the review content has actually rendered: the plan summary has loaded (Terraform
   * mode), the operation carries an action review, the change set has entries (of either phase), or the
   * operation names the instances it will reboot. Approval must wait for it.
   */
  onReadyChange: (ready: boolean) => void;
}

/** Instances the plan says will be rebooted; an operation such as a reboot carries them instead of a change set. */
function rebootTargetsOf(operation: OperationDetail): string[] {
  return stringsOf(operation.plan.reboot_identifiers);
}

/**
 * What the operator reviews before approving. An action (reboot, failover, maintenance, snapshot, promote)
 * shows what will happen in sentences. Otherwise Terraform mode shows the latest plan, and direct mode the
 * change set with reboot and downtime flags; an operation with neither, such as a group reboot, shows the
 * instances it will reboot. A two-phase operation (decommission) then shows what follows under the same
 * approval ("Then: …") with its rows. At a major upgrade's switchover pause the review also gives the green
 * endpoint to test, with a Copy button. All show who else manages the resource, the downtime and the
 * preflight checks.
 *
 * @example
 * <ReviewStep operation={operation} mode="direct" onReadyChange={setReviewReady} />
 */
export function ReviewStep({ operation, mode, onReadyChange }: ReviewStepProps) {
  const latestPlan = operation.terraform_runs.filter((run) => run.run_kind === "plan").at(-1);
  const changeSet = operation.change_set ?? [];
  const { firstChangeSet, secondPhase } = phasesOf(operation);
  const actionReview = actionReviewOf(operation);
  const rebootTargets = rebootTargetsOf(operation);
  const greenEndpoint =
    approvalStageOf(operation) === SWITCHOVER_APPROVAL_STAGE ? textOf(operation.plan.green_endpoint) : null;
  const showPlan = actionReview === null && mode !== "direct" && latestPlan !== undefined;
  const showChangeSet =
    actionReview === null &&
    !showPlan &&
    (changeSet.length > 0 || (mode === "direct" && rebootTargets.length === 0));
  const showRebootTargets =
    actionReview === null && !showPlan && changeSet.length === 0 && rebootTargets.length > 0;
  const [loadedPlanRunId, setLoadedPlanRunId] = useState<number | null>(null);
  const ready =
    actionReview !== null ||
    (showPlan ? loadedPlanRunId === latestPlan.id : showChangeSet ? changeSet.length > 0 : showRebootTargets);
  useEffect(() => {
    onReadyChange(ready);
  }, [ready, onReadyChange]);
  return (
    <div className="space-y-4">
      {operation.foreign_owner_banner && (
        <p className="rounded-md border border-status-amber/40 bg-status-amber/10 p-2 text-sm">
          This resource is managed by <strong>{operation.foreign_owner_banner}</strong>. Applying this change
          makes that code drift; update it with the attributes below.
        </p>
      )}
      {actionReview ? (
        <ActionReviewView review={actionReview} />
      ) : showPlan ? (
        <ReviewSection title="Terraform plan">
          <PlanViewer runId={latestPlan.id} onLoaded={setLoadedPlanRunId} />
        </ReviewSection>
      ) : showChangeSet ? (
        <ReviewSection title="Change set">
          {firstChangeSet.length > 0 ? (
            <ChangeSetView changeSet={firstChangeSet} />
          ) : secondPhase && changeSet.length > 0 ? (
            <p className="text-sm text-muted">Nothing needs changing first.</p>
          ) : (
            <p className="text-sm text-muted">Computing…</p>
          )}
        </ReviewSection>
      ) : showRebootTargets ? (
        <ReviewSection title="Will reboot">
          <ul aria-label="Instances to reboot" className="space-y-1 font-mono text-sm">
            {rebootTargets.map((identifier) => (
              <li key={identifier}>{identifier}</li>
            ))}
          </ul>
        </ReviewSection>
      ) : (
        <p className="text-sm text-muted">Waiting for the plan…</p>
      )}
      {greenEndpoint && <GreenEndpointView endpoint={greenEndpoint} />}
      {secondPhase && <SecondPhaseView phase={secondPhase} />}
      {actionReview === null && (
        <p className="text-sm">
          {operation.requires_reboot && <>A reboot is required. </>}
          Expected downtime: <strong>{operation.downtime}</strong>.
        </p>
      )}
      <PreflightChecklist reports={operation.diagnostic_reports} />
    </div>
  );
}
