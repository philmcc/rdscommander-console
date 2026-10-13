import type { Scalar } from "@/lib/recordValues";

/**
 * Backup retention and window in words; null when the retention is unknown.
 *
 * @example
 * backupSummary(7, "03:00-04:00"); // "7 days, window 03:00-04:00"
 * backupSummary(1, null); // "1 day, window —"
 */
export function backupSummary(retentionDays: Scalar, window: Scalar): string | null {
  if (retentionDays === null) return null;
  return `${retentionDays} ${Number(retentionDays) === 1 ? "day" : "days"}, window ${window ?? "—"}`;
}
