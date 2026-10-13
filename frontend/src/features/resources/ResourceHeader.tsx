import type { ActionAvailability, ResourceRecord } from "@/api/types";
import { environmentHue, useEnvironment } from "@/app/EnvironmentContext";
import { EnvironmentChip } from "@/components/EnvironmentChip";
import { OwnershipBadge } from "@/components/OwnershipBadge";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { ActionsMenu } from "./ActionsMenu";

/** The estate list a kind of resource belongs to. */
const LIST_FOR_KIND: Record<string, { label: string; path: string }> = {
  instance: { label: "Instances", path: "/estate/instances" },
  cluster: { label: "Clusters", path: "/estate/clusters" },
  parameter_group: { label: "Parameter groups", path: "/estate/parameter-groups" },
  snapshot: { label: "Snapshots", path: "/estate/snapshots" },
};

interface ResourceHeaderProps {
  record: ResourceRecord;
  actions: ActionAvailability[];
  onPickAction: (action: ActionAvailability) => void;
}

/**
 * Identifier, kind, environment, status, ownership and Terraform address (when adopted) of a resource, with
 * its Actions menu.
 *
 * @example
 * <ResourceHeader record={page.record} actions={page.actions} onPickAction={openTask} />
 */
export function ResourceHeader({ record, actions, onPickAction }: ResourceHeaderProps) {
  const { environments } = useEnvironment();
  const environment = environments.find((candidate) => candidate.slug === record.environment_slug);
  const list = LIST_FOR_KIND[record.kind];
  return (
    <PageHeader
      breadcrumb={[{ label: "Estate" }, list ? { label: list.label, to: list.path } : { label: record.kind }]}
      title={<span className="font-mono">{record.identifier}</span>}
      chips={
        <>
          <span className="text-xs uppercase text-muted">{record.kind.replaceAll("_", " ")}</span>
          {environment ? (
            <EnvironmentChip environment={environment} hue={environmentHue(environments, environment.slug)} />
          ) : (
            <span className="font-mono text-xs text-muted">{record.environment_slug}</span>
          )}
          <StatusBadge status={record.status} />
          <OwnershipBadge ownership={record.ownership} />
          {record.terraform_address && (
            <span className="font-mono text-xs text-muted">{record.terraform_address}</span>
          )}
        </>
      }
      actions={<ActionsMenu actions={actions} onPick={onPickAction} />}
    />
  );
}
