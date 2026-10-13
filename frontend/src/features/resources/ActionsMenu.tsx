import type { ActionAvailability } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

interface ActionsMenuProps {
  actions: ActionAvailability[];
  onPick: (action: ActionAvailability) => void;
  /** Accessible name of the button, when "Actions" alone would not say whose they are. */
  triggerLabel?: string;
  /** A smaller, quieter button, for a row of a table. */
  compact?: boolean;
  /** Called when the menu opens or closes, so the actions can be read the first time it opens. */
  onOpenChange?: (open: boolean) => void;
  /** Shown instead of the actions while they are read, or why they could not be. */
  status?: string | null;
}

/**
 * Every catalogue entry for the resource, grouped. An entry that cannot run now is disabled and says why,
 * in text as well as in its tooltip; destructive entries are red and disruptive ones are marked.
 *
 * @example
 * <ActionsMenu actions={page.actions} onPick={(action) => openTask(action.kind)} />
 * <ActionsMenu actions={[]} status="Loading…" triggerLabel="Actions for demo-aurora-2" compact onPick={start} />
 */
export function ActionsMenu({
  actions,
  onPick,
  triggerLabel,
  compact = false,
  onOpenChange,
  status = null,
}: ActionsMenuProps) {
  const groups = new Map<string, ActionAvailability[]>();
  for (const action of actions) groups.set(action.group, [...(groups.get(action.group) ?? []), action]);
  return (
    <DropdownMenu onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={triggerLabel}
          variant={compact ? "ghost" : "default"}
          size={compact ? "xs" : "default"}
        >
          Actions
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        {status !== null && <p className="px-2 py-1.5 text-sm text-muted">{status}</p>}
        {status === null && groups.size === 0 && (
          <p className="px-2 py-1.5 text-sm text-muted">No actions are defined for this resource.</p>
        )}
        {[...groups.entries()].map(([group, items], index) => (
          <DropdownMenuGroup key={group}>
            {index > 0 && <DropdownMenuSeparator />}
            <DropdownMenuLabel className="text-xs text-muted">{group}</DropdownMenuLabel>
            {items.map((action) => (
              <DropdownMenuItem
                key={action.kind}
                disabled={!action.applicable}
                onSelect={() => onPick(action)}
                title={action.reason || undefined}
                className="flex flex-col items-start gap-0"
              >
                <span className={cn(action.destructive && "text-status-red")}>
                  {action.title}
                  {action.destructive && <span className="sr-only"> (destructive)</span>}
                  {action.disruptive && (
                    <>
                      {" "}
                      <span className="text-xs text-status-amber">disruptive</span>
                    </>
                  )}
                </span>
                {!action.applicable && <span className="text-xs text-muted">{action.reason}</span>}
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
