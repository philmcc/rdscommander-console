import type { DriftFindingView } from "@/api/types";
import { DiffTable } from "@/components/DiffTable";
import { Button } from "@/components/ui/button";

interface DriftPanelProps {
  drift: DriftFindingView[];
  onResolve: (choice: "accept" | "restore") => void;
}

/**
 * Open drift findings with the two ways out: accept what AWS has into the record, or restore AWS from the
 * record. Says so in green when nothing differs.
 *
 * @example
 * <DriftPanel drift={page.drift ?? []} onResolve={(choice) => openDriftResolution(choice)} />
 */
export function DriftPanel({ drift, onResolve }: DriftPanelProps) {
  if (drift.length === 0) return <p className="text-sm text-status-green">Matches the record.</p>;
  return (
    <div className="space-y-3">
      {drift.map((finding) => (
        <div key={`${finding.finding_kind}-${finding.detected_at_utc}`} className="space-y-2">
          <p className="text-sm text-status-amber">
            {finding.finding_kind === "record"
              ? "AWS differs from the committed record"
              : "Terraform state differs from the files"}{" "}
            · {new Date(finding.detected_at_utc).toLocaleString()}
          </p>
          <DiffTable
            rows={finding.attributes.map((attribute) => ({
              attribute: attribute.attribute,
              before: attribute.recorded,
              after: attribute.live,
            }))}
            beforeLabel="Recorded"
            afterLabel="Live"
          />
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={() => onResolve("accept")}>
          Accept into the record
        </Button>
        <Button variant="outline" onClick={() => onResolve("restore")}>
          Restore from the record
        </Button>
      </div>
    </div>
  );
}
