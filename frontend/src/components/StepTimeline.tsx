import type { StepAttemptSummary } from "@/api/types";
import { StatusBadge } from "./StatusBadge";

interface StepTimelineProps {
  /** Step names in execution order; when empty, the steps that have attempts are listed instead. */
  stepNames: string[];
  attempts: StepAttemptSummary[];
}

/**
 * One row per step in order, with the latest attempt's outcome: its outcome status, "running" while it has
 * none yet, or "pending" for a step not reached.
 *
 * @example
 * <StepTimeline stepNames={["create_snapshot", "apply_parameters"]} attempts={operation.step_attempts} />
 */
export function StepTimeline({ stepNames, attempts }: StepTimelineProps) {
  const latest = new Map<string, StepAttemptSummary>();
  for (const attempt of attempts) {
    const earlier = latest.get(attempt.step_name);
    if (!earlier || attempt.attempt_number >= earlier.attempt_number) latest.set(attempt.step_name, attempt);
  }
  const names = stepNames.length ? stepNames : [...latest.keys()];
  return (
    <ol className="space-y-1">
      {names.map((name) => {
        const attempt = latest.get(name);
        return (
          <li key={name} className="flex items-center gap-3 text-sm">
            <StatusBadge
              status={attempt ? (attempt.outcome_status ?? "running") : "pending"}
              className="min-w-28 justify-center"
            />
            <span className="font-mono">{name}</span>
            {attempt?.detail && <span className="truncate text-muted">{attempt.detail}</span>}
          </li>
        );
      })}
    </ol>
  );
}
