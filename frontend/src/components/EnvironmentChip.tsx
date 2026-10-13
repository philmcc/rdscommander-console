import type { EnvironmentSummary } from "@/api/types";
import { cn } from "@/lib/utils";
import { SimulatedBadge } from "./SimulatedBadge";

interface Props {
  /** What the chip shows: the name, whether it is production, and the targets (a simulated one is badged). */
  environment: Pick<EnvironmentSummary, "display_name" | "is_production" | "targets">;
  hue: number;
  selected?: boolean;
  onClick?: () => void;
}

/** A chip with the environment's hue; production carries a red left border and the word, simulated the dashed amber badge. */
export function EnvironmentChip({ environment, hue, selected = false, onClick }: Props) {
  const simulated = environment.targets.some((t) => t.simulated);
  const body = (
    <>
      <span
        className="inline-block size-2 rounded-full"
        style={{ background: `var(--env-hue-${hue})` }}
        aria-hidden="true"
      />
      <span className="font-medium">{environment.display_name}</span>
      {environment.is_production && (
        <span className="text-xs uppercase tracking-wide text-status-red">production</span>
      )}
      <SimulatedBadge simulated={simulated} />
    </>
  );
  // The production border comes last: tailwind-merge lets a later `border-<colour>` drop an earlier `border-l-<colour>`.
  const className = cn(
    "inline-flex h-7 shrink-0 items-center gap-2 whitespace-nowrap rounded-md border px-2.5 text-sm",
    selected ? "border-accent bg-accent/10 text-ink" : "border-hairline bg-surface text-muted hover:text-ink",
    environment.is_production ? "border-l-4 border-l-status-red" : "",
  );
  if (!onClick) return <span className={className}>{body}</span>;
  return (
    <button type="button" aria-pressed={selected} onClick={onClick} className={className}>
      {body}
    </button>
  );
}
