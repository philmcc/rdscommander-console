const PATH_SEPARATOR = " -> ";

/** Server problems sorted into the ones that belong to a form field and the ones that do not. */
export interface SortedProblems {
  /** Message per top-level field name. */
  byField: Record<string, string>;
  /** Everything else, in the order received. */
  general: string[];
}

/**
 * Sort the `problems` of a 422 answer. A schema error reads `<field>: <message>` (or
 * `options -> <field> -> <inner>: <message>`) and belongs under that field when it names a field of the
 * form; any other message, such as `work_mem must be an integer`, is general.
 *
 * @example
 * sortProblems(["name: required", "work_mem must be an integer"], ["name"])
 * // { byField: { name: "required" }, general: ["work_mem must be an integer"] }
 */
export function sortProblems(problems: string[], fieldNames: string[]): SortedProblems {
  const byField: Record<string, string> = {};
  const general: string[] = [];
  for (const problem of problems) {
    const colon = problem.indexOf(":");
    const path =
      colon < 0
        ? []
        : problem
            .slice(0, colon)
            .split(PATH_SEPARATOR)
            .map((part) => part.trim());
    if (path[0] === "options") path.shift();
    const field = path[0];
    if (field && fieldNames.includes(field)) {
      const message = problem.slice(colon + 1).trim();
      const inner = path.slice(1);
      const text = inner.length > 0 ? `${inner.join(PATH_SEPARATOR)}: ${message}` : message;
      byField[field] = byField[field] ? `${byField[field]}; ${text}` : text;
    } else {
      general.push(problem.replace(/^\s*:\s*/, ""));
    }
  }
  return { byField, general };
}
