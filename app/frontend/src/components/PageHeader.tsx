import type { ReactNode } from "react";
import { Link } from "react-router";

export interface Crumb {
  label: string;
  /** Omit for the current page. */
  to?: string;
}

interface PageHeaderProps {
  breadcrumb: Crumb[];
  title: ReactNode;
  chips?: ReactNode;
  actions?: ReactNode;
}

/**
 * Breadcrumb, title, chips and the actions slot on the right.
 *
 * @example
 * <PageHeader breadcrumb={[{ label: "Estate", to: "/estate/instances" }, { label: "demo-orders" }]} title="demo-orders" actions={<Button>Reboot</Button>} />
 */
export function PageHeader({ breadcrumb, title, chips, actions }: PageHeaderProps) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <nav aria-label="Breadcrumb" className="text-xs text-muted">
          {breadcrumb.map((crumb, index) => (
            <span key={crumb.label}>
              {index > 0 && <span aria-hidden="true"> / </span>}
              {crumb.to ? (
                <Link to={crumb.to} className="hover:text-ink">
                  {crumb.label}
                </Link>
              ) : (
                crumb.label
              )}
            </span>
          ))}
        </nav>
        <h1 className="truncate text-2xl font-semibold">{title}</h1>
        {chips && <div className="mt-1 flex flex-wrap items-center gap-2">{chips}</div>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
