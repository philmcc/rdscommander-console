import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/api/client";
import type { HistoryPage } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ExecutionRecordView } from "@/features/operations/ExecutionRecordView";
import { describeError } from "@/lib/describeError";

/** The most records one search returns. */
const RECORD_LIMIT = 200;

/**
 * Searchable, exportable record of everything that ever ran in an environment. The filters apply when the
 * form is submitted; the first search runs on its own, and again whenever the environment changes.
 *
 * @example
 * <ExecutionRecordsView environmentSlug="sandbox" />
 */
export function ExecutionRecordsView({ environmentSlug }: { environmentSlug: string }) {
  const [recordKind, setRecordKind] = useState("");
  const [targetKey, setTargetKey] = useState("");
  const [text, setText] = useState("");
  const [since, setSince] = useState("");
  const [page, setPage] = useState<HistoryPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Counts the searches started; only the latest one may show its answer.
  const latestSearch = useRef(0);

  const query = useCallback(
    () => ({
      environment_slug: environmentSlug,
      record_kind: recordKind,
      target_key: targetKey,
      text,
      since: since ? new Date(since).toISOString() : "",
    }),
    [environmentSlug, recordKind, targetKey, text, since],
  );

  const search = useCallback(async () => {
    latestSearch.current += 1;
    const thisSearch = latestSearch.current;
    try {
      const found = await apiGet<HistoryPage>("/api/v1/history", { ...query(), limit: RECORD_LIMIT });
      if (thisSearch !== latestSearch.current) return;
      setPage(found);
      setError(null);
    } catch (caught) {
      if (thisSearch !== latestSearch.current) return;
      setError(describeError(caught));
    }
  }, [query]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: load once per environment; filters apply on submit
  useEffect(() => {
    setPage(null);
    void search();
  }, [environmentSlug]);

  const exportUrl = () => {
    const parameters = new URLSearchParams();
    for (const [key, value] of Object.entries(query())) if (value) parameters.set(key, value);
    window.open(`/api/v1/history/export?${parameters.toString()}`, "_blank");
  };

  return (
    <div className="space-y-4">
      <form
        className="flex flex-wrap items-end gap-3 text-sm"
        onSubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label className="space-y-1" htmlFor="history-kind">
          <span>Kind</span>
          <select
            id="history-kind"
            className="block h-8 rounded-md border border-hairline bg-surface px-2"
            value={recordKind}
            onChange={(e) => setRecordKind(e.target.value)}
          >
            <option value="">any</option>
            <option value="terraform">terraform</option>
            <option value="aws_api">aws_api</option>
            <option value="git">git</option>
          </select>
        </label>
        <label className="space-y-1" htmlFor="history-target">
          <span>Target</span>
          <Input
            id="history-target"
            className="h-8 font-mono"
            value={targetKey}
            onChange={(e) => setTargetKey(e.target.value)}
            placeholder="env/account/region"
          />
        </label>
        <label className="space-y-1" htmlFor="history-text">
          <span>Text</span>
          <Input
            id="history-text"
            className="h-8 font-mono"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="command, API name, output"
          />
        </label>
        <label className="space-y-1" htmlFor="history-since">
          <span>Since</span>
          <Input
            id="history-since"
            className="h-8"
            type="datetime-local"
            value={since}
            onChange={(e) => setSince(e.target.value)}
          />
        </label>
        <div className="flex gap-2">
          <Button type="submit" size="sm">
            Search
          </Button>
          <Button type="button" size="sm" variant="outline" onClick={exportUrl}>
            Export JSON
          </Button>
        </div>
      </form>
      {error && (
        <p role="alert" className="text-sm text-status-red">
          Could not search the records: {error}
        </p>
      )}
      <div className="space-y-2">
        {!page && !error && <p className="text-sm text-muted">Loading…</p>}
        {page?.records.map((record) => (
          <ExecutionRecordView key={record.id} record={record} />
        ))}
        {page && page.records.length === 0 && <p className="text-sm text-muted">No records match.</p>}
      </div>
    </div>
  );
}
