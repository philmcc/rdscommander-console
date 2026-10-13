/**
 * A timestamp as UTC to the minute, the way maintenance windows are written; a value that is not a date is
 * returned as it is.
 *
 * @example
 * formatUtc("2026-10-20T02:00:00Z"); // "2026-10-20 02:00 UTC"
 */
export function formatUtc(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp;
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
