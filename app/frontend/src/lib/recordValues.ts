/** A scalar fact of a resource. */
export type Scalar = string | number | boolean | null;

/** `facts`, `relationships` or `raw` of a record; the API omits any of them when it is empty. */
export type Attributes = Record<string, unknown> | undefined;

/**
 * A scalar value of a record's `facts` or `relationships`; `null` when it is absent or not a scalar.
 *
 * @example
 * scalar(record.relationships, "cluster_identifier"); // "demo-aurora", or null
 */
export function scalar(values: Attributes, key: string): Scalar {
  const value = values?.[key];
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean" ? value : null;
}

/**
 * A list of names held by a record's `facts` or `relationships`; empty when it is absent.
 *
 * @example
 * names(record.relationships, "replica_identifiers"); // ["demo-orders-replica"]
 */
export function names(values: Attributes, key: string): string[] {
  const value = values?.[key];
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
}

/**
 * A map of text to text held by a record's `facts`, such as its tags; entries whose value is not a scalar
 * are left out. Empty when it is absent.
 *
 * @example
 * textMap(record.facts, "tags"); // { team: "orders" }
 */
export function textMap(values: Attributes, key: string): Record<string, string> {
  const value = values?.[key];
  if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
  const entries = Object.entries(value).filter(
    (entry): entry is [string, string | number | boolean] =>
      typeof entry[1] === "string" || typeof entry[1] === "number" || typeof entry[1] === "boolean",
  );
  return Object.fromEntries(entries.map(([name, entry]) => [name, String(entry)]));
}

/**
 * The entries of a list of objects held by a record's `raw` description, such as `DBClusterMembers`;
 * anything that is not an object is left out. Empty when it is absent.
 *
 * @example
 * objects(record.raw, "DBClusterMembers"); // [{ DBInstanceIdentifier: "demo-aurora-1", PromotionTier: 0 }]
 */
export function objects(values: Attributes, key: string): Record<string, unknown>[] {
  const value = values?.[key];
  if (!Array.isArray(value)) return [];
  return value.filter(
    (entry): entry is Record<string, unknown> =>
      typeof entry === "object" && entry !== null && !Array.isArray(entry),
  );
}

/**
 * A value as text for a cell: a dash for nothing, the text itself for a string, JSON for anything else.
 *
 * @example
 * displayValue(null); // "—"
 * displayValue({ a: 1 }); // '{"a":1}'
 */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}
