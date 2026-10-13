import { createContext, type ReactNode, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { NavigateFunction } from "react-router";
import type { AttentionItem } from "@/api/types";
import { TaskPanel } from "./TaskPanel";

/** What opens the task panel. */
export interface TaskRequest {
  /** Operation kind from the catalogue; empty when only `operationId` is known. */
  kind: string;
  environmentSlug?: string;
  resourceId?: string;
  resourceIdentifier?: string;
  resourceKind?: string;
  targetKey?: string;
  initialOptions?: Record<string, unknown>;
  /** An existing operation: the panel opens on its review, approval or progress instead of the options. */
  operationId?: string;
}

interface OpenTask {
  request: TaskRequest;
  /** Distinguishes one opening from the next, so the panel starts fresh each time. */
  serial: number;
}

interface TaskState {
  current: TaskRequest | null;
  open: (request: TaskRequest) => void;
  close: () => void;
}

const TaskContext = createContext<TaskState | null>(null);

/**
 * Holds the single open task and mounts its panel. The panel never stacks: opening another task while one
 * started from the options asks first, whereas reviewing an existing operation is replaced silently.
 */
export function TaskProvider({ children }: { children: ReactNode }) {
  const [openTask, setOpenTask] = useState<OpenTask | null>(null);
  // Read when a request arrives, so the confirmation is asked once, outside any state updater.
  const openTaskRef = useRef<OpenTask | null>(null);
  const lastSerial = useRef(0);
  const open = useCallback((request: TaskRequest) => {
    const existing = openTaskRef.current;
    if (
      existing &&
      !existing.request.operationId &&
      !window.confirm("Close the current task and start another?")
    ) {
      return;
    }
    lastSerial.current += 1;
    openTaskRef.current = { request, serial: lastSerial.current };
    setOpenTask(openTaskRef.current);
  }, []);
  const close = useCallback(() => {
    openTaskRef.current = null;
    setOpenTask(null);
  }, []);
  const value = useMemo<TaskState>(
    () => ({ current: openTask?.request ?? null, open, close }),
    [openTask, open, close],
  );
  return (
    <TaskContext.Provider value={value}>
      {children}
      <TaskPanel key={openTask?.serial} />
    </TaskContext.Provider>
  );
}

/** Open or close the task panel from anywhere under `TaskProvider`. */
export function useTask(): TaskState {
  const value = useContext(TaskContext);
  if (!value) throw new Error("useTask must be used inside TaskProvider");
  return value;
}

/**
 * The action that resolves an inbox row.
 *
 * - Awaiting approval: the task panel on that operation's review.
 * - A row with a resolving task (`action_kind`): the task panel for it, on the row's resource when it has a
 *   page, with the row's ready-made options.
 * - A row about an operation (a failure): the operation's page.
 * - Any other row about a resource: the resource's page, on its history for a change made outside the
 *   console.
 * - A row that points nowhere: nothing.
 *
 * @example
 * openForAttentionItem(item, task.open, navigate);
 */
export function openForAttentionItem(
  item: AttentionItem,
  open: (request: TaskRequest) => void,
  navigate: NavigateFunction,
): void {
  if (item.kind === "awaiting_approval" && item.operation_id) {
    open({ kind: "", operationId: item.operation_id, environmentSlug: item.environment_slug });
    return;
  }
  if (item.action_kind) {
    open({
      kind: item.action_kind,
      environmentSlug: item.environment_slug,
      resourceId: item.resource_id ?? undefined,
      resourceIdentifier: item.resource_identifier ?? undefined,
      initialOptions: item.action_options,
    });
    return;
  }
  if (item.operation_id) {
    navigate(`/operations/${item.operation_id}`);
    return;
  }
  if (item.resource_id) {
    navigate(`/resources/${item.resource_id}${item.kind === "outside_change" ? "#history" : ""}`);
  }
}
