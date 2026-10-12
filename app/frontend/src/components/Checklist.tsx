import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface ChecklistItem {
  label: string;
  done: boolean;
  /** Shown beside the label while the item is not done. */
  action?: ReactNode;
}

/**
 * First-steps checklist; the caller hides it once every item is done.
 *
 * @example
 * <Checklist items={[{ label: "Add an environment", done: true }, { label: "Refresh inventory", done: false, action: <Button>Refresh</Button> }]} />
 */
export function Checklist({ items }: { items: ChecklistItem[] }) {
  return (
    <ol className="space-y-2">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-3 text-sm">
          <span
            aria-hidden="true"
            className={cn(
              "inline-flex size-5 items-center justify-center rounded-full border",
              item.done ? "border-status-green text-status-green" : "border-hairline text-muted",
            )}
          >
            {item.done ? "✓" : ""}
          </span>
          <span className="sr-only">{item.done ? "Done:" : "To do:"}</span>
          <span className={cn(item.done && "text-muted line-through")}>{item.label}</span>
          {!item.done && item.action}
        </li>
      ))}
    </ol>
  );
}
