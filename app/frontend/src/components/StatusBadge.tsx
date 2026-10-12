import { cn } from "@/lib/utils";

const GREEN = new Set(["available", "in-sync", "succeeded", "completed", "adopted", "ok", "pass", "carried"]);
const AMBER = new Set([
  "pending-reboot",
  "awaiting_approval",
  "modifying",
  "applying",
  "backing-up",
  "warn",
  "pending",
  "draft",
  "needs_review",
  // RDS statuses of a database that is not serving normally but will be, or was stopped on purpose.
  "stopped",
  "stopping",
  "starting",
  "restoring",
  "failing-over",
  "renaming",
  "storage-optimization",
]);
const RED = new Set([
  "failed",
  "cancelled",
  "deleting",
  "refused",
  "blocked",
  "fail",
  "error",
  "removed",
  // RDS statuses a database stays in until someone fixes it.
  "incompatible-parameters",
  "incompatible-network",
  "incompatible-option-group",
  "incompatible-restore",
  "incompatible-credentials",
  "inaccessible-encryption-credentials",
  "inaccessible-encryption-credentials-recoverable",
  "insufficient-capacity",
  "restore-error",
  "storage-full",
]);
const BLUE = new Set([
  "running",
  "queued",
  "creating",
  "rebooting",
  "upgrading",
  "in-progress",
  "default_changed",
  "renamed",
]);

interface StatusBadgeProps {
  /** Status string from the API (operation state, RDS status, apply status, upgrade classification); case-insensitive. */
  status: string | null | undefined;
  className?: string;
}

/**
 * One colour per meaning: green healthy or succeeded, amber pending or needs attention, red failed or
 * destructive, blue in progress. Anything else is neutral. This is the only place a status maps to a hue.
 *
 * @example
 * <StatusBadge status="pending-reboot" />
 */
export function StatusBadge({ status, className }: StatusBadgeProps) {
  const key = (status ?? "unknown").toLowerCase();
  const colour = GREEN.has(key)
    ? "text-status-green border-status-green/40"
    : AMBER.has(key)
      ? "text-status-amber border-status-amber/40"
      : RED.has(key)
        ? "text-status-red border-status-red/40"
        : BLUE.has(key)
          ? "text-status-blue border-status-blue/40"
          : "text-muted border-hairline";
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded-md border px-1.5 font-mono text-xs",
        colour,
        className,
      )}
    >
      {status ?? "unknown"}
    </span>
  );
}
