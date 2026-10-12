import { useId, useMemo, useState } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

export interface ParameterTableRow {
  name: string;
  value: string | null;
  default: string | null;
  data_type: string;
  apply_type: string;
  is_modifiable: boolean;
  description: string;
  changed: boolean;
  /** The operator has typed a value that is not applied yet; the row offers "Discard edit". */
  edited?: boolean;
  /** The row offers "Reset to default": the group sets this parameter and no reset is pending. */
  resettable?: boolean;
  /** A reset is pending: the row shows the default with a "will reset" marker and offers "Undo reset". */
  willReset?: boolean;
}

interface ParameterTableProps {
  rows: ParameterTableRow[];
  /** Show an input for each modifiable parameter. */
  editable?: boolean;
  onChange?: (name: string, value: string) => void;
  /** Called when the operator discards the pending edit of a row; only rows with `edited` offer it. */
  onReset?: (name: string) => void;
  /** Called when the operator asks to return a parameter to its default; only `resettable` rows offer it. */
  onResetToDefault?: (name: string) => void;
  /** Called when the operator cancels a pending reset; only `willReset` rows offer it. */
  onUndoReset?: (name: string) => void;
  /** Validation message per parameter name, shown under the input. */
  problems?: Record<string, string>;
}

/**
 * Name, value, default, type, apply type and modifiable flag, with a search box and a changed-only filter.
 * The header stays visible while the rows scroll.
 *
 * @example
 * <ParameterTable rows={rows} editable onChange={(name, value) => setDraft(name, value)} />
 */
export function ParameterTable({
  rows,
  editable = false,
  onChange,
  onReset,
  onResetToDefault,
  onUndoReset,
  problems = {},
}: ParameterTableProps) {
  const [search, setSearch] = useState("");
  const [changedOnly, setChangedOnly] = useState(false);
  const changedOnlyId = useId();
  const visible = useMemo(() => {
    const needle = search.toLowerCase();
    return rows.filter(
      (r) => (!changedOnly || r.changed) && (!needle || r.name.toLowerCase().includes(needle)),
    );
  }, [rows, search, changedOnly]);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <Input
          aria-label="Search parameters"
          placeholder="Search parameters"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="h-8 max-w-xs font-mono"
        />
        <div className="flex items-center gap-2 text-sm text-muted">
          <Checkbox
            id={changedOnlyId}
            checked={changedOnly}
            onCheckedChange={(v) => setChangedOnly(v === true)}
          />
          <label htmlFor={changedOnlyId}>Changed from default</label>
        </div>
        <span className="text-xs text-muted">
          {visible.length} of {rows.length}
        </span>
      </div>
      {/* The scroll container is this wrapper, so the sticky header sticks to it rather than to the table's own wrapper. */}
      <div className="max-h-[70vh] overflow-auto rounded-lg border border-hairline bg-surface [&_[data-slot=table-container]]:overflow-visible">
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-surface">
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Value</TableHead>
              <TableHead>Default</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Apply</TableHead>
              <TableHead>Modifiable</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody className="[&_td]:py-1">
            {visible.length === 0 && (
              <TableRow className="h-9">
                <TableCell colSpan={6} className="text-muted">
                  No parameters match.
                </TableCell>
              </TableRow>
            )}
            {visible.map((row) => {
              const problem = problems[row.name];
              const problemId = `parameter-problem-${row.name}`;
              return (
                <TableRow key={row.name} className={cn("h-9", row.changed && "bg-accent/5")}>
                  <TableCell className="font-mono">
                    {row.changed && (
                      <>
                        <span aria-hidden="true" className="mr-1 text-accent">
                          ●
                        </span>
                        <span className="sr-only">Changed from default: </span>
                      </>
                    )}
                    {row.name}
                  </TableCell>
                  <TableCell className="font-mono">
                    {editable && row.is_modifiable ? (
                      <div>
                        <div className="flex items-center gap-2">
                          <Input
                            aria-label={`Value of ${row.name}`}
                            aria-invalid={problem ? true : undefined}
                            aria-describedby={problem ? problemId : undefined}
                            value={row.value ?? ""}
                            placeholder={row.default ?? ""}
                            onChange={(e) => onChange?.(row.name, e.target.value)}
                            className={cn("h-7 min-w-32 flex-1 font-mono", problem && "border-status-red")}
                          />
                          {row.willReset && (
                            <span className="shrink-0 text-xs text-status-amber">will reset</span>
                          )}
                          {row.edited && onReset && (
                            <button
                              type="button"
                              aria-label={`Discard edit for ${row.name}`}
                              className="shrink-0 text-xs text-muted underline"
                              onClick={() => onReset(row.name)}
                            >
                              Discard edit
                            </button>
                          )}
                          {row.willReset && onUndoReset && (
                            <button
                              type="button"
                              aria-label={`Undo reset of ${row.name}`}
                              className="shrink-0 text-xs text-muted underline"
                              onClick={() => onUndoReset(row.name)}
                            >
                              Undo reset
                            </button>
                          )}
                          {row.resettable && onResetToDefault && (
                            <button
                              type="button"
                              aria-label={`Reset ${row.name} to default`}
                              className="shrink-0 text-xs text-muted underline"
                              onClick={() => onResetToDefault(row.name)}
                            >
                              Reset to default
                            </button>
                          )}
                        </div>
                        {problem && (
                          <p id={problemId} className="text-xs text-status-red">
                            {problem}
                          </p>
                        )}
                      </div>
                    ) : (
                      (row.value ?? <span className="text-muted">{row.default ?? "—"}</span>)
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-muted">{row.default ?? "—"}</TableCell>
                  <TableCell>{row.data_type}</TableCell>
                  <TableCell>
                    {row.apply_type === "static" ? (
                      <span className="text-status-amber">static (reboot)</span>
                    ) : (
                      "dynamic"
                    )}
                  </TableCell>
                  <TableCell>{row.is_modifiable ? "yes" : "no"}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
