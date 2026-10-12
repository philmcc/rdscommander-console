import { useEffect, useId, useRef, useState } from "react";
import { Input } from "@/components/ui/input";

interface IdentifierConfirmProps {
  /** The exact text the operator must type. */
  identifier: string;
  /**
   * Called with true only while the typed text equals the identifier, and with the text as typed (which the
   * approval sends, so the server checks it too); also called once on mount with false and an empty text.
   */
  onConfirmed: (ok: boolean, typedText: string) => void;
}

/**
 * Destructive and disruptive operations require typing the identifier; nothing destructive is one click.
 *
 * @example
 * <IdentifierConfirm identifier="demo-orders" onConfirmed={setConfirmed} />
 */
export function IdentifierConfirm({ identifier, onConfirmed }: IdentifierConfirmProps) {
  const [typed, setTyped] = useState("");
  const inputId = useId();
  // The latest callback lives in a ref so an inline callback does not re-announce the result on every parent render.
  const latestOnConfirmed = useRef(onConfirmed);
  useEffect(() => {
    latestOnConfirmed.current = onConfirmed;
  });
  useEffect(() => {
    latestOnConfirmed.current(typed === identifier, typed);
  }, [typed, identifier]);
  return (
    <div className="space-y-1 text-sm">
      <label htmlFor={inputId} className="block">
        Type <code className="rounded bg-status-red/10 px-1 text-status-red">{identifier}</code> to confirm
      </label>
      <Input
        id={inputId}
        value={typed}
        onChange={(e) => setTyped(e.target.value)}
        className="font-mono"
        autoComplete="off"
      />
    </div>
  );
}
