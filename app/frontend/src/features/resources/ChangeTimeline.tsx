import { Link } from "react-router";
import type { ChangeEventSummary } from "@/api/types";
import { Button } from "@/components/ui/button";
import { displayValue } from "@/lib/recordValues";

interface ChangeTimelineProps {
  events: ChangeEventSummary[];
  /** Called with the event id when the operator marks an outside-the-console change reviewed. */
  onReview?: (id: number) => void;
  /** Names the resource each change belongs to, as a link to its page; for a feed that spans resources. */
  showResource?: boolean;
}

/**
 * Attribute changes between inventory snapshots, newest first. A change a console operation made links to
 * that operation; a change made outside the console is amber until someone marks it reviewed.
 *
 * @example
 * <ChangeTimeline events={page.change_events} onReview={(id) => review(id)} />
 * <ChangeTimeline events={environmentPage.change_events} showResource />
 */
export function ChangeTimeline({ events, onReview, showResource = false }: ChangeTimelineProps) {
  if (events.length === 0) return <p className="text-sm text-muted">No changes recorded yet.</p>;
  return (
    <ul className="space-y-1">
      {events.map((event) => (
        <li
          key={event.id}
          className="flex flex-wrap items-center gap-2 border-b border-hairline py-1 text-sm last:border-0"
        >
          <span className="w-40 shrink-0 text-xs text-muted">
            {new Date(event.detected_at_utc).toLocaleString()}
          </span>
          {showResource && (
            <Link to={`/resources/${event.resource_id}`} className="font-mono text-accent hover:underline">
              {event.resource_arn.split(":").at(-1)}
            </Link>
          )}
          <span className="font-mono">{event.attribute_path}</span>
          <span className="break-all font-mono text-muted">{displayValue(event.value_before)}</span>
          <span aria-hidden="true">→</span>
          <span className="break-all font-mono">{displayValue(event.value_after)}</span>
          {event.explanation === "operation" ? (
            event.operation_id ? (
              <Link to={`/operations/${event.operation_id}`} className="text-xs text-accent hover:underline">
                by the console
              </Link>
            ) : (
              <span className="text-xs text-muted">by the console</span>
            )
          ) : (
            <span className="text-xs text-status-amber">outside the console</span>
          )}
          {event.explanation === "outside_console" && !event.reviewed_at_utc && onReview && (
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Mark reviewed: ${event.attribute_path}`}
              onClick={() => onReview(event.id)}
            >
              Mark reviewed
            </Button>
          )}
          {event.reviewed_by_operator_name && (
            <span className="text-xs text-muted">reviewed by {event.reviewed_by_operator_name}</span>
          )}
        </li>
      ))}
    </ul>
  );
}
