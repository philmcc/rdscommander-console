import { useEffect, useRef, useState } from "react";

/** The note a picker shows once it has cleared a choice that no longer fits. */
export const STALE_CHOICE_NOTE = "The earlier choice no longer fits; pick again.";

/**
 * Clears a choice the picker offered once and offers no longer, because a field it depends on changed (the
 * engine version a group's family follows, the target a subnet group belongs to). A value the picker has never
 * offered (a prefilled one) is left alone, and nothing happens while the options are still loading.
 *
 * @param chosen The current value, `""` when none.
 * @param offered Whether the options the picker shows now include `chosen`.
 * @param settled Whether those options are final (the lists they come from have loaded).
 * @param clear Emits no value for the field.
 * @returns Whether to show `STALE_CHOICE_NOTE`: from the clearing until a new value is chosen.
 *
 * @example
 * const showStaleNote = useStaleChoice(chosen, options.some((o) => o.value === chosen), loaded, () => onChange(undefined));
 */
export function useStaleChoice(
  chosen: string,
  offered: boolean,
  settled: boolean,
  clear: () => void,
): boolean {
  const wasOffered = useRef(false);
  const [cleared, setCleared] = useState(false);
  useEffect(() => {
    if (chosen === "") {
      wasOffered.current = false;
      return;
    }
    setCleared(false);
    if (!settled) return;
    if (offered) {
      wasOffered.current = true;
    } else if (wasOffered.current) {
      wasOffered.current = false;
      setCleared(true);
      clear();
    }
  }, [chosen, offered, settled, clear]);
  return cleared && chosen === "";
}
