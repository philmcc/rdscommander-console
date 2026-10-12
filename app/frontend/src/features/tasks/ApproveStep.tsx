import { useCallback, useId, useState } from "react";
import type { OperationDetail } from "@/api/types";
import { IdentifierConfirm } from "@/components/IdentifierConfirm";
import { Button } from "@/components/ui/button";
import { approvalStageOf, SWITCHOVER_APPROVAL_STAGE } from "./ReviewStep";

interface ApproveStepProps {
  operation: OperationDetail;
  /** False until the review content has rendered; Approve stays disabled, so nothing is approved unseen. */
  reviewReady: boolean;
  /** True while a decision is being sent; both buttons are disabled. */
  busy?: boolean;
  /**
   * Called with the identifier as the operator typed it, which the approval sends for the server to check, or
   * with null when the operation asks for no identifier.
   */
  onApprove: (confirmationText: string | null) => void;
  onReject: () => void;
}

/**
 * Identifier confirmation when the operation requires it, then approve or reject. Approve stays disabled
 * until the review has rendered and, when the operation requires it, the exact identifier has been typed.
 * At a major upgrade's second pause the button says what approving does: "Approve switchover". Approving
 * hands on the identifier as typed, so the server can refuse an approval whose text does not match.
 *
 * @example
 * <ApproveStep operation={operation} reviewReady onApprove={approve} onReject={reject} />
 */
export function ApproveStep({ operation, reviewReady, busy = false, onApprove, onReject }: ApproveStepProps) {
  const hintId = useId();
  const requiresIdentifier = Boolean(operation.approval_required_text);
  const [confirmed, setConfirmed] = useState(!requiresIdentifier);
  const [typedIdentifier, setTypedIdentifier] = useState("");
  const recordConfirmation = useCallback((ok: boolean, typedText: string) => {
    setConfirmed(ok);
    setTypedIdentifier(typedText);
  }, []);
  return (
    <div className="space-y-3">
      {operation.approval_required_text && (
        <IdentifierConfirm identifier={operation.approval_required_text} onConfirmed={recordConfirmation} />
      )}
      {!reviewReady && (
        <p id={hintId} className="text-sm text-muted">
          Approval opens once the review above has loaded.
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onReject} disabled={busy}>
          Reject
        </Button>
        <Button
          onClick={() => onApprove(requiresIdentifier ? typedIdentifier : null)}
          disabled={!confirmed || !reviewReady || busy}
          aria-describedby={reviewReady ? undefined : hintId}
          className={
            operation.approval_required_text ? "bg-status-red text-white hover:bg-status-red/90" : undefined
          }
        >
          {approvalStageOf(operation) === SWITCHOVER_APPROVAL_STAGE
            ? "Approve switchover"
            : "Approve and run"}
        </Button>
      </div>
    </div>
  );
}
