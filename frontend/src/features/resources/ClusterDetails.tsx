import type { ActionAvailability, PendingMaintenanceView, ResourceRecord } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { names, objects, scalar } from "@/lib/recordValues";
import { backupSummary } from "./backupSummary";
import { FactsGrid } from "./FactsGrid";
import { MemberActionsMenu } from "./MemberActionsMenu";
import { PendingMaintenance } from "./PendingMaintenance";
import { RelatedSnapshots } from "./RelatedSnapshots";
import { ResourceLink } from "./ResourceLink";
import { ResourceSection } from "./ResourceSection";
import { UpgradeAvailability } from "./UpgradeAvailability";
import type { ResourceDirectory } from "./useResourceDirectory";

/** The failover priority AWS reports for a member in the cluster's description; null when it gives none. */
function promotionTierOf(cluster: ResourceRecord, memberIdentifier: string): number | null {
  const membership = objects(cluster.raw, "DBClusterMembers").find(
    (entry) => entry.DBInstanceIdentifier === memberIdentifier,
  );
  return typeof membership?.PromotionTier === "number" ? membership.PromotionTier : null;
}

/** `0.5 to 8 ACU` when the cluster has Serverless v2 scaling, otherwise null. */
function capacitySummary(cluster: ResourceRecord): string | null {
  const minimum = scalar(cluster.facts, "serverless_v2_min_capacity");
  const maximum = scalar(cluster.facts, "serverless_v2_max_capacity");
  return minimum === null || maximum === null ? null : `${minimum} to ${maximum} ACU`;
}

interface ClusterDetailsProps {
  record: ResourceRecord;
  directory: ResourceDirectory;
  pendingMaintenance: PendingMaintenanceView[];
  /** Starts one of a member's actions (Remove reader, …) on that member; no member menus when omitted. */
  onPickMemberAction?: (member: ResourceRecord, action: ActionAvailability) => void;
}

/**
 * The body of a cluster page: facts (with the engine upgrades available), cluster parameter group, capacity,
 * members with their roles, failover tiers and each member's own Actions menu (Remove reader is there),
 * snapshots and pending maintenance.
 *
 * @example
 * <ClusterDetails record={page.record} directory={directory} pendingMaintenance={pending} />
 */
export function ClusterDetails({
  record,
  directory,
  pendingMaintenance,
  onPickMemberAction,
}: ClusterDetailsProps) {
  const facts = record.facts;
  const groupName = scalar(record.relationships, "cluster_parameter_group_name");
  const writerName = scalar(record.relationships, "writer_identifier");
  const memberNames = names(record.relationships, "member_identifiers");
  const capacity = capacitySummary(record);
  return (
    <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2">
      <ResourceSection title="Facts">
        <FactsGrid
          facts={[
            {
              label: "Engine",
              value: `${scalar(facts, "engine") ?? "—"} ${scalar(facts, "engine_version") ?? ""}`.trim(),
            },
            { label: "Engine mode", value: scalar(facts, "engine_mode") },
            {
              label: "Backups",
              value: backupSummary(
                scalar(facts, "backup_retention_period"),
                scalar(facts, "preferred_backup_window"),
              ),
            },
            { label: "Maintenance window", value: scalar(facts, "preferred_maintenance_window") },
            { label: "Encrypted", value: scalar(facts, "storage_encrypted") },
            { label: "Deletion protection", value: scalar(facts, "deletion_protection") },
          ]}
        />
        <UpgradeAvailability record={record} />
      </ResourceSection>
      <div className="space-y-4">
        <ResourceSection title="Cluster parameter group">
          <p className="text-sm">
            {groupName ? (
              <ResourceLink
                target={directory.find("parameter_group", String(groupName), "cluster")}
                identifier={String(groupName)}
              />
            ) : (
              <span className="font-mono">—</span>
            )}
          </p>
        </ResourceSection>
        <ResourceSection title="Capacity">
          {capacity ? (
            <FactsGrid facts={[{ label: "Serverless v2", value: capacity }]} />
          ) : (
            <p className="text-sm text-muted">Serverless v2 scaling is not configured.</p>
          )}
        </ResourceSection>
      </div>
      <ResourceSection title="Members" className="md:col-span-2">
        {memberNames.length === 0 ? (
          <p className="text-sm text-muted">No members.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Instance</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Tier</TableHead>
                <TableHead>Class</TableHead>
                <TableHead>Status</TableHead>
                {onPickMemberAction && (
                  <TableHead>
                    <span className="sr-only">Actions</span>
                  </TableHead>
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {memberNames.map((memberName) => {
                const member = directory.find("instance", memberName);
                const tier = promotionTierOf(record, memberName);
                return (
                  <TableRow key={memberName} className="h-9">
                    <TableCell>
                      <ResourceLink target={member} identifier={memberName} />
                    </TableCell>
                    <TableCell className={memberName === writerName ? "font-medium" : undefined}>
                      {memberName === writerName ? "writer" : "reader"}
                    </TableCell>
                    <TableCell className="font-mono">{tier ?? "—"}</TableCell>
                    <TableCell className="font-mono">
                      {member ? (scalar(member.facts, "db_instance_class") ?? "—") : "—"}
                    </TableCell>
                    <TableCell>{member ? <StatusBadge status={member.status} /> : "—"}</TableCell>
                    {onPickMemberAction && (
                      <TableCell className="text-right">
                        {member && <MemberActionsMenu member={member} onPick={onPickMemberAction} />}
                      </TableCell>
                    )}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </ResourceSection>
      <PendingMaintenance pending={pendingMaintenance} />
      <RelatedSnapshots record={record} directory={directory} />
    </div>
  );
}
