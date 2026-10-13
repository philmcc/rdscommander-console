import { useCallback, useState } from "react";
import { ApiError, apiPost, type OperationApproveRequest } from "@/api/client";
import type { OperationSummary } from "@/api/types";
import { useOperationPolling } from "@/features/operations/useOperationPolling";
import { approvalStageOf } from "./ReviewStep";

/** How often the open panel reloads the operation; short because the operator is watching it. */
const PANEL_POLL_INTERVAL_MILLISECONDS = 500;

/** The decision an operator made on an operation awaiting approval. */
export type OperationDecision = "approve" | "reject";

/**
 * Why a decision was refused, in one line: the server's reason and, when it gave one, "Fix: <suggestion>".
 *
 * @example
 * refusalMessageOf(new ApiError(409, "the operation waits at switchover", [], "approval_stage_mismatch", "review the switchover"));
 * // "the operation waits at switchover. Fix: review the switchover"
 */
function refusalMessageOf(caught: unknown): string {
  if (caught instanceof ApiError) {
    if (!caught.fixSuggestion) return caught.detail;
    return `${caught.detail.replace(/[.\s]+$/, "")}. Fix: ${caught.fixSuggestion}`;
  }
  return caught instanceof Error ? caught.message : String(caught);
}

/** A decision that was accepted, with the pause it answered (`approvalStageOf`, empty for a single pause). */
interface AcceptedDecision {
  verb: OperationDecision;
  approvalStage: string;
}

/**
 * Follows one operation and exposes approve and reject.
 *
 * `busy` is true while a decision is being sent, and `decision` records the one that was accepted, so the
 * panel can leave the approval controls before the next poll reports the new state. A decision answers one
 * pause: once the operation stops at a later one (a major upgrade's switchover after its green copy),
 * `decision` reads null again and the panel shows that pause's review.
 *
 * `approve` sends the pause it approves (`plan.approval_stage`, null for a single pause) and the identifier
 * as typed (null when none is asked for). The server refuses an approval made on a view of a pause that has
 * passed, or with text that is not the identifier; `error` then says why and how to fix it, and the
 * operation is read again so the panel shows the pause it is actually at.
 *
 * @example
 * const { operation, approve, reject, busy, decision, error } = useOperationDriver(operationId);
 * approve("demo-orders"); // at a switchover that asks for the identifier
 */
export function useOperationDriver(operationId: string | null) {
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState<AcceptedDecision | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const { operation, error } = useOperationPolling(operationId, version, PANEL_POLL_INTERVAL_MILLISECONDS);
  const currentStage = operation ? approvalStageOf(operation) : "";
  const decide = useCallback(
    async (verb: OperationDecision, approvalStage: string, confirmationText: string | null) => {
      if (!operationId) return;
      setBusy(true);
      setActionError(null);
      const approval: OperationApproveRequest = {
        approval_stage: approvalStage === "" ? null : approvalStage,
        confirmation_text: confirmationText,
      };
      try {
        await apiPost<OperationSummary>(
          `/api/v1/operations/${operationId}/${verb}`,
          verb === "approve" ? approval : undefined,
        );
        setAccepted({ verb, approvalStage });
      } catch (caught) {
        setActionError(refusalMessageOf(caught));
      } finally {
        setBusy(false);
        // Reload either way: a refused decision usually means the state changed somewhere else.
        setVersion((v) => v + 1);
      }
    },
    [operationId],
  );
  return {
    operation,
    error: actionError ?? error,
    busy,
    decision: accepted?.approvalStage === currentStage ? accepted.verb : null,
    approve: (confirmationText: string | null) => decide("approve", currentStage, confirmationText),
    reject: () => decide("reject", currentStage, null),
  };
}
