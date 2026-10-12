import { type KeyboardEvent, type MouseEvent, type ReactNode, useMemo, useState } from "react";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

export interface Column<Row> {
  key: string;
  header: string;
  render: (row: Row) => ReactNode;
  /** Makes the column sortable (its header becomes a button) and searchable by the filter box. */
  sortValue?: (row: Row) => string | number | null | undefined;
  className?: string;
}

interface DataTableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string;
  /** Called on click or Enter; rows are focusable and arrow keys move between them. */
  onOpen?: (row: Row) => void;
  filterPlaceholder?: string;
  /** Text the filter box matches against; defaults to the sort values of every column. */
  searchText?: (row: Row) => string;
  /** Shown instead of rows when the filter leaves nothing. */
  empty?: ReactNode;
}

const INTERACTIVE_SELECTOR = "a,button,input,select,textarea,[role=button]";

type SortState = { key: string; direction: 1 | -1 } | null;

function focusRow(candidate: Element | null): void {
  if (candidate instanceof HTMLElement) candidate.focus();
}

/**
 * Below the `sm` breakpoint (phone width) the table becomes stacked cards: the header row turns into a
 * wrapped line of sort buttons, each row into a card, and each cell into a line labelled with its column
 * (from `data-label`). One markup serves both widths, so keyboard and screen-reader behaviour stay the same.
 */
const STACKED_TABLE = "max-sm:block";
const STACKED_SECTION = "max-sm:block";
const STACKED_HEADER_ROW =
  "max-sm:flex max-sm:flex-wrap max-sm:gap-x-3 max-sm:border-b max-sm:px-2 max-sm:py-1";
const STACKED_HEADER_CELL = "max-sm:h-auto max-sm:p-0";
const STACKED_ROW = "max-sm:block max-sm:h-auto max-sm:px-1 max-sm:py-2";
const STACKED_CELL =
  "max-sm:flex max-sm:items-start max-sm:justify-between max-sm:gap-3 max-sm:py-0.5 max-sm:whitespace-normal " +
  "max-sm:before:shrink-0 max-sm:before:text-xs max-sm:before:text-muted max-sm:before:content-[attr(data-label)]";

/**
 * Dense table: sticky header, sortable columns, filter box, 36px rows, arrow keys to move and Enter to open;
 * stacked cards at phone width.
 *
 * @example
 * <DataTable rows={items} rowKey={(r) => r.id} onOpen={open} columns={[{ key: "name", header: "Name", sortValue: (r) => r.name, render: (r) => r.name }]} />
 */
export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  onOpen,
  filterPlaceholder = "Filter",
  searchText,
  empty,
}: DataTableProps<Row>) {
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<SortState>(null);
  const visible = useMemo(() => {
    const text = filter.toLowerCase();
    let list = rows.filter(
      (row) =>
        !text ||
        (searchText ? searchText(row) : columns.map((c) => String(c.sortValue?.(row) ?? "")).join(" "))
          .toLowerCase()
          .includes(text),
    );
    if (sort) {
      const column = columns.find((c) => c.key === sort.key);
      if (column?.sortValue) {
        const value = column.sortValue;
        list = [...list].sort((a, b) => {
          const left = value(a) ?? "";
          const right = value(b) ?? "";
          return (left < right ? -1 : left > right ? 1 : 0) * sort.direction;
        });
      }
    }
    return list;
  }, [rows, filter, sort, columns, searchText]);
  const toggleSort = (key: string) =>
    setSort((current) =>
      current?.key === key ? { key, direction: current.direction === 1 ? -1 : 1 } : { key, direction: 1 },
    );
  const onRowClick = (event: MouseEvent<HTMLTableRowElement>, row: Row) => {
    // A click on a link, button or field inside a cell belongs to that control and must not also open the row.
    if (event.target instanceof Element && event.target.closest(INTERACTIVE_SELECTOR)) return;
    onOpen?.(row);
  };
  const onRowKeyDown = (event: KeyboardEvent<HTMLTableRowElement>, row: Row) => {
    // Keys pressed inside a control in the row belong to that control.
    if (event.target !== event.currentTarget) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusRow(event.currentTarget.nextElementSibling);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusRow(event.currentTarget.previousElementSibling);
    } else if (event.key === "Enter" && onOpen) {
      event.preventDefault();
      onOpen(row);
    }
  };
  return (
    <div className="space-y-2">
      <Input
        aria-label={filterPlaceholder}
        placeholder={filterPlaceholder}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        className="h-8 max-w-sm"
      />
      {/* The scroll container is this wrapper, so the sticky header sticks to it rather than to the table's own wrapper. */}
      <div className="max-h-[70vh] overflow-auto rounded-lg border border-hairline bg-surface [&_[data-slot=table-container]]:overflow-visible">
        <Table className={STACKED_TABLE}>
          <TableHeader className={cn("sticky top-0 z-10 bg-surface", STACKED_SECTION)}>
            <TableRow className={STACKED_HEADER_ROW}>
              {columns.map((column) => (
                <TableHead
                  key={column.key}
                  className={cn(column.className, STACKED_HEADER_CELL)}
                  aria-sort={
                    sort?.key === column.key ? (sort.direction === 1 ? "ascending" : "descending") : undefined
                  }
                >
                  {column.sortValue ? (
                    <button
                      type="button"
                      aria-label={`Sort by ${column.header}`}
                      onClick={() => toggleSort(column.key)}
                      className="inline-flex items-center gap-1"
                    >
                      {column.header}
                      {sort?.key === column.key && (
                        <span aria-hidden="true">{sort.direction === 1 ? "↑" : "↓"}</span>
                      )}
                    </button>
                  ) : (
                    column.header
                  )}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody className={STACKED_SECTION}>
            {visible.length === 0 && (
              <TableRow>
                <TableCell colSpan={columns.length} className="h-9 text-muted">
                  {empty ?? "Nothing matches."}
                </TableCell>
              </TableRow>
            )}
            {visible.map((row) => (
              <TableRow
                key={rowKey(row)}
                tabIndex={0}
                onKeyDown={(e) => onRowKeyDown(e, row)}
                onClick={(e) => onRowClick(e, row)}
                className={cn(
                  "h-9 focus-visible:bg-accent/10 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent",
                  onOpen && "cursor-pointer",
                  STACKED_ROW,
                )}
              >
                {columns.map((column) => (
                  <TableCell
                    key={column.key}
                    data-label={column.header}
                    className={cn("py-0", column.className, STACKED_CELL)}
                  >
                    {column.render(row)}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
