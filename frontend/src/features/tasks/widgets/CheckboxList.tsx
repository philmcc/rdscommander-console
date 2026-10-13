import { useId } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";

export interface CheckboxListOption {
  value: string;
  label: string;
}

interface CheckboxListProps {
  /** Accessible name of the group, shown as its legend. */
  legend: string;
  options: CheckboxListOption[];
  selected: string[];
  onChange: (selected: string[]) => void;
  describedBy?: string;
  /** Shown instead of the list when there is nothing to choose from. */
  emptyMessage?: string;
  className?: string;
}

/**
 * A group of checkboxes over a set of options. Selected values missing from `options` (for example ones
 * prefilled by the caller) are listed too, so everything that will be submitted stays visible.
 *
 * @example
 * <CheckboxList legend="Regions" options={[{ value: "eu-west-2", label: "eu-west-2" }]} selected={[]} onChange={setRegions} />
 */
export function CheckboxList({
  legend,
  options,
  selected,
  onChange,
  describedBy,
  emptyMessage,
  className,
}: CheckboxListProps) {
  const groupId = useId();
  const known = new Set(options.map((option) => option.value));
  const shown = [...options, ...selected.filter((v) => !known.has(v)).map((v) => ({ value: v, label: v }))];
  const toggle = (value: string) =>
    onChange(selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value]);
  return (
    <fieldset className="min-w-0 space-y-1 text-sm" aria-describedby={describedBy}>
      <legend>{legend}</legend>
      {shown.length === 0 && emptyMessage && <p className="text-muted">{emptyMessage}</p>}
      <div className={cn("grid gap-1", className)}>
        {shown.map((option, index) => (
          <div key={option.value} className="flex items-center gap-2 font-mono">
            <Checkbox
              id={`${groupId}-${index}`}
              checked={selected.includes(option.value)}
              onCheckedChange={() => toggle(option.value)}
            />
            <label htmlFor={`${groupId}-${index}`}>{option.label}</label>
          </div>
        ))}
      </div>
    </fieldset>
  );
}
