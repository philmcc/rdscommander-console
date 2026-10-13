import { useState } from "react";
import type { ExecutionRecordSummary } from "@/api/types";
import { Button } from "@/components/ui/button";

/**
 * One execution record; the title button expands the complete output. Commands, API names and output are
 * monospace.
 *
 * @example
 * <ExecutionRecordView record={operation.execution_records[0]} />
 */
export function ExecutionRecordView({ record }: { record: ExecutionRecordSummary }) {
  const [open, setOpen] = useState(false);
  const title = record.command_line ?? record.api_name ?? record.record_kind;
  return (
    <div className="rounded-lg border border-hairline p-2">
      <Button
        variant="ghost"
        aria-expanded={open}
        className="h-auto w-full justify-start gap-2 p-1 text-left font-mono text-xs"
        onClick={() => setOpen((v) => !v)}
      >
        <span className={record.succeeded ? "text-status-green" : "text-status-red"}>
          {record.succeeded ? "ok" : "fail"}
        </span>
        <span className="text-muted">{record.record_kind}</span>
        <span className="truncate">{title}</span>
        <span className="ml-auto text-muted">{record.duration_milliseconds} ms</span>
      </Button>
      {open ? (
        <div className="mt-2 space-y-2 text-xs">
          {record.error_message ? <p className="text-status-red">{record.error_message}</p> : null}
          {record.request_parameters ? (
            <pre className="overflow-x-auto rounded bg-secondary p-2 font-mono">
              {JSON.stringify(record.request_parameters, null, 2)}
            </pre>
          ) : null}
          {record.response_payload ? (
            <pre className="overflow-x-auto rounded bg-secondary p-2 font-mono">
              {JSON.stringify(record.response_payload, null, 2)}
            </pre>
          ) : null}
          {record.standard_output ? (
            <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-secondary p-2 font-mono">
              {record.standard_output}
            </pre>
          ) : null}
          {record.standard_error ? (
            <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-secondary p-2 font-mono text-status-red">
              {record.standard_error}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
