import type { ParameterMetadata } from "@/api/types";

// These patterns and messages mirror `rdscommander/parameters/metadata.py`, so the form shows what the
// server will say before anything is submitted. Change them together.

/** RDS formulas such as `{DBInstanceClassMemory/32768}` or `LEAST({DBInstanceClassMemory/9531392},5000)`. */
const FORMULA_PATTERN = /^(GREATEST|LEAST)?\(?\{.*\}.*$/;
const NUMBER_TEXT = String.raw`(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?`;
const INTEGER_PATTERN = /^[+-]?\d+$/;
const NUMBER_PATTERN = new RegExp(`^[+-]?${NUMBER_TEXT}$`);
/** AllowedValues such as `64-2147483647`, `-1-86400` or `0-1.79769e+308`. */
const NUMERIC_RANGE_PATTERN = new RegExp(`^(-?${NUMBER_TEXT})-(-?${NUMBER_TEXT})$`);

/** The two ends of a numeric range such as `64-2147483647`, as written; null for anything else. */
function numberBounds(allowedValues: string): [string, string] | null {
  if (allowedValues.includes(",")) return null;
  const match = NUMERIC_RANGE_PATTERN.exec(allowedValues);
  return match?.[1] !== undefined && match[2] !== undefined ? [match[1], match[2]] : null;
}

function validateNumber(metadata: ParameterMetadata, value: string, bounds: [string, string]): string | null {
  const isInteger = metadata.data_type === "integer";
  if (!(isInteger ? INTEGER_PATTERN : NUMBER_PATTERN).test(value)) {
    return `${metadata.name} must be ${isInteger ? "an integer" : "a number"}`;
  }
  const [lowText, highText] = bounds;
  const number = Number(value);
  if (!(Number(lowText) <= number && number <= Number(highText))) {
    return `${metadata.name} must be between ${lowText} and ${highText}`;
  }
  return null;
}

/**
 * Why `value` cannot be set on the parameter, or null when it can. The same answer as the backend's
 * `validate_parameter_value`: a parameter RDS does not let anyone modify is refused, RDS formulas are
 * accepted (RDS evaluates them), and otherwise the value is checked against the parameter's type and
 * `allowed_values`: numeric ranges (in AWS's own bound text), choices, or, for `list` parameters such as
 * `shared_preload_libraries`, every comma-separated element against the choices.
 *
 * @example
 * validateParameterValue(workMem, "10"); // "work_mem must be between 64 and 2147483647"
 */
export function validateParameterValue(metadata: ParameterMetadata, value: string): string | null {
  if (!metadata.is_modifiable) return `${metadata.name} cannot be modified`;
  if (FORMULA_PATTERN.test(value)) return null;
  const allowedValues = (metadata.allowed_values ?? "").trim();
  const bounds = numberBounds(allowedValues);
  if ((metadata.data_type === "integer" || metadata.data_type === "float") && bounds !== null) {
    return validateNumber(metadata, value, bounds);
  }
  let choices = allowedValues
    .split(",")
    .map((choice) => choice.trim())
    .filter(Boolean);
  if (metadata.data_type === "list" && choices.length > 0) {
    const rejected = value
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item && !choices.includes(item));
    if (rejected.length === 0) return null;
    return `${metadata.name} must be a comma-separated list of ${choices.join(", ")} (${rejected.join(", ")} ${rejected.length === 1 ? "is" : "are"} not allowed)`;
  }
  if (metadata.data_type === "boolean" && choices.length === 0) choices = ["0", "1"];
  if (choices.length > 1 && !choices.includes(value)) {
    return `${metadata.name} must be one of ${choices.join(", ")}`;
  }
  return null;
}
