import { useEffect, useId } from "react";
import { Link } from "react-router";
import { useEnvironment } from "@/app/EnvironmentContext";
import { CopyableValue } from "@/components/CopyableValue";
import { StatusBadge } from "@/components/StatusBadge";
import { StepTimeline } from "@/components/StepTimeline";
import { Button } from "@/components/ui/button";
import { type Fact, FactsGrid } from "@/features/resources/FactsGrid";
import {
  ActionReviewView,
  actionReviewOf,
  type BlueGreenDeployment,
  blueGreenDeploymentOf,
  ChangeSetView,
  cleanupDeploymentIdOf,
  PreflightChecklist,
  phasesOf,
  SecondPhaseView,
  ReviewSection as Section,
} from "@/features/tasks/ReviewStep";
import { useTask } from "@/features/tasks/TaskContext";
import { PlanViewer } from "@/features/terraform/PlanViewer";
import { formatUtc } from "@/lib/formatUtc";
import { ExecutionRecordView } from "./ExecutionRecordView";
import { OperationProgressText } from "./OperationProgressText";
import { OperationTitle } from "./OperationTitle";
import { useOperationPolling } from "./useOperationPolling";

/**
 * A major upgrade's blue/green deployment: its id and name, the status AWS last reported ("being created"
 * before the first), the green endpoint with a Copy button once it exists, who approved the switchover, and
 * the blue side kept after it.
 *
 * @example
 * <BlueGreenDeploymentSection deployment={blueGreenDeploymentOf(operation)} />
 */
function BlueGreenDeploymentSection({ deployment }: { deployment: BlueGreenDeployment }) {
  const facts: Fact[] = [
    { label: "Deployment", value: deployment.deploymentId },
    ...(deployment.deploymentName ? [{ label: "Name", value: deployment.deploymentName }] : []),
    {
      label: "Status",
      value: deployment.status ?? (deployment.greenEndpoint ? null : "being created"),
      mono: deployment.status !== null,
    },
    {
      label: "Green endpoint",
      value: deployment.greenEndpoint ? (
        <CopyableValue value={deployment.greenEndpoint} />
      ) : (
        "once the green side is available"
      ),
      mono: false,
    },
    ...(deployment.switchoverApprovedBy
      ? [
          {
            label: "Switchover approved",
            value: `by ${deployment.switchoverApprovedBy}${
              deployment.switchoverApprovedAtUtc ? ` at ${formatUtc(deployment.switchoverApprovedAtUtc)}` : ""
            }`,
            mono: false,
          },
        ]
      : []),
    ...(deployment.blueSideIdentifiers.length > 0
      ? [{ label: "Blue side kept", value: deployment.blueSideIdentifiers.join(", ") }]
      : []),
  ];
  return (
    <Section title="Blue/green deployment">
      <FactsGrid facts={facts} />
    </Section>
  );
}

/**
 * Full view of one operation: its state and progress, steps, what an action will do, a major upgrade's
 * blue/green deployment (a cleanup names the deployment it deletes in one line), the change set (and a two-phase operation's second phase after it), plan,
 * diagnostics and every execution record. An operation waiting for approval offers "Review and approve",
 * which opens the task panel on it, so every approval goes through the panel's identifier confirmation.
 * The operation is read again every two seconds while it is active; a failed read leaves the last answer on
 * screen under a banner and is retried. Opening an operation of another environment selects that
 * environment in the shell, as a resource page does for its resource.
 *
 * @example
 * <OperationDetail operationId={operationId} />
 */
export function OperationDetail({ operationId }: { operationId: string }) {
  const { operation, error } = useOperationPolling(operationId);
  const { open } = useTask();
  const { environments, select } = useEnvironment();
  const titleId = useId();

  // The shell shows the environment of the operation being looked at.
  const operationEnvironment = operation?.environment_slug;
  useEffect(() => {
    if (
      operationEnvironment &&
      environments.some((environment) => environment.slug === operationEnvironment)
    ) {
      select(operationEnvironment);
    }
  }, [operationEnvironment, environments, select]);

  if (!operation) {
    return error ? (
      <p role="alert" className="text-sm text-status-red">
        Could not load the operation: {error}
      </p>
    ) : (
      <p className="text-muted">Loading…</p>
    );
  }

  const changeSet = operation.change_set ?? [];
  const { firstChangeSet, secondPhase } = phasesOf(operation);
  const actionReview = actionReviewOf(operation);
  const deployment = blueGreenDeploymentOf(operation);
  const deletedDeploymentId = cleanupDeploymentIdOf(operation);
  const planRuns = operation.terraform_runs.filter((run) => run.run_kind === "plan");
  // A failed plan run has no summary, and the plan viewer would wait for one forever.
  const latestPlan = planRuns.filter((run) => run.plan_summary !== null).at(-1);
  const resourceIdentifier = operation.resource_arn?.split(":").at(-1);
  return (
    <article aria-labelledby={titleId} className="space-y-5 rounded-lg border border-hairline bg-surface p-4">
      {error && (
        <p role="alert" className="text-sm text-status-red">
          Could not refresh the operation: {error}. Showing what was last read.
        </p>
      )}
      <header className="space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <h2 id={titleId} className="min-w-0 break-words text-lg font-semibold">
            <OperationTitle title={operation.title} />
          </h2>
          {operation.state === "AWAITING_APPROVAL" && (
            <Button
              onClick={() =>
                open({
                  kind: "",
                  operationId: operation.id,
                  environmentSlug: operation.environment_slug ?? undefined,
                })
              }
            >
              Review and approve
            </Button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          <StatusBadge status={operation.state} />
          {operation.progress && (
            <span>
              <OperationProgressText progress={operation.progress} />
            </span>
          )}
          <span className="font-mono">{operation.operation_kind}</span>
          {operation.environment_slug && <span>{operation.environment_slug}</span>}
          {operation.target_key && <span className="font-mono">{operation.target_key}</span>}
          <span>by {operation.created_by_operator_name}</span>
          <span>{new Date(operation.created_at_utc).toLocaleString()}</span>
          {operation.resource_id && resourceIdentifier && (
            <Link
              to={`/resources/${operation.resource_id}`}
              className="font-mono text-accent hover:underline"
            >
              {resourceIdentifier}
            </Link>
          )}
        </div>
        {operation.failure_summary && <p className="text-sm text-status-red">{operation.failure_summary}</p>}
      </header>
      <Section title="Steps">
        {operation.step_attempts.length === 0 ? (
          <p className="text-sm text-muted">No step has started yet.</p>
        ) : (
          <StepTimeline stepNames={[]} attempts={operation.step_attempts} />
        )}
      </Section>
      {actionReview && <ActionReviewView review={actionReview} />}
      {deployment && <BlueGreenDeploymentSection deployment={deployment} />}
      {deletedDeploymentId && (
        <p className="text-sm [overflow-wrap:anywhere]">
          Deleting deployment <span className="font-mono">{deletedDeploymentId}</span>
        </p>
      )}
      {firstChangeSet.length > 0 && (
        <Section title="Change set">
          <ChangeSetView changeSet={firstChangeSet} />
        </Section>
      )}
      {secondPhase && <SecondPhaseView phase={secondPhase} />}
      {changeSet.length > 0 && (
        <p className="text-sm">
          {operation.requires_reboot && <>A reboot is required. </>}
          Expected downtime: <strong>{operation.downtime}</strong>.
        </p>
      )}
      {operation.terraform_runs.length > 0 && (
        <Section title="Terraform">
          <ul className="space-y-1 text-sm">
            {operation.terraform_runs.map((run) => (
              <li key={run.id} className="flex flex-wrap items-center gap-2">
                <span className="font-mono">{run.run_kind}</span>
                <span className="text-muted">exit {run.exit_code}</span>
              </li>
            ))}
          </ul>
          {latestPlan && <PlanViewer runId={latestPlan.id} />}
          {!latestPlan && planRuns.length > 0 && <p className="text-sm text-muted">No plan was produced</p>}
        </Section>
      )}
      <PreflightChecklist reports={operation.diagnostic_reports} heading="Diagnostics" />
      <Section title={`Execution records (${operation.execution_records.length})`}>
        <div className="space-y-2">
          {operation.execution_records.map((record) => (
            <ExecutionRecordView key={record.id} record={record} />
          ))}
          {operation.execution_records.length === 0 && (
            <p className="text-sm text-muted">Nothing has run yet.</p>
          )}
        </div>
      </Section>
    </article>
  );
}
