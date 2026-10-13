import {
  Bell,
  Boxes,
  Camera,
  ChevronsLeft,
  ChevronsRight,
  Database,
  Globe,
  History,
  LayoutDashboard,
  ListChecks,
  type LucideIcon,
  Menu,
  Moon,
  Search,
  Server,
  Settings,
  SlidersHorizontal,
  Sun,
} from "lucide-react";
import { useEffect, useState, useSyncExternalStore } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { apiGet } from "@/api/client";
import type { AttentionInbox, OperationList } from "@/api/types";
import { EnvironmentChip } from "@/components/EnvironmentChip";
import { ProgressStrip } from "@/components/ProgressStrip";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Launcher } from "@/features/launcher/Launcher";
import { cn } from "@/lib/utils";
import { applyTheme, getThemeSnapshot, subscribeTheme } from "@/theme";
import { environmentHue, useEnvironment } from "./EnvironmentContext";
import { PageErrorBoundary } from "./PageErrorBoundary";

interface NavLeaf {
  to: string;
  label: string;
  icon: LucideIcon;
}

interface NavItem extends NavLeaf {
  children?: NavLeaf[];
}

const NAV: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard },
  {
    to: "/estate/instances",
    label: "Estate",
    icon: Database,
    children: [
      { to: "/estate/instances", label: "Instances", icon: Server },
      { to: "/estate/clusters", label: "Clusters", icon: Boxes },
      { to: "/estate/parameter-groups", label: "Parameter groups", icon: SlidersHorizontal },
      { to: "/estate/snapshots", label: "Snapshots", icon: Camera },
    ],
  },
  { to: "/operations", label: "Operations", icon: ListChecks },
  { to: "/history", label: "History", icon: History },
  { to: "/environments", label: "Environments", icon: Globe },
  { to: "/settings", label: "Settings", icon: Settings },
];
const COLLAPSED_KEY = "rdscommander.sidebarCollapsed";
const REFRESH_INTERVAL_MILLISECONDS = 10_000;

/** Total rows across every section of the attention inbox. */
function attentionCount(inbox: AttentionInbox): number {
  return Object.values(inbox).reduce(
    (total, section) => total + (Array.isArray(section) ? section.length : 0),
    0,
  );
}

interface NavigationProps {
  collapsed: boolean;
  onNavigate?: () => void;
}

/** One sidebar link; collapsed it is an icon whose name stays available as `title` and `aria-label`. */
function NavigationLink({
  leaf,
  collapsed,
  nested = false,
  onNavigate,
}: {
  leaf: NavLeaf;
  collapsed: boolean;
  nested?: boolean;
  onNavigate?: () => void;
}) {
  return (
    <NavLink
      to={leaf.to}
      end={leaf.to === "/"}
      onClick={onNavigate}
      title={leaf.label}
      aria-label={leaf.label}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2 rounded-md py-1.5 text-sm [&>svg]:size-4 [&>svg]:shrink-0",
          collapsed ? "justify-center px-2" : nested ? "pl-8 pr-2" : "px-2",
          isActive ? "bg-accent/10 text-ink" : "text-muted hover:text-ink",
        )
      }
    >
      {(!nested || collapsed) && <leaf.icon />}
      {!collapsed && leaf.label}
    </NavLink>
  );
}

/**
 * The primary navigation. Expanded it groups the four Estate pages under Estate; collapsed it is a flat
 * list of icon links in which the Estate pages replace the Estate group, so every page stays one click away.
 */
function Navigation({ collapsed, onNavigate }: NavigationProps) {
  return (
    <nav aria-label="Primary" data-collapsed={collapsed} className="flex flex-col gap-1 p-2">
      {collapsed
        ? NAV.flatMap((item) => item.children ?? [item]).map((leaf) => (
            <NavigationLink key={leaf.to} leaf={leaf} collapsed onNavigate={onNavigate} />
          ))
        : NAV.map((item) => (
            <div key={item.to}>
              <NavigationLink leaf={item} collapsed={false} onNavigate={onNavigate} />
              {item.children?.map((child) => (
                <NavigationLink
                  key={child.to}
                  leaf={child}
                  collapsed={false}
                  nested
                  onNavigate={onNavigate}
                />
              ))}
            </div>
          ))}
    </nav>
  );
}

/**
 * What the top bar reports, refreshed every ten seconds.
 *
 * - `attention`: rows in the selected environment's inbox (the same count the inbox page shows); `null`
 *   while unknown (loading, after switching environment, or when the request failed).
 * - `running`: whether any operation in any environment is running, for the progress strip; `false`
 *   while unknown or when the request failed.
 *
 * The two requests are independent, so one failing never discards the other.
 */
function useTopBarStatus(selectedSlug: string | null): { attention: number | null; running: boolean } {
  const [attention, setAttention] = useState<number | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (!selectedSlug) return;
    let cancelled = false;
    setAttention(null);
    const refresh = async () => {
      try {
        const inbox = await apiGet<AttentionInbox>("/api/v1/attention", { environment_slug: selectedSlug });
        if (!cancelled) setAttention(attentionCount(inbox));
      } catch {
        if (!cancelled) setAttention(null);
      }
    };
    void refresh();
    const timer = setInterval(refresh, REFRESH_INTERVAL_MILLISECONDS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [selectedSlug]);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const operations = await apiGet<OperationList>("/api/v1/operations", { state: "RUNNING", limit: 1 });
        if (!cancelled) setRunning(operations.operations.length > 0);
      } catch {
        if (!cancelled) setRunning(false);
      }
    };
    void refresh();
    const timer = setInterval(refresh, REFRESH_INTERVAL_MILLISECONDS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return { attention, running };
}

/** Sidebar, the sticky top bar with its progress strip, and the routed page body behind an error boundary. */
export function Shell() {
  const { bootstrap, error, environments, selected, select } = useEnvironment();
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem(COLLAPSED_KEY) === "true";
    } catch {
      return false;
    }
  });
  const [navigationOpen, setNavigationOpen] = useState(false);
  const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot);
  const { attention, running } = useTopBarStatus(selected?.slug ?? null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        window.dispatchEvent(new CustomEvent("rdscommander:launcher"));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((value) => {
      try {
        window.localStorage.setItem(COLLAPSED_KEY, String(!value));
      } catch {
        // storage unavailable
      }
      return !value;
    });
  };
  const nextTheme = theme.resolved === "dark" ? "light" : "dark";
  const switchTheme = () => applyTheme(nextTheme);

  if (error) return <main className="p-6 text-status-red">Cannot reach the console backend: {error}</main>;
  if (!bootstrap) return <main className="p-6 text-muted">Loading…</main>;

  return (
    <div className="flex min-h-screen">
      <aside
        className={cn(
          "hidden shrink-0 border-r border-hairline bg-surface nav:flex nav:flex-col",
          collapsed ? "w-14" : "w-[232px]",
        )}
      >
        <div className="flex h-12 items-center justify-between px-3">
          {!collapsed && <span className="truncate text-sm font-semibold">RDSCommander</span>}
          <Button
            variant="ghost"
            size="icon"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={toggleCollapsed}
          >
            {collapsed ? <ChevronsRight /> : <ChevronsLeft />}
          </Button>
        </div>
        <Navigation collapsed={collapsed} />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        {/* The top bar stays in view however far a page scrolls, so the selected environment (production's
            red border and word) is on screen next to every action on the page. */}
        <div className="sticky top-0 z-30 bg-surface">
          <header className="flex h-12 items-center gap-3 border-b border-hairline bg-surface px-3">
            <Sheet open={navigationOpen} onOpenChange={setNavigationOpen}>
              <SheetTrigger asChild>
                <Button variant="ghost" size="icon" className="nav:hidden" aria-label="Open navigation">
                  <Menu />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-[232px] p-0">
                <SheetTitle className="px-4 pt-4 text-sm font-semibold">RDSCommander</SheetTitle>
                <SheetDescription className="sr-only">Navigate between console pages</SheetDescription>
                <Navigation collapsed={false} onNavigate={() => setNavigationOpen(false)} />
              </SheetContent>
            </Sheet>
            <div className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto">
              {environments.map((environment) => (
                <EnvironmentChip
                  key={environment.slug}
                  environment={environment}
                  hue={environmentHue(environments, environment.slug)}
                  selected={environment.slug === selected?.slug}
                  onClick={() => select(environment.slug)}
                />
              ))}
            </div>
            <Button
              variant="outline"
              size="sm"
              aria-label="Start a task"
              onClick={() => window.dispatchEvent(new CustomEvent("rdscommander:launcher"))}
            >
              <Search />
              <span className="hidden sm:inline">Start a task</span>
              <kbd className="ml-1 hidden rounded border border-hairline px-1 text-xs nav:inline">⌘K</kbd>
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Needs attention: ${attention ?? "unknown"}`}
              onClick={() => navigate("/attention")}
              className={cn("relative", attention === null && "text-muted")}
            >
              <Bell />
              {attention === null && (
                <span className="absolute -right-0.5 -top-0.5 rounded-full bg-secondary px-1 text-xs text-muted">
                  ?
                </span>
              )}
              {attention !== null && attention > 0 && (
                <span className="absolute -right-0.5 -top-0.5 rounded-full bg-status-amber px-1 text-xs text-paper">
                  {attention}
                </span>
              )}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Switch to ${nextTheme} theme`}
              onClick={switchTheme}
            >
              {nextTheme === "dark" ? <Moon /> : <Sun />}
            </Button>
            <span className="hidden text-sm text-muted sm:inline">{bootstrap.operator_name}</span>
          </header>
          <ProgressStrip active={running} />
        </div>
        <Launcher />
        <main className="min-w-0 flex-1 p-4 nav:p-6">
          <PageErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </PageErrorBoundary>
        </main>
      </div>
    </div>
  );
}
