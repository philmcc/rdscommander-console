import { UpgradeBuilderReview } from "@/features/parameters/UpgradeBuilderReview";
import type { WidgetProps } from "../SchemaForm";

/** The upgrade build's review of the groups it will create, bound to the per-scope edits. */
export function UpgradeReviewField({ value, onChange, context, formValue }: WidgetProps) {
  return (
    <UpgradeBuilderReview
      resourceId={context.resourceId ?? ""}
      targetEngineVersion={
        typeof formValue.target_engine_version === "string" ? formValue.target_engine_version : ""
      }
      overrides={(value ?? {}) as Record<string, Record<string, string>>}
      onChange={onChange}
    />
  );
}
