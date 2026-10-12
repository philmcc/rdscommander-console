import { useState } from "react";
import { Button } from "@/components/ui/button";

/** What the last press of Copy did: nothing yet, put the value on the clipboard, or found no clipboard. */
type CopyOutcome = "idle" | "copied" | "failed";

/**
 * A value in monospace with a Copy button that puts it on the clipboard, and says whether that worked. A
 * browser without clipboard access (a page served over plain HTTP) is told to select the value instead.
 *
 * @example
 * <CopyableValue value="demo-orders-green.eu-west-2.rds.amazonaws.com" />
 */
export function CopyableValue({ value }: { value: string }) {
  const [copyOutcome, setCopyOutcome] = useState<CopyOutcome>("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopyOutcome("copied");
    } catch {
      setCopyOutcome("failed");
    }
  };
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 rounded-md border border-hairline px-2 py-1 font-mono text-sm [overflow-wrap:anywhere]">
          {value}
        </code>
        <Button size="sm" variant="outline" className="shrink-0" onClick={() => void copy()}>
          Copy
        </Button>
      </div>
      <p role="status" className="text-xs text-muted">
        {copyOutcome === "copied" && "Copied"}
        {copyOutcome === "failed" && "Could not reach the clipboard; select the text and copy it instead."}
      </p>
    </div>
  );
}
