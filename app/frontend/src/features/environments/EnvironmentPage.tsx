import { Link, useParams } from "react-router";
import type { EnvironmentPageData } from "@/api/types";
import { environmentHue, useEnvironment } from "@/app/EnvironmentContext";
import { EnvironmentChip } from "@/components/EnvironmentChip";
import { PageHeader } from "@/components/PageHeader";
import { SimulatedBadge } from "@/components/SimulatedBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ActionsMenu } from "@/features/resources/ActionsMenu";
import { ChangeTimeline } from "@/features/resources/ChangeTimeline";
import { FactsGrid } from "@/features/resources/FactsGrid";
import { ResourceSection } from "@/features/resources/ResourceSection";
import { useTask } from "@/features/tasks/TaskContext";
import { useReloadWhenTaskCloses } from "@/features/tasks/useReloadWhenTaskCloses";
import { EnvironmentDrift } from "./EnvironmentDrift";
import { useEnvironmentPage } from "./useEnvironmentPage";

/**
 * Tasks the Actions menu leaves out because this page is not where they are started. Drift is resolved from
 * the open drift list, row by row, since the task acts on one resource that the form cannot ask for; an
 * import acts on no existing environment, and the Environments page is where it is started and where the
 * environment switcher is refreshed once it has run.
 */
const TASKS_STARTED_ELSEWHERE = new Set(["drift_resolve", "environment_import"]);

/** Resource kinds in the order the counts list them. */
const COUNTED_KINDS: { kind: string; label: string }[] = [
  { kind: "instance", label: "Instances" },
  { kind: "cluster", label: "Clusters" },
  { kind: "parameter_group", label: "Parameter groups" },
  { kind: "snapshot", label: "Snapshots" },
];

function whenOrNever(timestamp: string | null): string {
  return timestamp ? new Date(timestamp).toLocaleString() : "never";
}

/** A loaded environment: header with its actions, settings, counts, targets, drift, operations and changes. */
function EnvironmentView({ page }: { page: EnvironmentPageData }) {
  const { overview } = page;
  const { environments } = useEnvironment();
  const { open } = useTask();
  const requiresApproval = environments.find(
    (environment) => environment.slug === overview.slug,
  )?.requires_approval;
  const actions = page.actions.filter((action) => !TASKS_STARTED_ELSEWHERE.has(action.kind));
  return (
    <>
      <PageHeader
        breadcrumb={[{ label: "Environments", to: "/environments" }, { label: overview.display_name }]}
        title={<span className="font-mono">{overview.slug}</span>}
        chips={
          <>
            <EnvironmentChip
              environment={{
                display_name: overview.display_name,
                is_production: overview.is_production,
                targets: page.targets,
              }}
              hue={environmentHue(environments, overview.slug)}
            />
            <StatusBadge status={overview.change_management} />
          </>
        }
        actions={
          <>
            <Button asChild variant="outline">
              <Link to={`/environments/compare?left=${encodeURIComponent(overview.slug)}`}>
                Compare with…
              </Link>
            </Button>
            <ActionsMenu
              actions={actions}
              onPick={(action) => open({ kind: action.kind, environmentSlug: overview.slug })}
            />
          </>
        }
      />
      <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2">
        <ResourceSection title="Settings">
          <FactsGrid
            facts={[
              { label: "Change management", value: overview.change_management },
              {
                label: "Foreign-owned changes allowed",
                value: overview.allow_direct_changes_to_foreign_owned,
              },
              { label: "Inventory refresh every", value: `${overview.inventory_refresh_minutes} minutes` },
              { label: "Drift check every", value: `${overview.drift_check_hours} hours` },
              { label: "Approval required", value: requiresApproval },
              { label: "Last refresh", value: whenOrNever(overview.last_refresh_at_utc) },
            ]}
          />
        </ResourceSection>
        <ResourceSection title="Resources">
          <FactsGrid
            facts={COUNTED_KINDS.map(({ kind, label }) => ({ label, value: overview.counts[kind] ?? 0 }))}
          />
        </ResourceSection>
      </div>
      <ResourceSection title="Targets" className="mt-4">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Target</TableHead>
              <TableHead>Account</TableHead>
              <TableHead>Region</TableHead>
              <TableHead>Profile</TableHead>
              <TableHead>Simulated</TableHead>
              <TableHead>Last snapshot</TableHead>
              <TableHead>Workspace</TableHead>
              <TableHead>Last commit</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {page.targets.map((target) => (
              <TableRow key={target.target_key} className="h-9">
                <TableCell className="whitespace-normal break-all font-mono">{target.target_key}</TableCell>
                <TableCell className="font-mono">{target.aws_account_id}</TableCell>
                <TableCell className="font-mono">{target.aws_region}</TableCell>
                <TableCell className="font-mono">{target.aws_profile_name ?? "—"}</TableCell>
                <TableCell>{target.simulated ? <SimulatedBadge simulated /> : "no"}</TableCell>
                <TableCell>{whenOrNever(target.last_snapshot_at_utc)}</TableCell>
                <TableCell className="whitespace-normal break-all font-mono">
                  {target.workspace_relative_dir}
                </TableCell>
                <TableCell className="font-mono">
                  {target.last_applied_commit_sha ? (
                    <span title={target.last_applied_commit_sha}>
                      {target.last_applied_commit_sha.slice(0, 8)}
                    </span>
                  ) : (
                    "—"
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </ResourceSection>
      <div className="mt-4 grid grid-cols-1 items-start gap-4 md:grid-cols-2">
        <ResourceSection title="Open drift">
          <EnvironmentDrift
            findings={page.drift}
            onAccept={(key) =>
              open({
                kind: "drift_resolve",
                environmentSlug: overview.slug,
                resourceIdentifier: key,
                initialOptions: { resource_arn: key, choice: "accept" },
              })
            }
          />
        </ResourceSection>
        <ResourceSection title="Recent operations">
          <ul className="space-y-1 text-sm">
            {page.recent_operations.map((operation) => (
              <li key={operation.id} className="flex items-center gap-2">
                <StatusBadge status={operation.state} />
                <Link to={`/operations/${operation.id}`} className="truncate hover:text-accent">
                  {operation.title}
                </Link>
              </li>
            ))}
            {page.recent_operations.length === 0 && <li className="text-muted">None yet.</li>}
          </ul>
        </ResourceSection>
      </div>
      <ResourceSection title="Change feed" className="mt-4">
        <ChangeTimeline events={page.change_events} showResource />
      </ResourceSection>
    </>
  );
}

/**
 * One environment: its mode and settings, targets, counts, actions, open drift, recent operations and change
 * feed. Actions open the task panel on this environment, and the page is read again when the panel closes,
 * because the task may have changed the environment.
 *
 * @example
 * <Route path="environments/:slug" element={<EnvironmentPage />} />
 */
export default function EnvironmentPage() {
  const { slug } = useParams();
  const { page, error, reload } = useEnvironmentPage(slug);
  useReloadWhenTaskCloses(reload);
  return (
    <>
      {error && (
        <p role="alert" className="mb-4 text-sm text-status-red">
          Could not load this environment: {error}.{" "}
          <Link to="/environments" className="underline">
            Back to the environments
          </Link>
        </p>
      )}
      {!page && !error && <p className="text-muted">Loading…</p>}
      {page && <EnvironmentView page={page} />}
    </>
  );
}
