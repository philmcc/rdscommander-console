import type { PropertySchema, UiHints } from "../SchemaForm";

/**
 * The words a field is labelled with: the catalogue's `ui.label`, else the schema's title, else the field's
 * name, so a field the backend has not labelled still shows something.
 *
 * @example
 * fieldLabel("is_production", { title: "Is Production" }, { label: "Production environment" }); // "Production environment"
 * fieldLabel("note", {}, {}); // "note"
 */
export function fieldLabel(name: string, schema: PropertySchema, ui: UiHints): string {
  return ui.label ?? schema.title ?? name;
}
