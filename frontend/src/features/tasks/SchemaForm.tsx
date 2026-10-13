import { type ComponentType, useId } from "react";
import { BooleanField } from "./widgets/BooleanField";
import { DateTimeField } from "./widgets/DateTimeField";
import { EngineVersionPicker } from "./widgets/EngineVersionPicker";
import { FamilyPicker } from "./widgets/FamilyPicker";
import { HiddenField } from "./widgets/HiddenField";
import { InstanceClassField } from "./widgets/InstanceClassField";
import { NumberField } from "./widgets/NumberField";
import { ParameterTableField } from "./widgets/ParameterTableField";
import { ProfilePicker } from "./widgets/ProfilePicker";
import { RegionPicker } from "./widgets/RegionPicker";
import { ResourcePicker } from "./widgets/ResourcePicker";
import { SecurityGroupsField } from "./widgets/SecurityGroupsField";
import { SelectField } from "./widgets/SelectField";
import { SubnetGroupField } from "./widgets/SubnetGroupField";
import { TagsField } from "./widgets/TagsField";
import { TargetPicker } from "./widgets/TargetPicker";
import { TextField } from "./widgets/TextField";
import { UpgradeReviewField } from "./widgets/UpgradeReviewField";
import { WindowField } from "./widgets/WindowField";

/** UI hints the backend attaches to each property under its `ui` key; unknown extras pass through. */
export interface UiHints {
  widget?: string;
  /** The words the field is labelled with; the schema's title, then the field's name, stand in without it. */
  label?: string;
  help?: string;
  group?: string;
  order?: number;
  labels?: Record<string, string>;
  /** `resource` widget: the one kind of resource to list. */
  resource_kind?: string;
  /** `resource_picker` widget: the kinds of resource to list. */
  kinds?: string[];
  /** `resource_picker` widget: only resources with one of these ownerships are listed. */
  ownership?: string[];
  /**
   * `resource` widget listing parameter groups: only groups of the family of the group the task's resource
   * has attached, and of the scope that resource takes (`cluster` for a cluster, `instance` otherwise).
   */
  same_family_and_scope_as_resource?: boolean;
  /**
   * `resource` widget listing instances: only the members of the task's cluster (instances whose
   * `relationships.cluster_identifier` is the task's resource identifier); the value is the member's identifier.
   */
  members_of_resource?: boolean;
  /** `resource` widget listing parameter groups on a cluster's task: instance groups of the cluster's family. */
  instance_group_of_cluster_family?: boolean;
  /**
   * `resource` widget listing parameter groups on a restore: groups of the engine family restored (the
   * snapshot's, or the source's), of the scope restored (`cluster` for a cluster, `instance` otherwise).
   */
  group_of_restored_family?: boolean;
  /** `resource` widget listing parameter groups: only groups of this scope. */
  group_scope?: "instance" | "cluster";
  /**
   * `resource` widget listing parameter groups on a create: groups of the family of the `engine_version` chosen
   * in the same form (whose own `ui.engine` names the engine), leaving out AWS's default groups.
   */
  group_of_chosen_version_family?: boolean;
  /**
   * `engine_version` and `instance_class` widgets on a task with no resource (a create): the engine to list
   * versions or classes for, `postgres` or `aurora-postgresql`.
   */
  engine?: string;
  /** `engine_version` widget on an upgrade: list only the current version's `minor` or `major` targets. */
  upgrade_kind?: "minor" | "major";
  /** `security_groups` widget: the pattern (a regular expression) every id must match. */
  item_pattern?: string;
  /** `window` widget: a `weekly` window is `ddd:hh:mm-ddd:hh:mm`, a `daily` one `hh:mm-hh:mm` (UTC). */
  window_kind?: "daily" | "weekly";
  /** `tags` widget: the value is a list of tag keys (tags to remove) instead of a key-to-value map. */
  keys_only?: boolean;
  /** `number` widget: the increment of the input (0.5 for the serverless capacities). */
  step?: number;
}

export interface PropertySchema {
  type?: string | string[];
  title?: string;
  /** JSON-schema `enum`: strings, or numbers for an integer choice such as enhanced monitoring seconds. */
  enum?: (string | number)[];
  default?: unknown;
  anyOf?: PropertySchema[];
  ui?: UiHints;
  [key: string]: unknown;
}

export interface ObjectSchema {
  type?: string;
  properties?: Record<string, PropertySchema>;
  required?: string[];
  [key: string]: unknown;
}

/** What a widget may need to know about where the task runs. */
export interface FormContext {
  environmentSlug?: string;
  resourceId?: string;
  resourceKind?: string;
  targetKey?: string;
  /** The identifier (name) of the task's resource, such as `demo-aurora`. */
  resourceIdentifier?: string;
}

/** Every widget is a function of these props. */
export interface WidgetProps {
  name: string;
  schema: PropertySchema;
  ui: UiHints;
  value: unknown;
  onChange: (value: unknown) => void;
  /**
   * Merge a patch into the whole form value, for a widget that keeps a sibling field in step with its own
   * (the parameter editor's `reset` list), in one update so neither change is lost.
   */
  onFormChange?: (patch: Record<string, unknown>) => void;
  problem?: string;
  context: FormContext;
  /** The whole form's current values, for widgets that depend on a sibling field. */
  formValue: Record<string, unknown>;
  /** The whole form's schema, for widgets that read a sibling field's hints (the engine of `engine_version`). */
  formSchema?: ObjectSchema;
  /** Id of the element(s) holding this field's help and problem text, for `aria-describedby`. */
  describedBy?: string;
  /** Problems the server reported without naming a field; a widget may claim the ones that name its items. */
  generalProblems?: string[];
}

const WIDGETS: Record<string, ComponentType<WidgetProps>> = {
  text: TextField,
  number: NumberField,
  boolean: BooleanField,
  choice: SelectField,
  resource: ResourcePicker,
  resource_picker: ResourcePicker,
  parameters: ParameterTableField,
  regions: RegionPicker,
  profile: ProfilePicker,
  target: TargetPicker,
  target_picker: TargetPicker,
  family: FamilyPicker,
  engine_version: EngineVersionPicker,
  upgrade_review: UpgradeReviewField,
  window: WindowField,
  tags: TagsField,
  instance_class: InstanceClassField,
  datetime: DateTimeField,
  security_groups: SecurityGroupsField,
  subnet_group: SubnetGroupField,
  hidden: HiddenField,
};

function widgetFor(schema: PropertySchema, ui: UiHints): ComponentType<WidgetProps> {
  if (ui.widget && Object.hasOwn(WIDGETS, ui.widget)) return WIDGETS[ui.widget] ?? TextField;
  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  if (schema.enum) return SelectField;
  if (type === "boolean") return BooleanField;
  if (type === "integer" || type === "number") return NumberField;
  return TextField;
}

/**
 * The options as the form shows them: a required switch the operator left untouched shows "off", so it is
 * sent as `false` rather than left out (which the server refuses as missing). Optional fields stay unset,
 * so a change never touches a setting the operator did not choose.
 *
 * @example
 * optionsAsShown({ properties: { deletion_protection: { type: "boolean" } }, required: ["deletion_protection"] }, {});
 * // { deletion_protection: false }
 */
export function optionsAsShown(
  schema: ObjectSchema | undefined,
  options: Record<string, unknown>,
): Record<string, unknown> {
  const shown = { ...options };
  for (const name of schema?.required ?? []) {
    const property = schema?.properties?.[name];
    if (property?.type === "boolean" && shown[name] === undefined && property.default === undefined) {
      shown[name] = false;
    }
  }
  return shown;
}

interface SchemaFormProps {
  schema: ObjectSchema;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  /** Message per field name, shown under the field. */
  problems: Record<string, string>;
  context: FormContext;
  /** Server messages that name no field; forwarded to the widgets (see `WidgetProps.generalProblems`). */
  generalProblems?: string[];
}

/**
 * Fields from a JSON schema, one widget per property chosen by its `ui.widget` (falling back to the JSON
 * type), grouped by `ui.group` and ordered by `ui.order`, with `ui.help` under each field.
 *
 * @example
 * <SchemaForm schema={definition.options_schema} value={options} onChange={setOptions} problems={{}} context={{ environmentSlug: "sandbox" }} />
 */
export function SchemaForm({ schema, value, onChange, problems, context, generalProblems }: SchemaFormProps) {
  const formId = useId();
  const entries = Object.entries(schema.properties ?? {}).map(([name, property]) => ({
    name,
    property,
    ui: property.ui ?? {},
  }));
  entries.sort((a, b) => (a.ui.order ?? 0) - (b.ui.order ?? 0));
  const groups = new Map<string, typeof entries>();
  for (const entry of entries) {
    if (entry.ui.widget === "hidden") continue;
    const group = entry.ui.group ?? "";
    groups.set(group, [...(groups.get(group) ?? []), entry]);
  }
  return (
    <div className="space-y-5">
      {[...groups.entries()].map(([group, fields]) => (
        <fieldset key={group || "default"} aria-label={group || "Options"} className="min-w-0 space-y-4">
          {group && (
            <legend className="text-xs font-medium uppercase tracking-wide text-muted">{group}</legend>
          )}
          {fields.map(({ name, property, ui }) => {
            const Widget = widgetFor(property, ui);
            const problem = problems[name];
            const helpId = `${formId}-${name}-help`;
            const problemId = `${formId}-${name}-problem`;
            const describedBy = [ui.help && helpId, problem && problemId].filter(Boolean).join(" ");
            return (
              <div key={name} className="space-y-1">
                <Widget
                  name={name}
                  schema={property}
                  ui={ui}
                  value={value[name] ?? property.default}
                  onChange={(next) => onChange({ ...value, [name]: next })}
                  onFormChange={(patch) => onChange({ ...value, ...patch })}
                  problem={problem}
                  context={context}
                  formValue={value}
                  formSchema={schema}
                  describedBy={describedBy || undefined}
                  generalProblems={generalProblems}
                />
                {ui.help && (
                  <p id={helpId} className="text-xs text-muted">
                    {ui.help}
                  </p>
                )}
                {problem && (
                  <p id={problemId} className="text-xs text-status-red">
                    {problem}
                  </p>
                )}
              </div>
            );
          })}
        </fieldset>
      ))}
    </div>
  );
}
