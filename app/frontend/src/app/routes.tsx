import { lazy, type ReactElement, Suspense } from "react";
import { Route, Routes } from "react-router";
import NotFoundPage from "./NotFoundPage";
import { Shell } from "./Shell";

const OverviewPage = lazy(() => import("@/features/overview/OverviewPage"));
const InstancesPage = lazy(() => import("@/features/estate/InstancesPage"));
const ClustersPage = lazy(() => import("@/features/estate/ClustersPage"));
const ParameterGroupsPage = lazy(() => import("@/features/estate/ParameterGroupsPage"));
const SnapshotsPage = lazy(() => import("@/features/estate/SnapshotsPage"));
const ResourcePage = lazy(() => import("@/features/resources/ResourcePage"));
const OperationsPage = lazy(() => import("@/features/operations/OperationsPage"));
const HistoryPage = lazy(() => import("@/features/history/HistoryPage"));
const AttentionPage = lazy(() => import("@/features/attention/AttentionPage"));
const EnvironmentsPage = lazy(() => import("@/features/environments/EnvironmentsPage"));
const EnvironmentPage = lazy(() => import("@/features/environments/EnvironmentPage"));
const ComparePage = lazy(() => import("@/features/environments/ComparePage"));
const SettingsPage = lazy(() => import("@/features/settings/SettingsPage"));

function page(element: ReactElement) {
  return <Suspense fallback={<p className="text-muted">Loading…</p>}>{element}</Suspense>;
}

/** Every page has a URL; an address no page has shows the not-found page inside the shell. */
export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={page(<OverviewPage />)} />
        <Route path="estate/instances" element={page(<InstancesPage />)} />
        <Route path="estate/clusters" element={page(<ClustersPage />)} />
        <Route path="estate/parameter-groups" element={page(<ParameterGroupsPage />)} />
        <Route path="estate/snapshots" element={page(<SnapshotsPage />)} />
        <Route path="resources/:resourceId" element={page(<ResourcePage />)} />
        <Route path="operations" element={page(<OperationsPage />)} />
        <Route path="operations/:operationId" element={page(<OperationsPage />)} />
        <Route path="history" element={page(<HistoryPage />)} />
        <Route path="attention" element={page(<AttentionPage />)} />
        <Route path="environments" element={page(<EnvironmentsPage />)} />
        <Route path="environments/compare" element={page(<ComparePage />)} />
        <Route path="environments/:slug" element={page(<EnvironmentPage />)} />
        <Route path="settings" element={page(<SettingsPage />)} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
