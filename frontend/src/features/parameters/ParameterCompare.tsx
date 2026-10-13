import { useId, useState } from "react";
import type { ParameterComparison } from "@/api/types";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useApiGet } from "@/features/tasks/widgets/useApiGet";
import { cn } from "@/lib/utils";

interface ParameterCompareProps {
  /** Resource id of the parameter group on the left. */
  leftResourceId: string;
  /** Resource id of the group on the right; leave it out to compare with the family defaults. */
  rightResourceId?: string;
  /** Compare with the family defaults even when a right-hand group is given. */
  againstDefaults?: boolean;
}

/** A value one side sets, or, muted, the default it is left at. */
function SideValue({ value, fallback }: { value: string | null; fallback: string | null }) {
  if (value !== null) return <span className="font-mono">{value}</span>;
  return (
    <>
      <span className="font-mono text-muted">{fallback ?? "—"}</span>
      <span className="sr-only"> (default)</span>
    </>
  );
}

/** A column header: the group's name, and its family when the two sides are not of one family. */
function SideHeader({ name, family, showFamily }: { name: string; family: string; showFamily: boolean }) {
  return (
    <TableHead>
      <span className="font-mono">{name}</span>
      {showFamily && <span className="block font-mono text-xs font-normal text-muted">{family}</span>}
    </TableHead>
  );
}

/**
 * A parameter group against the family defaults or against another group: name, the value on each side, the
 * family default and whether the parameter needs a reboot. "Differences only" is on to begin with, so the
 * table shows what is worth looking at; a parameter a group leaves at its default shows that default in
 * muted type.
 *
 * @example
 * <ParameterCompare leftResourceId={tunedId} rightResourceId={otherId} />
 * <ParameterCompare leftResourceId={tunedId} againstDefaults />
 */
export function ParameterCompare({
  leftResourceId,
  rightResourceId,
  againstDefaults,
}: ParameterCompareProps) {
  const [differencesOnly, setDifferencesOnly] = useState(true);
  const switchId = useId();
  const comparison = useApiGet<ParameterComparison>("/api/v1/parameters/compare", {
    left: leftResourceId,
    right: rightResourceId,
    against_defaults: againstDefaults ? "true" : null,
  });
  if (comparison.error) {
    return (
      <p role="alert" className="text-sm text-status-red">
        Could not compare the groups: {comparison.error}
      </p>
    );
  }
  if (!comparison.data) return <p className="text-sm text-muted">Comparing…</p>;

  const { left_name, right_name, family_left, family_right, rows } = comparison.data;
  const shown = differencesOnly ? rows.filter((row) => row.differs) : rows;
  const showFamilies = family_left !== family_right;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Switch id={switchId} size="sm" checked={differencesOnly} onCheckedChange={setDifferencesOnly} />
        <label htmlFor={switchId}>Differences only</label>
        <span className="text-xs text-muted">
          {shown.length} of {rows.length} parameters
        </span>
      </div>
      {/* The scroll container is this wrapper, so the sticky header sticks to it rather than to the table's own wrapper. */}
      <div className="max-h-[70vh] overflow-auto rounded-lg border border-hairline bg-surface [&_[data-slot=table-container]]:overflow-visible">
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-surface">
            <TableRow>
              <TableHead>Name</TableHead>
              <SideHeader name={left_name} family={family_left} showFamily={showFamilies} />
              <SideHeader name={right_name} family={family_right} showFamily={showFamilies} />
              <TableHead>Default</TableHead>
              <TableHead>Apply</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody className="[&_td]:py-1">
            {shown.length === 0 && (
              <TableRow className="h-9">
                <TableCell colSpan={5} className="text-muted">
                  {differencesOnly ? "No differences between the two groups." : "No parameters."}
                </TableCell>
              </TableRow>
            )}
            {shown.map((row) => (
              <TableRow key={row.name} className={cn("h-9", row.differs && "bg-accent/5")}>
                <TableCell className="font-mono">
                  {row.differs && (
                    <>
                      <span aria-hidden="true" className="mr-1 text-accent">
                        ●
                      </span>
                      <span className="sr-only">Differs: </span>
                    </>
                  )}
                  {row.name}
                </TableCell>
                <TableCell>
                  <SideValue value={row.left} fallback={row.default} />
                </TableCell>
                <TableCell>
                  <SideValue value={row.right} fallback={row.default} />
                </TableCell>
                <TableCell className="font-mono text-muted">{row.default ?? "—"}</TableCell>
                <TableCell>
                  {row.apply_type === "static" ? (
                    <span className="text-status-amber">static (reboot)</span>
                  ) : (
                    "dynamic"
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
