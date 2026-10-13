import { useEffect, useRef, useState } from "react";
import { apiGet } from "@/api/client";
import type { OperationDetail } from "@/api/types";
import { describeError } from "@/lib/describeError";

const ACTIVE_STATES = new Set(["QUEUED", "RUNNING", "AWAITING_APPROVAL"]);

interface LoadedOperation {
  operationId: string;
  operation: OperationDetail;
}

interface LoadFailure {
  operationId: string;
  message: string;
}

/**
 * Poll one operation while it is active; reloads immediately when `version` changes.
 *
 * `operation` and `error` belong to `operationId`: as soon as the id changes both read `null` until the new
 * operation answers, so one operation is never shown under the address of another, and an answer that
 * arrives after the change is ignored.
 *
 * A failed read does not end the updates: `operation` keeps the last answer, `error` says why the read
 * failed, and while that last answer is active the read is retried at the same interval, which clears
 * `error` when it works. A first read that fails, or a failure after the operation was known to have
 * finished, is not retried, because nothing would change on its own.
 *
 * @example
 * const { operation, error } = useOperationPolling(operationId);
 */
export function useOperationPolling(operationId: string | null, version = 0, intervalMs = 2000) {
  const [loaded, setLoaded] = useState<LoadedOperation | null>(null);
  const [failure, setFailure] = useState<LoadFailure | null>(null);
  // The latest answer's state, whichever effect run read it, so a retry after a failure knows whether to go on.
  const lastKnownState = useRef<{ operationId: string; state: string } | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `version` deliberately forces a reload after approve/reject
  useEffect(() => {
    if (!operationId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const operation = await apiGet<OperationDetail>(`/api/v1/operations/${operationId}`);
        if (cancelled) return;
        lastKnownState.current = { operationId, state: operation.state };
        setLoaded({ operationId, operation });
        setFailure(null);
      } catch (caught) {
        if (cancelled) return;
        setFailure({ operationId, message: describeError(caught) });
      }
      const known = lastKnownState.current;
      if (known?.operationId === operationId && ACTIVE_STATES.has(known.state)) {
        timer = setTimeout(tick, intervalMs);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [operationId, version, intervalMs]);
  return {
    operation: loaded && loaded.operationId === operationId ? loaded.operation : null,
    error: failure && failure.operationId === operationId ? failure.message : null,
  };
}
