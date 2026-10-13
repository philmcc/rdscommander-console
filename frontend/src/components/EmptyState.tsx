import type { ReactNode } from "react";

interface EmptyStateProps {
  /** Decorative icon; hidden from assistive technology. */
  icon: ReactNode;
  title: string;
  action?: ReactNode;
}

/**
 * Icon, one sentence, one action.
 *
 * @example
 * <EmptyState icon={<Database />} title="No instances in this environment." action={<Button>Refresh inventory</Button>} />
 */
export function EmptyState({ icon, title, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-hairline bg-surface p-10 text-center">
      <div aria-hidden="true" className="text-muted">
        {icon}
      </div>
      <p className="text-sm text-muted">{title}</p>
      {action}
    </div>
  );
}
