import { useEffect, useId, useState } from "react";
import { useNavigate } from "react-router";
import type { AttentionInbox, AttentionItem } from "@/api/types";
import { useEnvironment } from "@/app/EnvironmentContext";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { loadCatalogue } from "@/features/tasks/catalogue";
import { openForAttentionItem, useTask } from "@/features/tasks/TaskContext";
import { useAttention } from "./useAttention";

/** One section of the inbox as the page shows it. */
interface SectionSpec {
  key: keyof AttentionInbox;
  title: string;
  /** The label of a row's button when no catalogue title applies. */
  action: string;
  /**
   * The label of a row that offers no task (its detail says why), when that row leads somewhere other than
   * `action` promises: a blue-side row that cannot be decommissioned opens its upgrade.
   */
  actionWithoutTask?: string;
}

/** The inbox's sections in the order they are shown. */
const SECTIONS: SectionSpec[] = [
  { key: "awaiting_approval", title: "Awaiting approval", action: "Review" },
  { key: "pending_reboot", title: "Pending reboot", action: "Reboot instance" },
  { key: "pending_maintenance", title: "Pending maintenance", action: "Open resource" },
  { key: "drift", title: "Drift from the record", action: "Resolve drift" },
  { key: "outside_changes", title: "Changed outside the console", action: "Review change" },
  { key: "failed_recent", title: "Failed in the last seven days", action: "Open operation" },
  {
    key: "blue_side",
    title: "Blue side after upgrade",
    action: "Decommission",
    actionWithoutTask: "Open operation",
  },
  { key: "foreign_owned", title: "Owned by other tooling", action: "Open resource" },
  { key: "unmanaged", title: "Not under Terraform", action: "Adopt resources" },
];

/** A key for each row; rows that read the same get a suffix, so React can tell them apart. */
function rowKeys(items: AttentionItem[]): string[] {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const base = [item.kind, item.title, item.detail, item.operation_id, item.resource_id].join("|");
    const occurrence = (seen.get(base) ?? 0) + 1;
    seen.set(base, occurrence);
    return occurrence === 1 ? base : `${base}#${occurrence}`;
  });
}

/**
 * A row's title with its identifier in monospace: the resource's identifier wherever the title names it, or
 * the part of an operation's title after its colon (`<what>: <identifier>`).
 */
function RowTitle({ item }: { item: AttentionItem }) {
  const identifier = item.resource_identifier;
  const at = identifier ? item.title.indexOf(identifier) : -1;
  if (identifier && at >= 0) {
    return (
      <>
        {item.title.slice(0, at)}
        <span className="font-mono">{identifier}</span>
        {item.title.slice(at + identifier.length)}
      </>
    );
  }
  const separator = item.operation_id ? item.title.indexOf(": ") : -1;
  if (separator < 0) return <>{item.title}</>;
  return (
    <>
      {item.title.slice(0, separator + 2)}
      <span className="font-mono">{item.title.slice(separator + 2)}</span>
    </>
  );
}

/** One row; its button is described by the row's text, since the same label repeats down a section. */
function AttentionRow({ item, label, onAct }: { item: AttentionItem; label: string; onAct: () => void }) {
  const descriptionId = useId();
  return (
    <li className="flex min-h-9 items-center gap-3 border-b border-hairline px-3 py-1.5 last:border-0">
      <span id={descriptionId} className="min-w-0 flex-1 break-words">
        <RowTitle item={item} /> <span className="text-muted">{item.detail}</span>
      </span>
      <Button
        size="sm"
        variant="outline"
        className="shrink-0"
        aria-describedby={descriptionId}
        onClick={onAct}
      >
        {label}
      </Button>
    </li>
  );
}

/** One section of the inbox, headed by its title and row count. */
function AttentionSection({
  title,
  items,
  labelFor,
  onAct,
}: {
  title: string;
  items: AttentionItem[];
  labelFor: (item: AttentionItem) => string;
  onAct: (item: AttentionItem) => void;
}) {
  const headingId = useId();
  const keys = rowKeys(items);
  return (
    <section aria-labelledby={headingId} className="rounded-lg border border-hairline bg-surface">
      <h2 id={headingId} className="border-b border-hairline px-3 py-2 text-sm font-medium">
        {title} ({items.length})
      </h2>
      <ul>
        {items.map((item, index) => (
          <AttentionRow key={keys[index]} item={item} label={labelFor(item)} onAct={() => onAct(item)} />
        ))}
      </ul>
    </section>
  );
}

/**
 * Everything that needs a person in the selected environment, each row with the action that resolves it.
 * The button of a row with a resolving task carries that task's catalogue title. The old instances a major
 * upgrade kept (its blue side) are listed with Decommission, or with the way to the upgrade and the reason
 * when they cannot be decommissioned; a major upgrade that failed or was rejected offers the cleanup of the
 * deployment it left in place.
 */
export default function AttentionPage() {
  const { selected } = useEnvironment();
  const { inbox, error } = useAttention(selected?.slug);
  const { open } = useTask();
  const navigate = useNavigate();
  const [titles, setTitles] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    loadCatalogue().then(
      (definitions) => {
        if (!cancelled) setTitles(Object.fromEntries(definitions.map((d) => [d.kind, d.title])));
      },
      () => {
        // Rows fall back to their section's label; opening a task reports a catalogue failure itself.
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const total = inbox ? SECTIONS.reduce((count, { key }) => count + inbox[key].length, 0) : 0;
  return (
    <>
      <PageHeader breadcrumb={selected ? [{ label: selected.display_name }] : []} title="Needs attention" />
      {error && (
        <p role="alert" className="mb-4 text-sm text-status-red">
          Could not load the inbox: {error}
        </p>
      )}
      {!inbox && !error && <p className="text-sm text-muted">Loading…</p>}
      {inbox && total === 0 && <EmptyState icon="✓" title="Nothing needs attention in this environment." />}
      <div className="space-y-6">
        {inbox &&
          SECTIONS.map(({ key, title, action, actionWithoutTask }) =>
            inbox[key].length === 0 ? null : (
              <AttentionSection
                key={key}
                title={title}
                items={inbox[key]}
                labelFor={(item) =>
                  item.action_kind ? (titles[item.action_kind] ?? action) : (actionWithoutTask ?? action)
                }
                onAct={(item) => openForAttentionItem(item, open, navigate)}
              />
            ),
          )}
      </div>
    </>
  );
}
