import type { OperationProgress } from "@/api/types";

/**
 * Where an active operation is: the step it is on out of the total, and that step's name in monospace.
 * Renders inline text for the caller's own element, which sets the colour and size.
 *
 * @example
 * <span className="text-xs text-muted"><OperationProgressText progress={operation.progress} /></span>
 */
export function OperationProgressText({ progress }: { progress: OperationProgress }) {
  return (
    <>
      {`step ${Math.min(progress.completed_steps + 1, progress.total_steps)} of ${progress.total_steps}`}
      {progress.current_step && (
        <>
          {" · "}
          <span className="font-mono">{progress.current_step}</span>
        </>
      )}
    </>
  );
}
