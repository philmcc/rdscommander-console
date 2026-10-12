import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { ApiError, apiPost } from "@/api/client";
import type { DefinitionSummary, OperationCreatedResponse, OperationDetail } from "@/api/types";
import { environmentHue, useEnvironment } from "@/app/EnvironmentContext";
import { EnvironmentChip } from "@/components/EnvironmentChip";
import { StatusBadge } from "@/components/StatusBadge";
import { StepTimeline } from "@/components/StepTimeline";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { OperationTitle } from "@/features/operations/OperationTitle";
import { cn } from "@/lib/utils";
import { ApproveStep } from "./ApproveStep";
import { approvalPauseOf, REVIEW_SUBMIT_LABEL } from "./approvalPause";
import { loadCatalogue } from "./catalogue";
import { sortProblems } from "./problems";
import { approvalStageOf, PreflightChecklist, ReviewStep } from "./ReviewStep";
import { type ObjectSchema, optionsAsShown, SchemaForm } from "./SchemaForm";
import { type TaskRequest, useTask } from "./TaskContext";
import { type OperationDecision, useOperationDriver } from "./useOperationDriver";

/** Where the operator is: the options form, waiting for the operation, approving, or watching it run. */
type Step = "options" | "loading" | "approve" | "progress";

/**
 * The steps of the header; running replaces them with the timeline. A task that pauses has three: while the
 * operation waits for approval the current step is Review, since the approve controls close the review, and
 * Approve is current only once the operator has approved, which is when the steps give way to the timeline.
 * A task that runs to the end once submitted has no review to promise: Options, then Run.
 */
const PAUSING_HEADER_STEPS = ["Options", "Review", "Approve"] as const;
const RUNNING_HEADER_STEPS = ["Options", "Run"] as const;
/** The step after the options, in either list: Review for a task that pauses, Run for one that does not. */
const AFTER_OPTIONS_STEP_INDEX = 1;

/** Kinds that act on no environment, so the selected environment must not be attached to them. */
export const ENVIRONMENT_FREE_KINDS = new Set(["environment_import"]);

/** The task that adds an environment, which the console's list of environments must then pick up. */
const ENVIRONMENT_IMPORT_KIND = "environment_import";

const STATE_LABELS: Record<string, string> = {
  QUEUED: "Queued",
  RUNNING: "Running",
  SUCCEEDED: "Succeeded",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

/** The state in words; right after a decision the state may still be the old one, so say what is happening. */
function statusLabel(state: string, decision: OperationDecision | null): string {
  if (STATE_LABELS[state]) return STATE_LABELS[state];
  if (state === "AWAITING_APPROVAL" && decision) return decision === "approve" ? "Approving" : "Cancelling";
  return state;
}

function stepOf(
  operationId: string | null,
  operation: OperationDetail | null,
  decision: OperationDecision | null,
): Step {
  if (!operationId) return "options";
  if (decision) return "progress";
  if (!operation) return "loading";
  return operation.state === "AWAITING_APPROVAL" ? "approve" : "progress";
}

/** Why the server refused the options: a message for the whole form, per-field messages, or both. */
interface Rejection {
  detail: string | null;
  fields: Record<string, string>;
  general: string[];
}

const NO_REJECTION: Rejection = { detail: null, fields: {}, general: [] };

function HeaderSteps({ steps, current }: { steps: readonly string[]; current: number }) {
  return (
    <ol aria-label="Steps" className="mt-2 flex gap-4 text-xs">
      {steps.map((label, index) => (
        <li
          key={label}
          aria-current={index === current ? "step" : undefined}
          className={cn("flex items-center gap-1", index === current ? "text-ink" : "text-muted")}
        >
          <span
            aria-hidden="true"
            className={cn(
              "inline-flex size-4 items-center justify-center rounded-full border text-[10px]",
              index === current ? "border-accent text-accent" : "border-hairline",
            )}
          >
            {index < current ? "✓" : index + 1}
          </span>
          {label}
          {index < current && <span className="sr-only"> (done)</span>}
        </li>
      ))}
    </ol>
  );
}

/**
 * The right-hand sheet every operation runs in: options, review and approve, then progress. Its fixed header
 * carries the environment's chip, so production stays marked however far the review scrolls. Mounted once by
 * `TaskProvider`; renders nothing while no task is open.
 */
export function TaskPanel() {
  const { current, close } = useTask();
  if (!current) return null;
  return <TaskSheet request={current} onClose={close} />;
}

function TaskSheet({ request, onClose }: { request: TaskRequest; onClose: () => void }) {
  const { environments, selected, reload: reloadEnvironments } = useEnvironment();
  const [definitions, setDefinitions] = useState<DefinitionSummary[] | null>(null);
  const [catalogueError, setCatalogueError] = useState<string | null>(null);
  const [options, setOptions] = useState<Record<string, unknown>>(request.initialOptions ?? {});
  const [rejection, setRejection] = useState<Rejection>(NO_REJECTION);
  const [operationId, setOperationId] = useState<string | null>(request.operationId ?? null);
  const [creating, setCreating] = useState(false);
  const [reviewReady, setReviewReady] = useState(false);
  const { operation, approve, reject, busy, decision, error } = useOperationDriver(operationId);
  const problemsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    loadCatalogue().then(
      (loaded) => {
        if (!cancelled) setDefinitions(loaded);
      },
      (caught: unknown) => {
        if (!cancelled) setCatalogueError(caught instanceof Error ? caught.message : String(caught));
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  // A long form can hide the summary above it, so bring it into view when the server refuses the options.
  useEffect(() => {
    if (rejection !== NO_REJECTION) problemsRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [rejection]);

  // Other features open the panel with an empty kind and the id of an existing operation.
  const kind = request.kind || operation?.operation_kind || "";
  const definition = definitions?.find((d) => d.kind === kind);

  // An import adds an environment, so once its panel closes (by whatever way, from whatever entry point)
  // the switcher, the selects and everything else derived from bootstrap are brought up to date. A failed
  // read leaves the environments the console already has.
  const isImport = kind === ENVIRONMENT_IMPORT_KIND;
  useEffect(() => {
    if (!isImport) return;
    return () => {
      reloadEnvironments().catch(() => undefined);
    };
  }, [isImport, reloadEnvironments]);
  const environmentSlug =
    operation?.environment_slug ??
    request.environmentSlug ??
    (ENVIRONMENT_FREE_KINDS.has(kind) ? undefined : selected?.slug);
  const environment = environments.find((e) => e.slug === environmentSlug);
  const step = stepOf(operationId, operation, decision);
  const schema = definition?.options_schema as ObjectSchema | undefined;
  const pause = definition
    ? approvalPauseOf(definition, options)
    : { pauses: true, submitLabel: REVIEW_SUBMIT_LABEL };

  const submit = async () => {
    if (!definition) return;
    setCreating(true);
    setRejection(NO_REJECTION);
    try {
      const created = await apiPost<OperationCreatedResponse>("/api/v1/operations", {
        kind: definition.kind,
        environment_slug: environmentSlug ?? null,
        resource_id: request.resourceId ?? null,
        options: optionsAsShown(schema, options),
        idempotency_key: `ui-${definition.kind}-${Date.now()}`,
      });
      setOperationId(created.operation_id);
    } catch (caught) {
      if (caught instanceof ApiError && caught.problems.length > 0) {
        const sorted = sortProblems(caught.problems, Object.keys(schema?.properties ?? {}));
        setRejection({ detail: null, fields: sorted.byField, general: sorted.general });
      } else {
        setRejection({
          detail: caught instanceof Error ? caught.message : String(caught),
          fields: {},
          general: [],
        });
      }
    } finally {
      setCreating(false);
    }
  };

  const title =
    definition?.title ?? (definitions === null && !catalogueError ? "Loading task…" : kind || "Task");
  const subject = request.resourceIdentifier;
  const generalMessages = rejection.detail ? [rejection.detail] : rejection.general;
  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-[560px]">
        <SheetHeader className="border-b border-hairline p-4 pr-12">
          {/* The header never scrolls, so the environment (production's red border and word) stays in view
              down to the approve button of the longest review. */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <SheetTitle>{title}</SheetTitle>
            {environment && (
              <EnvironmentChip
                environment={environment}
                hue={environmentHue(environments, environment.slug)}
              />
            )}
          </div>
          <SheetDescription>
            {definition?.summary}
            {subject && (
              <>
                {definition?.summary ? " · " : ""}
                <span className="font-mono">{subject}</span>
              </>
            )}
            {!subject && operation?.title && (
              <>
                {definition?.summary ? " · " : ""}
                <OperationTitle title={operation.title} />
              </>
            )}
          </SheetDescription>
          {step === "progress" && operation ? (
            <div className="mt-2 flex items-center gap-2">
              <StatusBadge status={operation.state} />
              <span className="text-sm">{statusLabel(operation.state, decision)}</span>
            </div>
          ) : (
            <HeaderSteps
              steps={pause.pauses ? PAUSING_HEADER_STEPS : RUNNING_HEADER_STEPS}
              current={step === "options" ? 0 : AFTER_OPTIONS_STEP_INDEX}
            />
          )}
        </SheetHeader>
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
          {environment?.is_production && (
            <p className="rounded-md border-l-4 border-status-red bg-status-red/10 p-2 text-sm">
              This is <strong>production</strong> ({environment.display_name}).
            </p>
          )}
          {step === "options" && catalogueError && (
            <p role="alert" className="text-sm text-status-red">
              Could not load the task catalogue: {catalogueError}
            </p>
          )}
          {step === "options" && !definition && !catalogueError && (
            <p className="text-sm text-muted">
              {definitions === null ? "Loading…" : `This task is not available: ${kind || "unknown"}.`}
            </p>
          )}
          {step === "options" && definition && schema && (
            <>
              {rejection !== NO_REJECTION && (
                <div
                  ref={problemsRef}
                  role="alert"
                  className="space-y-1 rounded-md border border-status-red/40 p-2 text-sm text-status-red"
                >
                  {Object.keys(rejection.fields).length > 0 && <p>Fix the options marked below.</p>}
                  {generalMessages.length > 0 && (
                    <ul className="space-y-1">
                      {generalMessages.map((message) => (
                        <li key={message}>{message}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              <SchemaForm
                schema={schema}
                value={options}
                onChange={setOptions}
                problems={rejection.fields}
                generalProblems={rejection.general}
                context={{
                  environmentSlug,
                  resourceId: request.resourceId,
                  resourceKind: request.resourceKind,
                  targetKey: request.targetKey,
                  resourceIdentifier: request.resourceIdentifier,
                }}
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <Button onClick={submit} disabled={creating}>
                  {pause.submitLabel}
                </Button>
              </div>
            </>
          )}
          {step === "loading" && (
            <p className="text-sm text-muted">
              {request.operationId ? "Loading the operation…" : "Creating the operation…"}
            </p>
          )}
          {step === "approve" && operation && (
            <>
              {/* Each pause of an operation (a major upgrade's green copy, then its switchover) starts its
                  review and approval afresh: nothing typed or loaded for one counts for the next. */}
              <ReviewStep
                key={`review-${approvalStageOf(operation)}`}
                operation={operation}
                mode={environment?.change_management ?? "terraform"}
                onReadyChange={setReviewReady}
              />
              <ApproveStep
                key={`approve-${approvalStageOf(operation)}`}
                operation={operation}
                reviewReady={reviewReady}
                busy={busy}
                onApprove={approve}
                onReject={reject}
              />
            </>
          )}
          {step === "progress" && operation && (
            <>
              {operation.failure_summary && (
                <p className="text-sm text-status-red">{operation.failure_summary}</p>
              )}
              <StepTimeline stepNames={[]} attempts={operation.step_attempts} />
              {/* Once it runs, a report says why a step failed and how to fix it, not how a preflight went. */}
              <PreflightChecklist reports={operation.diagnostic_reports} heading="Diagnostics" />
              <div className="flex justify-end gap-2">
                <Button variant="outline" asChild>
                  <Link to={`/operations/${operation.id}`} onClick={onClose}>
                    Open operation
                  </Link>
                </Button>
                <Button onClick={onClose}>Close</Button>
              </div>
            </>
          )}
          {error && (
            <p role="alert" className="text-sm text-status-red">
              {error}
            </p>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
