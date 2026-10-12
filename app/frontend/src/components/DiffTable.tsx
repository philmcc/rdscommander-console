import type { ReactNode } from "react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export interface DiffRow {
  attribute: string;
  before: unknown;
  after: unknown;
  flags?: string[];
}

interface DiffTableProps {
  rows: DiffRow[];
  /** Header of the before column; an element such as a link to the thing it names. */
  beforeLabel?: ReactNode;
  /** Header of the after column; an element such as a link to the thing it names. */
  afterLabel?: ReactNode;
}

/**
 * Longest word (a run with no space or hyphen to wrap at) a cell keeps whole. Three text columns of words this
 * long, their padding and the flags still fit the 560px task panel, so keeping them whole never pushes the
 * flags column out of view.
 */
const LONGEST_WORD_KEPT_WHOLE = 16;
/** Wraps at spaces and hyphens; the column stays at least as wide as its longest word, so 65536 never splits. */
const WRAP_AT_WORDS = "whitespace-normal [overflow-wrap:break-word]";
/** Also lets the table narrow the column below a word, which a word too long for any column needs. */
const WRAP_ANYWHERE = "whitespace-normal [overflow-wrap:anywhere]";

/**
 * How a cell's text may wrap: at words only, unless one word is too long to keep whole in the task panel.
 *
 * `overflow-wrap: anywhere` counts every character as a place to break when the table sizes its columns, so
 * the table may squeeze a column until a short number splits (`6553` / `6`); `break-word` keeps the column at
 * least as wide as its longest word and breaks inside a word only when that word is wider than the column.
 *
 * @example
 * wrappingFor("65536"); // "whitespace-normal [overflow-wrap:break-word]"
 * wrappingFor("pg_stat_statements,auto_explain"); // "whitespace-normal [overflow-wrap:anywhere]"
 */
function wrappingFor(text: string): string {
  const longestWordLength = Math.max(...text.split(/[\s-]+/).map((word) => word.length));
  return longestWordLength > LONGEST_WORD_KEPT_WHOLE ? WRAP_ANYWHERE : WRAP_AT_WORDS;
}

function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

/**
 * Attribute, before, after and flags for a change set; every value is monospace and flags are amber.
 *
 * @example
 * <DiffTable rows={[{ attribute: "multi_az", before: false, after: true, flags: ["reboot"] }]} />
 */
export function DiffTable({ rows, beforeLabel = "Before", afterLabel = "After" }: DiffTableProps) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Attribute</TableHead>
          <TableHead>{beforeLabel}</TableHead>
          <TableHead>{afterLabel}</TableHead>
          <TableHead>Flags</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => {
          const before = show(row.before);
          const after = show(row.after);
          return (
            <TableRow key={row.attribute} className="h-9">
              <TableCell className={`font-mono ${wrappingFor(row.attribute)}`}>{row.attribute}</TableCell>
              <TableCell className={`font-mono text-muted ${wrappingFor(before)}`}>{before}</TableCell>
              <TableCell className={`font-mono ${wrappingFor(after)}`}>{after}</TableCell>
              <TableCell className="whitespace-normal">
                {row.flags?.map((flag) => (
                  <span
                    key={flag}
                    className="mr-1 rounded border border-status-amber/40 px-1 text-xs text-status-amber"
                  >
                    {flag}
                  </span>
                ))}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
