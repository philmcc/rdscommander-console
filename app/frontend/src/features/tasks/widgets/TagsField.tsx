import { useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";

/** Tag key prefixes that belong to RDSCommander and AWS; the backend refuses to change them. */
const PROTECTED_TAG_PREFIXES = ["rdscommander:", "aws:"] as const;

/**
 * Whether a tag key is reserved (starts with `rdscommander:` or `aws:`) and so cannot be set or removed here.
 *
 * @example
 * isProtectedTagKey("rdscommander:managed"); // true
 * isProtectedTagKey("team"); // false
 */
export function isProtectedTagKey(key: string): boolean {
  return PROTECTED_TAG_PREFIXES.some((prefix) => key.startsWith(prefix));
}

interface TagRow {
  rowId: number;
  key: string;
  value: string;
}

function initialRows(value: unknown): Omit<TagRow, "rowId">[] {
  if (Array.isArray(value))
    return value.filter((key): key is string => typeof key === "string").map((key) => ({ key, value: "" }));
  if (value !== null && typeof value === "object")
    return Object.entries(value).map(([key, tagValue]) => ({ key, value: String(tagValue) }));
  return [];
}

/** Why a row cannot be emitted, or `null` when it can; an empty key is simply not finished yet. */
function rowProblem(row: TagRow, rows: TagRow[]): string | null {
  if (isProtectedTagKey(row.key)) return "Keys starting with rdscommander: or aws: cannot be changed here.";
  if (row.key !== "" && rows.some((other) => other.rowId < row.rowId && other.key === row.key))
    return `The key ${row.key} is already used above.`;
  return null;
}

/**
 * Tags as key and value rows with an "Add tag" button; the value is a map. With `ui.keys_only` it is a list of
 * keys (tags to remove) and the value is an array of keys. Rows with an empty or reserved key (`rdscommander:`
 * or `aws:`) or a repeated key show the problem beside them and are left out of what is emitted.
 *
 * @example
 * <TagsField name="tags" schema={{ type: "object" }} ui={{ widget: "tags" }} value={{ team: "orders" }} onChange={setTags} context={{}} formValue={{}} />
 */
export function TagsField({ name, schema, ui, value, onChange, problem, describedBy }: WidgetProps) {
  const idPrefix = useId();
  const keysOnly = ui.keys_only === true;
  const nextRowId = useRef(0);
  const withIds = (rows: Omit<TagRow, "rowId">[]): TagRow[] =>
    rows.map((row) => ({ ...row, rowId: nextRowId.current++ }));
  const [rows, setRows] = useState<TagRow[]>(() => withIds(initialRows(value)));
  // A value that arrives different from the one before it and from what this widget last emitted came from
  // outside (a form reset, a restored draft) and replaces the rows. The widget's own emits come back equal
  // to what it emitted, so a row it left out (unfinished or refused) stays on screen.
  const incomingKey = JSON.stringify(initialRows(value));
  const [previousIncomingKey, setPreviousIncomingKey] = useState(incomingKey);
  const [lastEmittedKey, setLastEmittedKey] = useState(incomingKey);
  if (incomingKey !== previousIncomingKey) {
    setPreviousIncomingKey(incomingKey);
    if (incomingKey !== lastEmittedKey) {
      setLastEmittedKey(incomingKey);
      setRows(withIds(initialRows(value)));
    }
  }
  const label = fieldLabel(name, schema, ui);

  const update = (next: TagRow[]) => {
    setRows(next);
    const emittable = next.filter((row) => row.key !== "" && rowProblem(row, next) === null);
    setLastEmittedKey(
      JSON.stringify(emittable.map((row) => ({ key: row.key, value: keysOnly ? "" : row.value }))),
    );
    onChange(
      keysOnly
        ? emittable.map((row) => row.key)
        : Object.fromEntries(emittable.map((row) => [row.key, row.value])),
    );
  };
  const patchRow = (rowId: number, patch: Partial<TagRow>) =>
    update(rows.map((row) => (row.rowId === rowId ? { ...row, ...patch } : row)));

  return (
    <fieldset className="min-w-0 space-y-2 text-sm">
      <legend className="mb-1">{label}</legend>
      {rows.map((row, index) => {
        const position = index + 1;
        const rowMessage = rowProblem(row, rows);
        const messageId = `${idPrefix}-row-${row.rowId}`;
        return (
          <div key={row.rowId} className="space-y-1">
            <div className="flex items-center gap-2">
              <Input
                aria-label={`Tag key ${position}`}
                aria-invalid={rowMessage || problem ? true : undefined}
                aria-describedby={
                  [describedBy, rowMessage ? messageId : undefined].filter(Boolean).join(" ") || undefined
                }
                value={row.key}
                onChange={(e) => patchRow(row.rowId, { key: e.target.value })}
                autoComplete="off"
                className="font-mono"
              />
              {!keysOnly && (
                <Input
                  aria-label={`Tag value ${position}`}
                  aria-describedby={describedBy}
                  value={row.value}
                  onChange={(e) => patchRow(row.rowId, { value: e.target.value })}
                  autoComplete="off"
                  className="font-mono"
                />
              )}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={`Remove tag ${position}`}
                onClick={() => update(rows.filter((other) => other.rowId !== row.rowId))}
              >
                Remove
              </Button>
            </div>
            {rowMessage && (
              <p id={messageId} className="text-xs text-status-red">
                {rowMessage}
              </p>
            )}
          </div>
        );
      })}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setRows([...rows, ...withIds([{ key: "", value: "" }])])}
      >
        Add tag
      </Button>
    </fieldset>
  );
}
