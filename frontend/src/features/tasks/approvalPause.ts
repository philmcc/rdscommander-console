import type { DefinitionSummary } from "@/api/types";
import type { ObjectSchema } from "./SchemaForm";

/** Whether a task stops for review and approval with the options chosen so far, and what its button says. */
export interface ApprovalPause {
  /** True when the operation will wait for the operator to review and approve it. */
  pauses: boolean;
  /** The words of the options step's submit button. */
  submitLabel: string;
}

/** The button of a task that pauses: the next thing the operator sees is the review. */
export const REVIEW_SUBMIT_LABEL = "Continue to review";

/** The button of each task that runs to the end once submitted; any other such task says "Run". */
const RUN_SUBMIT_LABELS: Record<string, string> = {
  inventory_refresh: "Refresh",
  drift_detect: "Detect drift",
  drift_resolve: "Accept into the record",
  environment_import: "Import environment",
};
const DEFAULT_RUN_SUBMIT_LABEL = "Run";

/**
 * The option whose value decides whether the task pauses: the one whose fixed choices include every value
 * `pauses_for_approval_when` names (drift resolution's `choice`).
 */
function decidingOptionOf(schema: ObjectSchema | undefined, values: string[]): string | undefined {
  const properties = Object.entries(schema?.properties ?? {});
  return properties.find(([, property]) => values.every((value) => property.enum?.includes(value)))?.[0];
}

/**
 * Whether the task pauses for approval with these options, and the words of its submit button. A task whose
 * catalogue entry names a deciding option follows the chosen value; one with nothing chosen yet, or an entry
 * that says nothing, is taken to pause. A task that runs to the end is labelled with what it does, such as
 * "Refresh", or with the chosen value's own label, such as "Accept into the record".
 *
 * @example
 * approvalPauseOf(driftResolve, { choice: "accept" }); // { pauses: false, submitLabel: "Accept into the record" }
 * approvalPauseOf(driftResolve, { choice: "restore" }); // { pauses: true, submitLabel: "Continue to review" }
 */
export function approvalPauseOf(
  definition: DefinitionSummary,
  options: Record<string, unknown>,
): ApprovalPause {
  const schema = definition.options_schema as ObjectSchema | undefined;
  const runLabel = RUN_SUBMIT_LABELS[definition.kind] ?? DEFAULT_RUN_SUBMIT_LABEL;
  const decidedBy = definition.pauses_for_approval_when ?? null;
  if (decidedBy) {
    const option = decidingOptionOf(schema, Object.keys(decidedBy));
    const chosen = option ? options[option] : undefined;
    if (typeof chosen === "string" && Object.hasOwn(decidedBy, chosen)) {
      if (decidedBy[chosen]) return { pauses: true, submitLabel: REVIEW_SUBMIT_LABEL };
      const choiceLabel = option ? schema?.properties?.[option]?.ui?.labels?.[chosen] : undefined;
      return { pauses: false, submitLabel: choiceLabel ?? runLabel };
    }
  }
  return definition.pauses_for_approval === false
    ? { pauses: false, submitLabel: runLabel }
    : { pauses: true, submitLabel: REVIEW_SUBMIT_LABEL };
}
