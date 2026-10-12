import { useId } from "react";
import type { ResourcePage } from "@/api/types";
import { Input } from "@/components/ui/input";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";
import { useApiGet } from "./useApiGet";

interface InstanceClassList {
  classes: string[];
}

/** The query for the classes AWS offers: of the task's resource, or of the engine and version a create chose. */
function classQuery(
  context: WidgetProps["context"],
  ui: WidgetProps["ui"],
  formValue: Record<string, unknown>,
) {
  if (!context.environmentSlug) return null;
  if (context.resourceId)
    return { environment_slug: context.environmentSlug, resource_id: context.resourceId };
  const engineVersion = formValue.engine_version;
  if (typeof ui.engine !== "string" || typeof engineVersion !== "string" || engineVersion === "") return null;
  const targetKey = typeof formValue.target_key === "string" ? formValue.target_key : context.targetKey;
  return {
    environment_slug: context.environmentSlug,
    target_key: targetKey,
    engine: ui.engine,
    engine_version: engineVersion,
  };
}

/**
 * An instance class as a text input backed by the classes AWS offers for the resource's engine and version,
 * so a class can be picked from the browser's suggestions or typed. On a create (no resource, `ui.engine`
 * given) the classes are those of that engine and the version and target chosen in the form, and a note asks
 * for the version first. The list may be empty (the simulated estate cannot say) or fail to load; typing
 * always works. The resource's current class is shown beneath.
 *
 * @example
 * <InstanceClassField name="instance_class" schema={{ type: "string" }} ui={{ widget: "instance_class" }} value="" onChange={setClass} context={{ environmentSlug: "sandbox", resourceId: "abc" }} formValue={{}} />
 */
export function InstanceClassField({
  name,
  schema,
  ui,
  value,
  onChange,
  problem,
  context,
  formValue,
  describedBy,
}: WidgetProps) {
  const inputId = useId();
  const listId = useId();
  const query = classQuery(context, ui, formValue);
  const classes = useApiGet<InstanceClassList>(
    query ? "/api/v1/lifecycle/instance-classes" : null,
    query ?? {},
  );
  const waitsForVersion = !context.resourceId && typeof ui.engine === "string" && query === null;
  const resourcePage = useApiGet<ResourcePage>(
    context.resourceId ? `/api/v1/resources/${context.resourceId}` : null,
  );
  const currentClass = resourcePage.data?.record.facts?.db_instance_class;
  return (
    <div className="space-y-1 text-sm">
      <label htmlFor={inputId} className="block">
        {fieldLabel(name, schema, ui)}
      </label>
      <Input
        id={inputId}
        list={listId}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="off"
        className="max-w-64 font-mono"
      />
      <datalist id={listId}>
        {(classes.data?.classes ?? []).map((instanceClass) => (
          <option key={instanceClass} value={instanceClass} />
        ))}
      </datalist>
      {waitsForVersion && (
        <p className="text-xs text-muted">Choose an engine version to see the classes AWS offers.</p>
      )}
      {typeof currentClass === "string" && currentClass !== "" && (
        <p className="text-xs text-muted">Current: {currentClass}</p>
      )}
    </div>
  );
}
