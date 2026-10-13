import { TriangleAlert } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";

interface PageErrorBoundaryProps {
  children: ReactNode;
  /** The address of the page shown; when it changes the boundary forgets the error and shows the new page. */
  resetKey: string;
}

interface PageErrorBoundaryState {
  error: Error | null;
}

/**
 * Catches an error thrown while a routed page renders and shows it in place of that page, so the sidebar,
 * the top bar and every other page keep working instead of the whole console going blank. Moving to another
 * address clears it.
 *
 * @example
 * <PageErrorBoundary resetKey={location.pathname}><Outlet /></PageErrorBoundary>
 */
export class PageErrorBoundary extends Component<PageErrorBoundaryProps, PageErrorBoundaryState> {
  override state: PageErrorBoundaryState = { error: null };

  static getDerivedStateFromError(caught: unknown): PageErrorBoundaryState {
    return { error: caught instanceof Error ? caught : new Error(String(caught)) };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(
      JSON.stringify({
        event: "page_render_failed",
        address: this.props.resetKey,
        error_message: error.message,
        component_stack: info.componentStack ?? null,
        fix_suggestion:
          "A page rendered a value it could not show; check the API answer this page read against the contract",
      }),
    );
  }

  override componentDidUpdate(previous: PageErrorBoundaryProps): void {
    if (previous.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div role="alert" className="space-y-3 rounded-lg border border-status-red/40 bg-surface p-6">
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <TriangleAlert aria-hidden="true" className="size-5 text-status-red" />
          This page could not be shown
        </h1>
        <p className="text-sm">
          It failed while drawing: <span className="font-mono [overflow-wrap:anywhere]">{error.message}</span>
        </p>
        <p className="text-sm text-muted">
          The rest of the console still works. Reload to try this page again, or go back to the overview.
        </p>
        <Button asChild size="sm">
          <Link to="/">Back to the overview</Link>
        </Button>
      </div>
    );
  }
}
