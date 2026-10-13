import { Compass } from "lucide-react";
import { Link, useLocation } from "react-router";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";

/**
 * The page for an address no page has, such as a mistyped or truncated link: it names the address and
 * offers the way back, inside the shell so the sidebar still leads everywhere.
 *
 * @example
 * <Route path="*" element={<NotFoundPage />} />
 */
export default function NotFoundPage() {
  const { pathname } = useLocation();
  return (
    <>
      <PageHeader breadcrumb={[{ label: "Overview", to: "/" }]} title="Page not found" />
      <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-hairline bg-surface p-10 text-center">
        <Compass aria-hidden="true" className="size-6 text-muted" />
        <p className="text-sm text-muted">
          No page of the console lives at{" "}
          <span className="font-mono text-ink [overflow-wrap:anywhere]">{pathname}</span>. The link may be
          mistyped or cut short; the sidebar leads to every page.
        </p>
        <Button asChild size="sm">
          <Link to="/">Back to the overview</Link>
        </Button>
      </div>
    </>
  );
}
