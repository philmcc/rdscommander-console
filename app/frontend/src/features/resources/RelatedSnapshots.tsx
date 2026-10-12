import type { ResourceRecord } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { scalar } from "@/lib/recordValues";
import { ResourceLink } from "./ResourceLink";
import { ResourceSection } from "./ResourceSection";
import type { ResourceDirectory } from "./useResourceDirectory";

interface RelatedSnapshotsProps {
  /** The instance or cluster whose snapshots are listed. */
  record: ResourceRecord;
  directory: ResourceDirectory;
  className?: string;
}

/**
 * The snapshots taken of an instance or cluster, newest first, each linking to its own page.
 *
 * @example
 * <RelatedSnapshots record={record} directory={directory} />
 */
export function RelatedSnapshots({ record, directory, className }: RelatedSnapshotsProps) {
  const snapshots = directory.snapshotsOf(
    record.kind === "cluster" ? "cluster" : "instance",
    record.identifier,
  );
  return (
    <ResourceSection title="Snapshots" className={className}>
      {directory.error && (
        <p role="alert" className="text-sm text-status-red">
          Could not load snapshots: {directory.error}
        </p>
      )}
      {!directory.error && !directory.loaded && <p className="text-sm text-muted">Loading…</p>}
      {directory.loaded && snapshots.length === 0 && <p className="text-sm text-muted">No snapshots.</p>}
      {snapshots.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Snapshot</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Size (GB)</TableHead>
              <TableHead>Created</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {snapshots.map((snapshot) => (
              <TableRow key={snapshot.arn} className="h-9">
                <TableCell>
                  <ResourceLink target={snapshot} identifier={snapshot.identifier} />
                </TableCell>
                <TableCell>{scalar(snapshot.facts, "snapshot_type") ?? "—"}</TableCell>
                <TableCell>
                  <StatusBadge status={snapshot.status} />
                </TableCell>
                <TableCell>{scalar(snapshot.facts, "allocated_storage_gb") ?? "—"}</TableCell>
                <TableCell className="font-mono">
                  {scalar(snapshot.facts, "created_at_source_utc") ?? "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </ResourceSection>
  );
}
