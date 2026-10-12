import type { WidgetProps } from "../SchemaForm";
import { CheckboxList } from "./CheckboxList";
import { fieldLabel } from "./fieldLabel";

/** The regions offered when importing an environment. */
export const REGIONS = [
  "us-east-1",
  "us-east-2",
  "us-west-1",
  "us-west-2",
  "ca-central-1",
  "eu-west-1",
  "eu-west-2",
  "eu-west-3",
  "eu-central-1",
  "eu-north-1",
  "ap-southeast-1",
  "ap-southeast-2",
  "ap-northeast-1",
  "ap-south-1",
  "sa-east-1",
];

const REGION_OPTIONS = REGIONS.map((region) => ({ value: region, label: region }));

/** Checkboxes over the standard region list. */
export function RegionPicker({ name, schema, ui, value, onChange, describedBy }: WidgetProps) {
  const selected = Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  return (
    <CheckboxList
      legend={fieldLabel(name, schema, ui)}
      options={REGION_OPTIONS}
      selected={selected}
      onChange={onChange}
      describedBy={describedBy}
      className="grid-cols-2 sm:grid-cols-3"
    />
  );
}
