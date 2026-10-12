import { useEffect, useRef } from "react";
import { useTask } from "./TaskContext";

/**
 * Calls `onClosed` each time the task panel closes after having been open, because the task may have changed
 * what the page shows. Opening another task in place of the open one keeps the panel open, so it calls nothing.
 *
 * @example
 * useReloadWhenTaskCloses(reload);
 */
export function useReloadWhenTaskCloses(onClosed: () => void): void {
  const { current } = useTask();
  const taskWasOpen = useRef(false);
  useEffect(() => {
    if (current) {
      taskWasOpen.current = true;
    } else if (taskWasOpen.current) {
      taskWasOpen.current = false;
      onClosed();
    }
  }, [current, onClosed]);
}
