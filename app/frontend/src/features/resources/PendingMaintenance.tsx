import type { PendingMaintenanceView } from "@/api/types";
import { formatUtc } from "@/lib/formatUtc";
import { ResourceSection } from "./ResourceSection";

/**
 * The maintenance AWS has queued for an instance or cluster: each action with its description, when it is
 * scheduled to apply, when AWS applies it on its own, and the opt-in status, for whichever of these AWS gave.
 *
 * @example
 * <PendingMaintenance pending={page.pending_maintenance ?? []} />
 */
export function PendingMaintenance({ pending }: { pending: PendingMaintenanceView[] }) {
  return (
    <ResourceSection title="Pending maintenance">
      {pending.length === 0 ? (
        <p className="text-sm text-muted">None pending.</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {pending.map((item) => {
            const timing = [
              item.current_apply_utc ? `applies ${formatUtc(item.current_apply_utc)}` : null,
              item.auto_applied_after_utc
                ? `auto-applied after ${formatUtc(item.auto_applied_after_utc)}`
                : null,
              item.opt_in_status ? `opt-in: ${item.opt_in_status}` : null,
            ].filter((part) => part !== null);
            return (
              <li key={item.action}>
                <p className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-mono">{item.action}</span>
                  <span className="text-muted">{item.description}</span>
                </p>
                {timing.length > 0 && (
                  <p className="flex flex-wrap gap-x-3 text-xs text-muted">
                    {timing.map((part) => (
                      <span key={part} className="font-mono">
                        {part}
                      </span>
                    ))}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </ResourceSection>
  );
}
